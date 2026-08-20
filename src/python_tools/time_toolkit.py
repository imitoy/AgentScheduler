"""时间工具类 (Time ToolKit) — 作息系统的时间工具.

包含:
  - get_time: 查看当前 Tick 与作息状态
  - take_rest: 休息工具. 无参数, 调用即进入休息状态 (ON_DUTY_IDLE),
    由事件/任务自动唤醒

时间规则: 1 Tick = 10 分钟. 系统启动 = Tick 0, 每天第 60 Tick 下班.

用法:
    from src.python_tools.time_toolkit import create_time_toolkit
    role.add_toolkit(create_time_toolkit())

接口文档 (模块结构与方法):

模块级函数:
    - create_time_toolkit(): 创建时间工具类.
    - bind_time_to_toolkit(): 将 TimeEventBus 绑定到时间工具类 (由 AgentRole.add_toolkit 内部调用).
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_time_toolkit() -> ToolKit:
    """创建时间工具类.

    返回:
        包含 get_time / take_rest 工具的 ToolKit.
    """

    tk = ToolKit(name="time", description="时间与作息工具类")

    # 工具类持有 time_manager / role 引用 (由 AgentRole.add_toolkit 注入)
    tk._time_holder = {"manager": None, "role": None}  # type: ignore[attr-defined]

    def _get_time(args: dict[str, Any]) -> str:
        """查看当前作息时间.

        参数:
            args: 无.

        返回:
            当前 Tick 数与作息状态描述.
        """
        manager = tk._time_holder["manager"]  # type: ignore[attr-defined]
        if manager is None:
            raise RuntimeError("时间工具类尚未绑定 TimeEventBus, 请通过 role.add_toolkit() 注册")

        tick = manager.current_tick()
        return manager.describe() + f"\n当前 Tick 数: {tick}"

    def _take_rest(args: dict[str, Any]) -> str:
        """休息: 调用即进入休息状态 (ON_DUTY_IDLE).

        不设 tick 倒计时, 不会自动唤醒 — 保持休息直到有事件/任务到来,
        由事件投递 (TASK_DUE / talk / SHIFT_START 等) 自动唤醒.

        参数:
            args: 无需参数 (不再需要 ticks).

        返回:
            休息状态说明.
        """
        manager = tk._time_holder["manager"]  # type: ignore[attr-defined]
        role = tk._time_holder["role"]  # type: ignore[attr-defined]
        if manager is None:
            raise RuntimeError("时间工具类尚未绑定 TimeEventBus, 请通过 role.add_toolkit() 注册")

        # 进入休息状态: ON_DUTY_IDLE (上班空闲, 不拦截事件, 等事件唤醒)
        from src.core.types import AgentState
        if role is not None and role.state != AgentState.ON_DUTY_IDLE:
            role.state = AgentState.ON_DUTY_IDLE
            logger.info("[%s] 开始休息 (状态 ON_DUTY_IDLE, 等待事件唤醒)", role.role_id)

        return "已开始休息 (状态 ON_DUTY_IDLE). 有任务或事件到来时会自动唤醒."

    tk.add_python_tool(
        name="get_time",
        description=(
            "查看当前作息时间. 返回当前 Tick 数和作息状态. "
            "时间规则: 1 Tick = 10 分钟, 系统启动 = Tick 0, 每天第 60 Tick 下班. "
            "用于判断现在是上班时间还是下班时间, 或距离下班还有多久."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_get_time,
    )

    tk.add_python_tool(
        name="take_rest",
        description=(
            "休息工具. 调用后立即进入休息状态 (ON_DUTY_IDLE), 不需要指定时长. "
            "休息期间保持空闲, 不会自动唤醒; 当有任务或事件 (定时提醒/他人消息/上班) "
            "到来时会自动唤醒你. 适合在没有任务时使用."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_take_rest,
    )

    return tk


def bind_time_to_toolkit(toolkit: ToolKit, manager: Any, role: Any = None) -> None:
    """将 TimeEventBus 绑定到时间工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit:  时间工具类实例
        manager:  TimeEventBus 实例
        role:     绑定的 AgentRole (可选, 用于休息时设置状态)
    """
    toolkit._time_holder["manager"] = manager  # type: ignore[attr-defined]
    toolkit._time_holder["role"] = role        # type: ignore[attr-defined]
