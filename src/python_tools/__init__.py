"""python_tools 工具类层包初始化.

提供全部 LLM 可调用工具 (ToolKit 工厂 + 默认装配清单 DEFAULT_TOOLKITS):

DEFAULT_TOOLKITS (角色自动装配的工具类):
    - memory: write_note/edit_note/list_notes/read_note/summary (笔记+每日总结)
    - time:   get_time/take_rest (作息)
    - todo:   todo_add/list/update/delete (个人待办)
    - task_view: my_tasks (任务列表视图)
    - hermes: hermes_new_conversation/hermes_send (电脑上的 Hermes Agent)
    - computer: run_command/computer_status/reboot (电脑操作)
    - mcp_manager: mcp_search/list/add/remove (MCP 工具管理)
    - skill_manager: skill 相关 (技能库)
    - talk: talk/list_roles (角色通信)
    - client: talk_to_client (与甲方交流)

模块级函数 (每文件一个工具类, 详见各文件头部):
    - create_memory_toolkit / create_time_toolkit / create_todo_toolkit /
      create_task_view_toolkit / create_hermes_toolkit / create_computer_toolkit /
      create_mcp_manager_toolkit / create_skill_manager_toolkit /
      create_talk_toolkit / create_client_toolkit / create_hr_toolkit"""

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
