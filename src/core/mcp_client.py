"""MCP 服务器客户端 (core 层).

单个 MCP 服务器连接: 通过 npx 或自定义命令 (如容器内 podman exec)
启动服务器进程, 在后台线程事件循环中保持 ClientSession 存活, 工具调用
通过 run_coroutine_threadsafe 提交到该循环执行.

从 python_tools/mcp_toolkit.py 下沉到 core — MCPServer 只是 MCP stdio
传输客户端 (不是"LLM 工具"概念), 消除 core → python_tools 反向依赖
(computer.py 需要它, 而 python_tools/computer_toolkit 又反向依赖 core).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
