"""Event Dispatcher — 事件广播到所有角色的 Layer 1-3 过滤.

Bridges EventBus → RolePool:
  1. trigger(event) fans out to all roles
  2. Each role independently runs Layer 1-3 via AgentRole.evaluate_event()
  3. If PASS: converts event → Task, inserts into role's priority queue
  4. If BLOCKED/AMBIENT: logs reason, no task created for that role

接口文档 (模块结构与方法):

类与方法:
    EventDispatcher:
        - trigger(): Fan out an event to roles.
        - get_stats(): 见方法源码
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.roles import AgentRole, RolePool, Task
from src.core.types import AgentState, Event, Priority

logger = logging.getLogger(__name__)


# # 事件分发器: 将事件广播给所有角色, 每个角色独立运行3层过滤. PASS事件自动转Task插入队列
class EventDispatcher:
    """Broadcasts events to all roles with per-role filtering.

    Usage:
        pool = RolePool()
        pool.add_role(coder)
        pool.add_role(reviewer)
        pool.start()

        dispatcher = EventDispatcher(pool)
        results = dispatcher.trigger(event)
        # results: {"coder": (True, "PASS..."), "reviewer": (False, "Salience 0.2 < 0.4")}
    """

    def __init__(self, pool: RolePool):
        self._pool = pool
        self.stats: dict[str, int] = {
            "total_events": 0,
            "total_tasks_created": 0,
            "roles_notified": 0,
            "roles_activated": 0,
            "roles_skipped": 0,
        }

    # ── Public API ─────────────────────────────────────────

# # 触发事件广播. 返回{role_id: {accepted, reason, task_id}}. 每个角色调用evaluate_event()
    def trigger(self, event: Event) -> dict[str, dict[str, Any]]:
        """Fan out an event to roles.

        - 广播事件 (target_role=None): 所有角色各自运行 Layer 1-3 过滤.
        - 定向事件 (target_role=xxx):  只投递给指定角色, 直接接受 (跳过过滤).
          用于定时任务提醒等"只有本人该收到"的事件.

        Returns per-role result dict:
            {"role_name": {"accepted": bool, "reason": str, "task_id": str|None}}
        """
        self.stats["total_events"] += 1
        results: dict[str, dict[str, Any]] = {}

        logger.info(
            "EventDispatcher trigger: id=%s type=%s/%s priority=%s target=%s",
            event.id, event.source, event.event_type, event.priority.name,
            event.target_role or "(广播)",
        )

        # 定向事件: 只投递给 target_role, 其他角色跳过
        if event.target_role is not None:
            # 目标角色不存在: 不能静默丢弃, 打警告并返回 (其余角色已标记跳过)
            if event.target_role not in {r.role_id for r in self._pool.all_roles()}:
                logger.warning(
                    "EventDispatcher: 定向事件目标角色 '%s' 不存在, 事件丢弃 "
                    "(id=%s type=%s)", event.target_role, event.id, event.event_type)
                return results
            for role in self._pool.all_roles():
                if role.role_id == event.target_role:
                    continue
                self.stats["roles_notified"] += 1
                self.stats["roles_skipped"] += 1
                results[role.role_id] = {
                    "accepted": False,
                    "reason": f"定向事件, 目标: {event.target_role}",
                    "task_id": None,
                }

        for role in self._pool.all_roles():
            role_name = role.role_id
            # 定向事件: 只处理目标角色, 直接接受 (任务是它自己创建的提醒)
            if event.target_role is not None:
                if role_name != event.target_role:
                    continue
                # 已下班 (OFF_DUTY/WRAPPING_UP) 或等待回复中 (WAIT) 的角色不被
                # 非紧急定向事件打扰: 强制入队会让 worker 在下班/等待时间处理提醒
                if event.priority < Priority.EMERGENCY and role.state in (
                        AgentState.OFF_DUTY, AgentState.WRAPPING_UP, AgentState.WAIT):
                    self.stats["roles_skipped"] += 1
                    logger.info(
                        "  → [%s] SKIPPED: 角色已%s, 非紧急定向事件不打扰",
                        role_name, role.state.value)
                    # 定向通知: 目标角色已下班被打断 → 写入该角色活动日志
                    role.journal(
                        f"定向通知 [{event.source}/{event.event_type}] "
                        f"({event.priority.name}): 跳过 — 角色已{role.state.value}")
                    results[role_name] = {
                        "accepted": False,
                        "reason": f"角色已{role.state.value}, 非紧急定向事件不打扰",
                        "task_id": None,
                    }
                    continue
                accepted, reason = True, f"定向任务提醒 (target_role={role_name})"
            else:
                self.stats["roles_notified"] += 1
                accepted, reason = role.evaluate_event(event)

            task_id = None
            if accepted:
                task = role.event_to_task(event)
                role.add_task(task)
                task_id = task.task_id
                self.stats["roles_activated"] += 1
                self.stats["total_tasks_created"] += 1
                logger.info(
                    "  → [%s] ACCEPTED: %s → Task %s (urgency=%s)",
                    role_name, reason, task_id, abs(task.urgency),
                )
            else:
                self.stats["roles_skipped"] += 1
                logger.info("  → [%s] SKIPPED: %s", role_name, reason)
                # 全局通知: 未接受的角色也写入活动日志 (含原因), 保证每个角色都记一条
                role.journal(
                    f"全局通知 [{event.source}/{event.event_type}] "
                    f"({event.priority.name}): 跳过 — {reason}")

            results[role_name] = {
                "accepted": accepted,
                "reason": reason,
                "task_id": task_id,
            }

        return results

    def get_stats(self) -> dict[str, int]:
        return dict(self.stats)
