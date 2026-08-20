"""Event Bus — 定时事件调度表 (时间层底座).

EventBus 只承担"定时事件注册 / 取消 / 到期取出"的调度表职责:
  - register_event(ev, tick=N): 存入 _tick_schedule
  - cancel_event / list_scheduled_events / _check_due_events

**事件过滤不在 EventBus** (2026-08 收敛): 3 层过滤是每角色独立的
`AgentRole.evaluate_event()` (roles.py)。运行时路径:
  TimeEventBus._dispatch → AgentSystem._on_time_event → EventDispatcher.trigger
  → role.evaluate_event (Layer 1 状态掩码 → Layer 2 显著性 → 转 Task 入队)。
旧的 `process_event` 单 Agent 过滤管线与 AmbientBuffer 已删除。

接口文档 (模块结构与方法):

类与方法:
    EventBus:
        - register_event(): 向调度表注册一个定时事件.
        - cancel_event(): 取消一个已注册的定时事件 (仅调度表中的).
        - list_scheduled_events(): 列出待触发的定时事件 (按触发 Tick 排序).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.types import Event

logger = logging.getLogger(__name__)


class EventBus:
    """定时事件调度表: {event_id: (trigger_tick, Event)}.

    子类 TimeEventBus (time_manager.py) 提供时间线程与事件发送回调;
    本类不持有任何过滤管线 (3 层过滤在 AgentRole.evaluate_event).
    """

    def __init__(self) -> None:
        self._tick_schedule: dict[str, tuple[int, Event]] = {}

    # ── 事件注册 (时间与事件深度绑定) ────────────────────

    def register_event(self, event: Event, tick: Optional[int] = None) -> str:
        """向调度表注册一个定时事件.

        参数:
            event: 要注册的 Event.
            tick:  触发绝对 Tick (必填; None 已无意义 — 立即触发请用
                   TimeEventBus.register_event, 它走 _dispatch → EventDispatcher).

        返回:
            事件 ID.

        异常:
            ValueError: tick 为 None (裸 EventBus 无发送回调, 无法立即投递).
        """
        if tick is None:
            raise ValueError(
                "EventBus.register_event 需要显式 tick (立即触发请用 "
                "TimeEventBus.register_event, 它走 _dispatch → EventDispatcher)"
            )
        event.trigger_tick = tick
        self._tick_schedule[event.id] = (tick, event)
        logger.info("EventBus 注册定时事件: id=%s type=%s → tick %d",
                    event.id, event.event_type, tick)
        return event.id

    def cancel_event(self, event_id: str) -> bool:
        """取消一个已注册的定时事件 (仅调度表中的)."""
        return self._tick_schedule.pop(event_id, None) is not None

    def list_scheduled_events(self) -> list[dict[str, Any]]:
        """列出待触发的定时事件 (按触发 Tick 排序)."""
        return [
            {"event_id": eid, "tick": tk, "type": ev.event_type,
             "target_role": ev.target_role}
            for eid, (tk, ev) in sorted(self._tick_schedule.items(),
                                        key=lambda kv: kv[1][0])
        ]

    def _check_due_events(self, current_tick: int) -> list[Event]:
        """检查并取回到期事件 (由时间线程周期性调用).

        参数:
            current_tick: 当前绝对 Tick.

        返回:
            到期的事件列表 (已从调度表移除, 调用方负责投递).
        """
        due = []
        for eid, (tk, ev) in list(self._tick_schedule.items()):
            if current_tick >= tk:
                del self._tick_schedule[eid]
                due.append(ev)
        return due
