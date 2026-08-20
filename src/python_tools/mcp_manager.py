"""MCP 工具管理类 (MCPManager) — 管理每角色电脑上的 MCP 服务器工具.

架构 (C 方案, 2026-08): 每个角色的电脑 (podman 容器) 内运行独立的
MCP filesystem 服务器, 授权目录 = 容器内 /home/agent. MCPManager 不维护
全局工具池, 所有查询/安装都基于当前角色自己的电脑服务器.

同时把管理操作打包成 LLM tool-call 工具 (mcp_manager 工具类):
  - mcp_search / mcp_list      — 搜索、列出本电脑 MCP 服务器上的工具
  - mcp_add / mcp_remove       — 为当前角色添加/移除工具
  - mcp_my_tools               — 查看当前角色已添加的工具

用法:
    from src.python_tools.mcp_manager import MCPManager, create_mcp_manager_toolkit

    mgr = MCPManager()                        # 全局一个实例即可
    tk = create_mcp_manager_toolkit(mgr)      # 打包成 LLM 工具类
    role.add_toolkit(tk)                      # add_toolkit 自动绑定当前角色
    # 或编程式:
    mgr.add_tool(role, "read_file")           # 给角色添加工具

接口文档 (模块结构与方法):

模块级函数:
    - create_mcp_manager_toolkit(): 把 MCP 管理操作打包成 LLM 可调用的工具类.
    - bind_mcp_manager_to_toolkit(): 将当前角色绑定到 mcp_manager 工具类 (由 AgentRole.add_toolkit 内部调用).

类与方法:
    MCPManager:
        - install_group_defaults(): 把指定组的 MCP 工具作为默认工具安装到角色 (从角色电脑的独立服务器).
        - add_tool(): 为角色安装一个 MCP 工具 (来自该角色电脑的独立 MCP 服务器).
        - remove_tool(): 从角色电脑卸载一个 MCP 工具.
        - list_role_tools(): 列出角色电脑上已安装的 MCP 工具.
        - role_toolkit(): 将角色已添加的 MCP 工具打包成一个 ToolKit (供提示词/汇总展示).
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


class MCPManager:
    """MCP 工具管理器: 管理每角色电脑上的 MCP 服务器工具.

    不维护全局工具池 — 工具都来自角色自己的电脑服务器 (C 方案).
    """

    def __init__(self):
        self._role_tools: dict[str, set[str]] = {}      # role_id → {已添加的工具名}

    # ── 角色工具管理 ──────────────────────────────────────

    def install_group_defaults(self, role: Any, group: str) -> list[str]:
        """把指定组的 MCP 工具作为默认工具安装到角色 (从角色电脑的独立服务器).

        供默认加载使用: 角色加入/启动时自动装配某个 MCP 工具组
        (如 file_ops 文件操作), 无需 mcp_search/mcp_add.

        参数:
            role:  AgentRole 实例.
            group: 工具组名 (如 "file_ops", 与 mcp_group_rules.json 分组一致).

        返回:
            成功安装的工具名列表 (跳过已存在/失败的).
        """
        # 1) 确保角色电脑的独立 MCP 服务器已安装 (自动创建的电脑)
        computer = role.computer
        computer.install_mcp_server()
        installed = computer.list_installed_mcp_tools()
        if not installed:
            logger.warning("[%s] 电脑无 MCP 服务器工具, 跳过默认组 '%s'",
                           role.role_id, group)
            return []

        # 2) 按分组规则过滤电脑服务器上的工具
        from src.python_tools.mcp_toolkit import _match_group, load_rules
        patterns: list[str] = []
        for g in load_rules().get("groups", []):
            if g["name"] == group:
                patterns = g.get("match", [])
                break
        targets = [n for n in installed
                   if not patterns or _match_group(n, patterns)]

        # 3) 逐个安装到角色 (经 add_tool, 从电脑服务器取)
        ok = []
        for name in targets:
            try:
                r = self.add_tool(role, name)
                if not r.startswith("错误"):
                    ok.append(name)
            except Exception:
                logger.exception("[%s] MCP 默认工具安装失败: %s", role.role_id, name)
        logger.info("[%s] MCP 默认工具组 '%s' 已安装 %d 个工具: %s",
                    role.role_id, group, len(ok), ok)
        return ok

    def add_tool(self, role: Any, tool_name: str) -> str:
        """为角色安装一个 MCP 工具 (来自该角色电脑的独立 MCP 服务器).

        安装语义:
          1. 确保角色电脑的独立 MCP 服务器已安装 (自动创建时自动装)
          2. 工具从电脑服务器工具池取 (computer._mcp_tools), 归属该电脑
          3. 在角色 ToolRegistry 注册一个代理 handler — 调用时转发到
             computer.run_mcp_tool, 在角色电脑的服务器上执行

        参数:
            role:      AgentRole 实例.
            tool_name: 工具名 (须在该角色电脑的服务器上存在, 如 "read_file").

        返回:
            操作结果说明 (成功/已存在/不存在).
        """
        # 1) 电脑的独立 MCP 服务器 (自动创建时已装, 幂等)
        computer = role.computer
        computer.install_mcp_server()
        role_id = role.role_id
        mine = self._role_tools.setdefault(role_id, set())
        if tool_name in mine:
            return f"工具 '{tool_name}' 已添加给 {role_id}, 无需重复添加."

        # 2) 从电脑服务器取工具
        td = computer._mcp_tools.get(tool_name)
        if td is None:
            return (f"错误: 电脑[{role_id}] 的 MCP 服务器上没有名为 '{tool_name}' 的工具. "
                    f"本电脑已安装: {computer.list_installed_mcp_tools() or '(无)'}. "
                    f"可用 mcp_search / mcp_list 查看全局可用工具.")

        # 3) 角色注册代理 handler → 转发到电脑服务器上执行
        from src.core.tools import ToolRegistry
        if role._tools is None:
            role._tools = ToolRegistry()
        role._tools.add_tool(
            name=td.name,
            description=td.description,
            input_schema=td.input_schema,
            handler=lambda args, _n=tool_name: computer.run_mcp_tool(_n, args),
            source=td.source,
        )
        mine.add(tool_name)
        logger.info("[%s] MCP 工具已安装到电脑: %s (来源 %s)", role_id, tool_name, td.source)
        return f"成功: 工具 '{tool_name}' 已安装到 {role_id} 的电脑 ({td.description[:60]})"

    def remove_tool(self, role: Any, tool_name: str) -> str:
        """从角色电脑卸载一个 MCP 工具.

        参数:
            role:      AgentRole 实例.
            tool_name: 工具名.

        返回:
            操作结果说明 (成功/未添加/不存在).
        """
        role_id = role.role_id
        mine = self._role_tools.get(role_id, set())
        if tool_name not in mine:
            return f"工具 '{tool_name}' 尚未添加给 {role_id}, 无需移除."

        # 1) 从角色电脑卸载
        computer = role.computer
        computer.uninstall_mcp_tool(tool_name)

        # 2) 从角色 ToolRegistry 移除
        from src.core.tools import ToolRegistry
        if role._tools is None:
            role._tools = ToolRegistry()
        role._tools.remove_tool(tool_name)
        mine.discard(tool_name)
        logger.info("[%s] MCP 工具已从电脑卸载: %s", role_id, tool_name)
        return f"成功: 工具 '{tool_name}' 已从 {role_id} 的电脑卸载."

    def list_role_tools(self, role: Any) -> list[dict[str, str]]:
        """列出角色电脑上已安装的 MCP 工具."""
        computer = role.computer
        return [
            {"name": n, "description": (computer._mcp_tools[n].description or "")[:120]}
            for n in computer.list_installed_mcp_tools() if n in computer._mcp_tools
        ]

    # ── 角色工具包 (MCP 工具集合, 按组) ──────────────────

    def role_toolkit(self, role: Any) -> ToolKit:
        """将角色已添加的 MCP 工具打包成一个 ToolKit (供提示词/汇总展示)."""
        tk = ToolKit(name=f"mcp[{role.role_id}]", description="该角色已启用的 MCP 工具")
        computer = role.computer
        for n in sorted(self._role_tools.get(role.role_id, set())):
            td = computer._mcp_tools.get(n)
            if td is not None:
                tk._tools[n] = td
        return tk


# ── LLM 管理工具 (打包成 tool_call 工具类) ────────────────

def create_mcp_manager_toolkit(manager: MCPManager) -> ToolKit:
    """把 MCP 管理操作打包成 LLM 可调用的工具类.

    参数:
        manager: MCPManager 实例 (全局共享).

    返回:
        包含 mcp_search / mcp_list / mcp_add / mcp_remove / mcp_my_tools 的工具类.
        角色 add_toolkit 后, LLM 即可自主搜索/添加/移除 MCP 工具.
    """
    tk = ToolKit(name="mcp_manager", description="MCP 工具管理: 搜索/添加/移除本地 MCP 工具")
    # 持有 manager 与当前角色引用 (由 AgentRole.add_toolkit 绑定)
    tk._mcp_holder = {"manager": manager, "role": None}  # type: ignore[attr-defined]

    def _role() -> Any:
        r = tk._mcp_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("mcp_manager 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r

    def _mcp_search(args: dict[str, Any]) -> str:
        """搜索当前角色电脑 MCP 服务器上可用的工具."""
        kw = args.get("keyword", "").strip().lower()
        if not kw:
            return "请提供 keyword 搜索词."
        computer = _role().computer
        hits = [
            {"name": n, "description": (td.description or "")}
            for n, td in sorted(computer._mcp_tools.items())
            if kw in n.lower() or kw in (td.description or "").lower()
        ]
        if not hits:
            return (f"没有匹配 '{kw}' 的 MCP 工具. "
                    f"本电脑服务器已装: {computer.list_installed_mcp_tools() or '(无)'}. "
                    f"可用 mcp_list 查看全部.")
        lines = [f"搜索 '{kw}' 找到 {len(hits)} 个工具:"]
        for h in hits:
            lines.append(f"- {h['name']}: {h['description']}")
        return "\n".join(lines)

    def _mcp_list(args: dict[str, Any]) -> str:
        """列出当前角色电脑 MCP 服务器上全部可用工具."""
        computer = _role().computer
        avail = [
            {"name": n, "description": (td.description or "")}
            for n, td in sorted(computer._mcp_tools.items())
        ]
        if not avail:
            return "本电脑暂无 MCP 服务器工具 (服务器可能未连接)."
        lines = [f"本电脑 MCP 服务器共有 {len(avail)} 个工具:"]
        for a in avail:
            lines.append(f"- {a['name']}: {a['description']}")
        return "\n".join(lines)

    def _mcp_add(args: dict[str, Any]) -> str:
        """为当前角色添加一个 MCP 工具."""
        name = args.get("tool_name", "").strip()
        if not name:
            return "请提供 tool_name."
        return manager.add_tool(_role(), name)

    def _mcp_remove(args: dict[str, Any]) -> str:
        """从当前角色移除一个 MCP 工具."""
        name = args.get("tool_name", "").strip()
        if not name:
            return "请提供 tool_name."
        return manager.remove_tool(_role(), name)

    def _mcp_my_tools(args: dict[str, Any]) -> str:
        """查看当前角色已添加的 MCP 工具."""
        mine = manager.list_role_tools(_role())
        if not mine:
            return "你还没有添加任何 MCP 工具. 可用 mcp_search / mcp_list 寻找, 用 mcp_add 添加."
        lines = [f"你已添加 {len(mine)} 个 MCP 工具:"]
        for m in mine:
            lines.append(f"- {m['name']}: {m['description']}")
        return "\n".join(lines)

    tk.add_python_tool(
        "mcp_search",
        "搜索本地已有的 MCP 工具 (按名称或描述关键词). 先搜索找到合适的工具, 再用 mcp_add 添加给自己.",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词, 如 file/git/issue/read"},
        }, "required": ["keyword"]},
        _mcp_search,
    )
    tk.add_python_tool(
        "mcp_list",
        "列出本地全部可用的 MCP 工具 (名称+简述). 查看有哪些工具可用.",
        {"type": "object", "properties": {}},
        _mcp_list,
    )
    tk.add_python_tool(
        "mcp_add",
        "为当前角色添加一个本地已有的 MCP 工具. 添加后即可在后续任务中直接调用该工具.",
        {"type": "object", "properties": {
            "tool_name": {"type": "string", "description": "要添加的工具名, 如 read_file"},
        }, "required": ["tool_name"]},
        _mcp_add,
    )
    tk.add_python_tool(
        "mcp_remove",
        "从当前角色移除一个已添加的 MCP 工具.",
        {"type": "object", "properties": {
            "tool_name": {"type": "string", "description": "要移除的工具名"},
        }, "required": ["tool_name"]},
        _mcp_remove,
    )
    tk.add_python_tool(
        "mcp_my_tools",
        "查看当前角色已添加的 MCP 工具列表.",
        {"type": "object", "properties": {}},
        _mcp_my_tools,
    )
    return tk


def bind_mcp_manager_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 mcp_manager 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: mcp_manager 工具类实例.
        role:    绑定的 AgentRole.
    """
    toolkit._mcp_holder["role"] = role  # type: ignore[attr-defined]
