"""个人电脑工具类 (Computer ToolKit) — 让 LLM 在自己电脑上工作.

包含:
  - run_command:     在个人电脑上运行命令
  - computer_status: 查看电脑状态 (开机/关机/工作目录)
  - lan_devices:     查看内网电脑设备 (人名 / 电脑名 / IP)
  - reboot:          重启个人电脑

每个角色有独立电脑 (默认 Podman 虚拟电脑, 见 src/core/computer.py),
各电脑在同一 podman 自定义桥接网络 (maf-net) 中, 可互相通信.
MCP 工具通过 mcp_manager 的 mcp_add 安装到电脑上, 经 computer.run_mcp_tool 执行
(本工具类不再提供 run_mcp_tool, 由 mcp_manager 统一管理).
电脑开机时机: 角色加入/启动时自动开机; 一天结束 (下班总结) 或离职时自动关机.

用法:
    from src.python_tools.computer_toolkit import create_computer_toolkit
    role.add_toolkit(create_computer_toolkit())   # add_toolkit 自动绑定当前角色

接口文档 (模块结构与方法):

模块级函数:
    - create_computer_toolkit(): 创建个人电脑工具类.
    - bind_computer_to_toolkit(): 将当前角色绑定到 computer 工具类 (由 AgentRole.add_toolkit 内部调用).
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_computer_toolkit() -> ToolKit:
    """创建个人电脑工具类.

    返回:
        包含 run_command / computer_status / power_off / run_mcp_tool 的 ToolKit.
        角色 add_toolkit 后自动绑定该角色 (获取其个人电脑).
    """
    tk = ToolKit(name="computer", description="个人电脑工具: 运行命令, 运行 MCP 工具")
    # 持有当前角色引用 (由 AgentRole.add_toolkit 绑定)
    tk._computer_holder = {"role": None}  # type: ignore[attr-defined]

    def _computer() -> Any:
        r = tk._computer_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("computer 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r.computer  # 角色添加时自动创建 (默认 Podman)

    def _run_command(args: dict[str, Any]) -> str:
        """在个人电脑上运行命令."""
        cmd = args.get("command", "").strip()
        if not cmd:
            return "错误: 'command' 为必填参数."
        return _computer().run_command(cmd)

    def _computer_status(args: dict[str, Any]) -> str:
        """查看个人电脑状态."""
        comp = _computer()
        return comp.describe()

    def _reboot(args: dict[str, Any]) -> str:
        """重启个人电脑."""
        return _computer().reboot()

    def _lan_devices(args: dict[str, Any]) -> str:
        """查看内网电脑设备 (人名 / 电脑名 / IP)."""
        from src.core.computer import _COMPUTER_MANAGER
        devices = _COMPUTER_MANAGER.list_lan_devices()
        if not devices:
            return "(内网暂无电脑设备)"
        lines = []
        for d in devices:
            lines.append("- {} ({}) | 电脑 {} | {}".format(
                d["person"], d["role_id"], d["computer"], d["ip"]))
        return "内网电脑设备 (网络 maf-net):\n" + "\n".join(lines)

    tk.add_python_tool(
        "run_command",
        "在你自己个人的电脑上运行一条命令 (如 ls, cat, python, git 等), 返回命令输出. "
        "适合查看电脑上的文件、执行脚本、检查项目状态.",
        {"type": "object", "properties": {
            "command": {"type": "string", "description": "要运行的命令"},
        }, "required": ["command"]},
        _run_command,
    )
    tk.add_python_tool(
        "computer_status",
        "查看你个人电脑的状态: 是否开机, 工作目录在哪里, 电脑类型.",
        {"type": "object", "properties": {}},
        _computer_status,
    )
    tk.add_python_tool(
        "lan_devices",
        "查看内网电脑设备列表: 每个人名, 电脑名称, 内网 IP. "
        "各角色电脑在同一桥接网络 (maf-net) 中, 可据此找到其他电脑并通信.",
        {"type": "object", "properties": {}},
        _lan_devices,
    )
    tk.add_python_tool(
        "reboot",
        "重启你的个人电脑 (关机后自动开机). 适合清理运行状态或安装工具后重启.",
        {"type": "object", "properties": {}},
        _reboot,
    )
    return tk


def bind_computer_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 computer 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: computer 工具类实例.
        role:    绑定的 AgentRole (其 computer 属性提供个人电脑).
    """
    toolkit._computer_holder["role"] = role  # type: ignore[attr-defined]
