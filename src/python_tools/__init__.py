"""Python 原生工具集.

本文件夹存放所有 Python 实现的工具类 (ToolKit)。
每个文件定义一个或多个工具类，角色通过 AgentRole.add_toolkit() 一次性导入。

默认工具 (DEFAULT_TOOLKITS): 角色被添加进 AgentSystem 时自动加载,
不需要额外配置。除 hr / client 之外的工具类均为默认:
  - memory: summary / write_note / edit_note / list_notes / read_note
  - time:   get_time / take_rest
  - task:   create_task / list_tasks / edit_task / delete_task
  - mcp_manager: mcp_search / mcp_list / mcp_add / mcp_remove / mcp_my_tools
    (MCP 工具自助管理, 共享全局 MCPManager, 懒加载服务器)
  - communication (talk / list_roles): 由 RolePool.start() 自动注入
    (需要 pool 引用, 见 roles.py _register_talk_tool)

需手动添加的工具类:
  - hr:     post_job_posting / list_candidates (招聘即入职)
  - client: talk_to_client (与甲方交流, 通常只给 CEO)

用法:
    from src.python_tools import DEFAULT_TOOLKITS
    for factory in DEFAULT_TOOLKITS.values():
        role.add_toolkit(factory())
"""

from typing import Callable

from src.python_tools.memory_toolkit import create_memory_toolkit
from src.python_tools.time_toolkit import create_time_toolkit
from src.python_tools.todo_toolkit import create_todo_toolkit
from src.python_tools.task_view_toolkit import create_task_view_toolkit
from src.python_tools.hermes_toolkit import create_hermes_toolkit
from src.python_tools.mcp_manager import MCPManager, create_mcp_manager_toolkit
from src.python_tools.computer_toolkit import create_computer_toolkit
from src.python_tools.skill_toolkit import SkillManager, create_skill_manager_toolkit

# 全局共享 MCP 管理器 (懒加载: 首次调用 mcp_* 工具时才连接服务器).
# 所有角色共享同一份工具池, 但每个角色 add_toolkit 时拿到独立的工具类实例
# (角色引用由 AgentRole.add_toolkit 自动绑定, 互不干扰).
_MCP_MANAGER = MCPManager()

# 全局共享技能库管理器 (懒扫描: 首次调用 skill_* 工具时扫描 data/skills/).
_SKILL_MANAGER = SkillManager()

# 默认工具类注册表: {名称: 工厂函数}
# 角色被添加进 AgentSystem 时 (auto_toolkits=True) 自动逐个加载.
# 注: 定时任务工具已并入笔记 (write_note 的 remind_tick 参数 = 定时提醒),
# 不再单独提供 create_task 工具 — 笔记与任务统一为"笔记"概念.
DEFAULT_TOOLKITS: dict[str, Callable[[], object]] = {
    "memory": create_memory_toolkit,   # 笔记 (含提醒=定时任务) / 总结
    "time": create_time_toolkit,
    "todo": create_todo_toolkit,       # Todo 清单 (个人待办, id+状态管理)
    "task_view": create_task_view_toolkit,   # 任务列表 (队列+历史视图)
    "hermes": create_hermes_toolkit,         # Hermes Agent 对话 (电脑上安装)
    "computer": create_computer_toolkit,          # run_command / computer_status / reboot
    "mcp_manager": lambda: create_mcp_manager_toolkit(_MCP_MANAGER),
    "skill_manager": lambda: create_skill_manager_toolkit(_SKILL_MANAGER),
}

# 默认 MCP 工具组: 角色加入/启动时自动把该组的 MCP 工具安装到个人电脑
# (工具本身来自 mcp_group_rules.json 配置的 MCP 服务器, 不是自研 Python 工具).
DEFAULT_MCP_GROUPS: tuple[str, ...] = ("file_ops",)  # 文件操作 MCP 工具集
