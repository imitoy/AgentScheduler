"""Core data types for the Shift & Event-Driven Agent Scheduler.

接口文档 (模块结构与方法):

类与方法:
    AgentState: (枚举/常量类)
    Priority: (枚举/常量类)
    Event: (枚举/常量类)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Optional


# ── Enums ────────────────────────────────────────────────────

# # Agent 生命周期状态枚举: OFF_DUTY(下班), ON_DUTY_IDLE(空闲), ON_DUTY_BUSY(忙碌), WRAPPING_UP(收尾), WAIT(等待回复)
class AgentState(str, Enum):
    """Agent lifecycle states."""
    OFF_DUTY      = "OFF_DUTY"       # 下班 — context flushed, not processing
    ON_DUTY_IDLE  = "ON_DUTY_IDLE"   # 上班空闲 — alive, listening for events
    ON_DUTY_BUSY  = "ON_DUTY_BUSY"   # 上班忙碌 — executing a workflow
    WRAPPING_UP   = "WRAPPING_UP"    # 收尾中 — finishing last task before shift end
    WAIT          = "WAIT"           # 等待中 — talk wait=true 同步等待对方回复 (worker 阻塞)


# # 事件优先级: 数值越大越紧急. LOW=1, NORMAL=3, HIGH=6, EMERGENCY=10
class Priority(IntEnum):
    """Event priority levels (higher = more urgent)."""
    LOW       = 1
    NORMAL    = 3
    HIGH      = 6
    EMERGENCY = 10


# ── Events ───────────────────────────────────────────────────

@dataclass
# # 标准化事件: id, source(来源), event_type(类型), priority(优先级), payload(载荷), timestamp(时间戳)
class Event:
    """A normalized event entering the system."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = ""                    # e.g. "github", "email", "slack", "cron", "time", "task"
    event_type: str = ""                # e.g. "new_pr", "mention", "alert", "SHIFT_START", "TASK_DUE"
    priority: Priority = Priority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # 定向投递: 只发给指定 role_id (None = 广播给所有角色)
    target_role: Optional[str] = None

    # 触发 Tick: None = 立即触发; 整数 = 在指定绝对 Tick 触发
    # (由 TimeEventBus 的时间线程到期投递). 用于"时间与事件深度绑定".
    trigger_tick: Optional[int] = None
