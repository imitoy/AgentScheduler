"""MCP 工具加载器 (MCP Tool Loader).

设计说明:
  - 不负责安装任何 MCP 服务器. 用户通过 npx 自行准备 (如: npx -y @modelcontextprotocol/server-github)
  - 配置文件 (mcp_group_rules.json) 只声明 npx 包名, 不写启动命令
  - 加载器自动构造 `npx -y <包名>` 命令, 通过 MCP Python SDK 的
    stdio_client + ClientSession.list_tools() 获取服务器暴露的工具列表
  - 按分组规则将工具分组为多个 ToolKit, 角色可一次导入某组

用法:
    from src.python_tools.mcp_toolkit import load_mcp_toolkits
    toolkits = load_mcp_toolkits()          # {组名: ToolKit}
    role.add_toolkit(toolkits["file_ops"])

接口文档 (模块结构与方法):

模块级函数:
    - load_rules(): 加载 MCP 分组规则 JSON.
    - load_mcp_toolkits(): 一键加载所有配置的 MCP 工具并分组.

类与方法:
    MCPServer:
        - connect(): 启动后台线程, 用 npx 拉起服务器并建立会话.
        - close(): 关闭服务器连接.
        - is_alive(): 探测服务器会话是否仍然可用 (进程死亡/管道断裂后返回 False).
        - list_tools(): 通过 SDK 的 ClientSession.list_tools() 获取服务器工具列表.
        - call_tool(): 调用服务器上的工具. 返回结果文本.
    MCPToolLoader:
        - load(): 加载所有配置的 MCP 服务器工具并分组.
        - list_loaded_tools(): 列出所有已加载工具及其来源服务器包名.
        - close(): 关闭所有 MCP 服务器连接.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from src.core.tools import ToolDef, ToolKit

logger = logging.getLogger(__name__)

# 分组规则文件路径 (可被环境变量覆盖)
RULES_FILE = Path(__file__).resolve().parent.parent / "config" / "mcp_group_rules.json"


def load_rules(rules_file: str | Path | None = None) -> dict[str, Any]:
    """加载 MCP 分组规则 JSON.

    参数:
        rules_file: 规则文件路径 (默认: src/config/mcp_group_rules.json).

    返回:
        规则字典: {"servers": ["npx包名", ...], "groups": [...], "default_group": "..."}
    """
    path = Path(rules_file) if rules_file else RULES_FILE
    if not path.exists():
        raise FileNotFoundError(f"MCP 分组规则文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════
#  MCP 服务器连接管理 (npx 启动 + stdio 会话)
# ═══════════════════════════════════════════════════════════

class MCPServer:
    """单个 MCP 服务器连接.

    通过 npx 启动服务器进程, 在后台线程事件循环中保持 ClientSession 存活,
    工具调用通过 run_coroutine_threadsafe 提交到该循环执行.

    参数:
        package: npx 包名 (如 "@modelcontextprotocol/server-github")
        args:    附加命令行参数 (可选, 如 filesystem 的授权目录)
    """

    def __init__(self, package: str, args: list[str] | None = None,
                 command: str | None = None,
                 command_args: list[str] | None = None):
        self.package = package
        self.args = args or []

        # 自定义启动命令 (如容器内: podman exec -i <容器> npx ...).
        # 指定后不再走宿主 npx, 直接 spawn command + command_args.
        self.command = command
        self.command_args = command_args or []

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._ready = threading.Event()
        self._connect_error: Optional[str] = None

    # ── 生命周期 ──────────────────────────────────────────

    def connect(self) -> None:
        """启动后台线程, 用 npx 拉起服务器并建立会话."""
        self._thread = threading.Thread(
            target=self._run_loop, name=f"mcp-{self.package}", daemon=True,
        )
        self._thread.start()
        # 等待连接就绪 (最多 20 秒, npx 首次拉包可能较慢)
        self._ready.wait(20)

    def _run_loop(self) -> None:
        """后台线程入口: 运行事件循环, 建立 session."""
        try:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _connect():
                if self.command:
                    # 自定义启动命令 (容器内执行等): 直接 spawn, 不经过宿主 npx
                    server_params = StdioServerParameters(
                        command=self.command,
                        args=self.command_args,
                    )
                else:
                    # 默认: npx -y <包名> [args...]
                    server_params = StdioServerParameters(
                        command="npx",
                        args=["-y", self.package, *self.args],
                    )
                async with stdio_client(server_params) as (read, write):
                    from mcp import ClientSession
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set()
                        logger.info("MCP 服务器 '%s' 连接成功 (npx -y %s)", self.package, self.package)
                        # 保持连接, 循环永远运行
                        await asyncio.Event().wait()

            self._loop.run_until_complete(_connect())
        except Exception as exc:
            self._connect_error = str(exc)
            logger.error("MCP 服务器 '%s' 连接失败: %s", self.package, exc)
            self._ready.set()  # 即使失败也唤醒, 避免卡死

    def close(self) -> None:
        """关闭服务器连接."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)

    def is_alive(self, timeout: float = 5.0) -> bool:
        """探测服务器会话是否仍然可用 (进程死亡/管道断裂后返回 False).

        用于电脑开机时的跨天重连检测: 容器 stop 会杀死 stdio 管道,
        但会话对象可能仍在, 只有实际往返一次才能确认. 走轻量 list_tools.
        """
        if self._session is None or self._loop is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.list_tools(), self._loop,
            )
            future.result(timeout=timeout)
            return True
        except Exception:
            return False

    # ── 工具操作 ──────────────────────────────────────────

    def list_tools(self) -> list[Any]:
        """通过 SDK 的 ClientSession.list_tools() 获取服务器工具列表."""
        if self._session is None or self._loop is None:
            return []
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.list_tools(), self._loop,
            )
            result = future.result(timeout=10)
            return list(result.tools)
        except Exception as exc:
            logger.error("MCP '%s' list_tools 失败: %s", self.package, exc)
            return []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用服务器上的工具. 返回结果文本."""
        if self._session is None or self._loop is None:
            return f"错误: MCP 服务器 '{self.package}' 未连接"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, arguments), self._loop,
            )
            result = future.result(timeout=60)
            # 提取文本内容
            parts = []
            for content in getattr(result, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    parts.append(text)
                elif hasattr(content, "type") and content.type == "text":
                    parts.append(str(content))
            if getattr(result, "is_error", False):
                return f"[MCP 错误] {''.join(parts)}"
            return "\n".join(parts) if parts else str(result)
        except Exception as exc:
            logger.error("MCP '%s' 调用 %s 失败: %s", self.package, name, exc)
            return f"错误: 调用 {name} 失败 - {exc}"


# ═══════════════════════════════════════════════════════════
#  分组加载器
# ═══════════════════════════════════════════════════════════

def _match_group(tool_name: str, patterns: list[str]) -> bool:
    """判断工具名是否匹配分组规则 (支持通配符 *)."""
    for pat in patterns:
        if fnmatch.fnmatch(tool_name, pat):
            return True
    return False


class MCPToolLoader:
    """MCP 工具加载器: npx 启动所有配置的服务器, 按规则分组.

    参数:
        rules_file: 分组规则 JSON 路径 (可选).
        server_args: 每个服务器的附加参数, 如 {"@modelcontextprotocol/server-filesystem": ["/tmp"]}

    用法:
        loader = MCPToolLoader(server_args={".../server-filesystem": ["/tmp"]})
        toolkits = loader.load()          # {组名: ToolKit}
        loader.close()                     # 关闭所有服务器连接
    """

    def __init__(self, rules_file: str | Path | None = None,
                 server_args: dict[str, list[str]] | None = None):
        self.rules = load_rules(rules_file)
        self.default_group = self.rules.get("default_group", "default")
        self.server_args = server_args or {}
        self._servers: dict[str, MCPServer] = {}   # 包名 -> 连接
        self._tool_owner: dict[str, str] = {}      # 工具名 -> 包名
        self._loaded = False
        self._result: dict[str, ToolKit] = {}      # 缓存上次加载结果

    # ── 加载流程 ──────────────────────────────────────────

    def load(self) -> dict[str, ToolKit]:
        """加载所有配置的 MCP 服务器工具并分组.

        返回:
            {组名: ToolKit} 字典. 每个 ToolKit 包含该组匹配到的所有工具.
        """
        if self._loaded:
            return self._result

        # 1. 连接所有配置的服务器 (自动构造 npx -y <包名>)
        for package in self.rules.get("servers", []):
            if not package or not isinstance(package, str):
                logger.warning("跳过无效服务器配置: %r", package)
                continue
            server = MCPServer(package=package, args=self.server_args.get(package, []))
            server.connect()
            self._servers[package] = server

        # 2. 通过 SDK 获取每个服务器的工具列表
        all_tools: dict[str, Any] = {}
        for package, server in self._servers.items():
            for tool in server.list_tools():
                tname = getattr(tool, "name", "")
                if not tname:
                    continue
                if tname in all_tools:
                    logger.warning("工具名冲突: '%s' 来自 '%s' 和 '%s', 保留第一个",
                                   tname, self._tool_owner.get(tname), package)
                    continue
                all_tools[tname] = tool
                self._tool_owner[tname] = package
                logger.info("MCP 工具加载: %s (来自 %s)", tname, package)

        self._loaded = True
        self._result = self._build_toolkits(all_tools)
        return self._result

    def _build_toolkits(self, all_tools: dict[str, Any]) -> dict[str, ToolKit]:
        """将工具按分组规则分配到各个 ToolKit."""
        groups = self.rules.get("groups", [])
        toolkits: dict[str, ToolKit] = {}
        group_tools: dict[str, dict[str, Any]] = {}

        for g in groups:
            gname = g["name"]
            toolkits[gname] = ToolKit(name=gname, description=g.get("description", ""))
            group_tools[gname] = {}

        default_gt = group_tools.get(self.default_group, {})

        # 分配工具到组 (default 组最后兜底)
        for tname, tool in all_tools.items():
            assigned = False
            for g in groups:
                if g["name"] == self.default_group:
                    continue
                if _match_group(tname, g.get("match", [])):
                    group_tools[g["name"]][tname] = tool
                    assigned = True
                    break
            if not assigned:
                default_gt[tname] = tool

        # 为每个组构建 ToolDef + 绑定服务器调用 handler
        for g in groups:
            gname = g["name"]
            for tname, tool in group_tools.get(gname, {}).items():
                owner = self._tool_owner.get(tname)
                server = self._servers.get(owner) if owner else None
                if server is None:
                    continue

                def _make_handler(srv=server, tn=tname):
                    def handler(args: dict[str, Any]) -> str:
                        return srv.call_tool(tn, args)
                    return handler

                td = ToolDef(
                    name=tname,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", {}) or {},
                    handler=_make_handler(),
                    source=f"mcp:{server.package}",
                    mcp_tool=tool,
                )
                toolkits[gname]._tools[tname] = td

        return toolkits

    # ── 查询 ──────────────────────────────────────────────

    def list_loaded_tools(self) -> list[dict[str, str]]:
        """列出所有已加载工具及其来源服务器包名."""
        result = []
        for tname, package in self._tool_owner.items():
            result.append({"tool": tname, "server": package})
        return result

    def close(self) -> None:
        """关闭所有 MCP 服务器连接."""
        for server in self._servers.values():
            server.close()
        self._servers.clear()
        self._loaded = False


# ── 便捷函数 ──────────────────────────────────────────────

def load_mcp_toolkits(rules_file: str | Path | None = None,
                      server_args: dict[str, list[str]] | None = None) -> dict[str, ToolKit]:
    """一键加载所有配置的 MCP 工具并分组.

    参数:
        rules_file: 分组规则 JSON 路径 (可选).
        server_args: 服务器附加参数, 如 {".../server-filesystem": ["/tmp"]}

    返回:
        {组名: ToolKit} 字典.
    """
    loader = MCPToolLoader(rules_file, server_args=server_args)
    return loader.load()
