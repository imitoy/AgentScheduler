#!/usr/bin/env python3
"""角色演示脚本 (role_demo.py): 演示单角色创建/工具装配/任务执行.

模块级函数:
    - header(): 终端 UI 打印辅助
    - main(): 创建角色 → 挂工具 → 派任务 → 打印执行结果"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
os.environ.setdefault("DEEPSEEK_THINKING", "true")

from src.core.roles import AgentRole, RolePool, Task, Urgency

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("role_demo")

# ── Pretty printer ───────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}\n")


def main():
    header("Multi-Role Concurrent Task Scheduler — DeepSeek Integration")

    # ── Define Roles ────────────────────────────────────────
    coder = AgentRole(
        name="李明",
        role_id="coder",
        title="Senior Backend Engineer",
        personality="严谨细致，追求代码质量，善于排查复杂 bug",
        skills=["Python", "Go", "PostgreSQL", "Kubernetes", "Redis"],
        interest_keywords={"bug", "fix", "crash", "500", "error", "debug", "race", "down"},
    )

    reviewer = AgentRole(
        name="reviewer",
        title="Code Review Lead",
        personality="目光敏锐，对安全和性能问题零容忍，但沟通方式温和",
        skills=["Code Review", "Security Audit", "Performance Profiling"],
        system_prompt_extra="每次审查代码时，必须指出至少一个潜在风险点。",
        interest_keywords={"pr", "review", "security", "vuln", "audit", "code"},
    )

    architect = AgentRole(
        name="architect",
        title="System Architect",
        personality="全局视野，善于权衡取舍，能用简洁语言解释复杂架构",
        skills=["System Design", "Microservices", "DDD", "Event Sourcing"],
        system_prompt_extra="回答必须简洁，不超过3句话。先给结论再给理由。",
        interest_keywords={"migration", "architecture", "design", "scale", "refactor", "拆分", "迁移"},
    )

    # ── Start Pool ──────────────────────────────────────────
    pool = RolePool()
    pool.add_role(coder)
    pool.add_role(reviewer)
    pool.add_role(architect)

    # Register callbacks for live output
    def on_start(role: AgentRole, task: Task) -> None:
        urg = Urgency(-task.urgency)
        urgency_color = {Urgency.CRITICAL: "\033[31m", Urgency.HIGH: YELLOW}
        c = urgency_color.get(urg, "")
        print(f"  {BLUE}[{role.name}]{RESET} {c}▶ {urg.name}{RESET} — {task.description[:70]}")

    def on_done(role: AgentRole, task: Task) -> None:
        status_icon = f"{GREEN}✓{RESET}" if task.status == "done" else "\033[31m✗\033[0m"
        print(f"  {BLUE}[{role.name}]{RESET} {status_icon} done ({task.tokens_consumed}t) → {task.result[:100]}")

    coder.on_task_start = on_start
    coder.on_task_done = on_done
    reviewer.on_task_start = on_start
    reviewer.on_task_done = on_done
    architect.on_task_start = on_start
    architect.on_task_done = on_done

    pool.start()
    print(f"  {GREEN}3 roles started: coder, reviewer, architect{RESET}\n")

    # ════════════════════════════════════════════════════════════
    #  Scenario 1: Assign tasks at different urgencies
    # ════════════════════════════════════════════════════════════
    header("Scenario 1: Task Distribution")

    pool.assign_task("coder", Task(
        urgency=Urgency.NORMAL,
        description="Fix: login page returns 500 after JWT token expiry. The error log shows NullPointerException in AuthService.validate().",
    ))

    pool.assign_task("reviewer", Task(
        urgency=Urgency.HIGH,
        description="Review PR #188 — JWT refresh token rotation. This changes auth flow for all users. 200+ lines changed.",
    ))

    pool.assign_task("architect", Task(
        urgency=Urgency.CRITICAL,
        description="Database migration gone wrong. 30% of users report corrupted profile data. Need rollback plan NOW.",
    ))

    # Give time for all to complete
    time.sleep(8)

    # ── Status check ────────────────────────────────────────
    status = pool.get_status()
    print(f"\n  {MAGENTA}Queue Status:{RESET}")
    for name, s in status.items():
        print(f"    {name:12} busy={s['busy']}  queue={s['queue_depth']}  "
              f"task={s['current_task'] or 'idle'}")

    # ════════════════════════════════════════════════════════════
    #  Scenario 2: Queue stacking — add multiple tasks to same role
    # ════════════════════════════════════════════════════════════
    header("Scenario 2: Priority Queue — Multi-task Stacking on Coder")

    pool.assign_task("coder", Task(
        urgency=Urgency.LOW,
        description="Update README with new API endpoints documentation.",
    ))

    pool.assign_task("coder", Task(
        urgency=Urgency.HIGH,
        description="Critical: payment webhook returning 402 for all Stripe callbacks. Affecting revenue!",
    ))

    pool.assign_task("coder", Task(
        urgency=Urgency.NORMAL,
        description="Add unit tests for UserService.updateProfile() — coverage dropped to 45%.",
    ))

    pool.assign_task("coder", Task(
        urgency=Urgency.CRITICAL,
        description="PRODUCTION DOWN — healthcheck failing on all pods. CPU 100% on worker nodes. Need immediate fix.",
    ))

    print(f"  {YELLOW}4 tasks stacked on coder. Execution order should be:{RESET}")
    print(f"    1. CRITICAL — PRODUCTION DOWN")
    print(f"    2. HIGH — payment webhook 402")
    print(f"    3. NORMAL — unit tests")
    print(f"    4. LOW — README update\n")

    time.sleep(15)

    # ════════════════════════════════════════════════════════════
    #  Scenario 3: Concurrent role execution
    # ════════════════════════════════════════════════════════════
    header("Scenario 3: Concurrent Execution — All 3 Roles Busy")

    pool.assign_task("coder", Task(
        urgency=Urgency.HIGH,
        description="Debug race condition in WebSocket message handler. Intermittent double-delivery of messages.",
    ))

    pool.assign_task("reviewer", Task(
        urgency=Urgency.HIGH,
        description="Security review: new file upload endpoint. Check for path traversal, file type bypass, size limits.",
    ))

    pool.assign_task("architect", Task(
        urgency=Urgency.HIGH,
        description="Evaluate migration of monolith billing module to separate service. Estimate effort and risks.",
    ))

    time.sleep(10)

    # ════════════════════════════════════════════════════════════
    #  Scenario 4: Event Dispatcher — fan-out to all roles
    # ════════════════════════════════════════════════════════════
    header("Scenario 4: Event Dispatcher — Fan-out with Per-Role Filtering")

    from src.core.dispatcher import EventDispatcher
    from src.core.types import Event as BusEvent, Priority

    dispatcher = EventDispatcher(pool)

    # Event 1: Security vulnerability — only reviewer should care
    print(f"  {YELLOW}Event 1: GitHub security advisory (HIGH){RESET}")
    sec_event = BusEvent(
        source="github",
        event_type="security_advisory",
        priority=Priority.HIGH,
        payload={"title": "CVE-2026-1234: SQL injection in login endpoint", "severity": "critical"},
    )
    results = dispatcher.trigger(sec_event)
    for role_name, r in results.items():
        icon = f"{GREEN}PASS{RESET}" if r["accepted"] else f"{YELLOW}SKIP{RESET}"
        print(f"    {icon} {role_name}: {r['reason']}")

    # Event 2: Production crash — coder should accept, reviewer might too
    print(f"\n  {YELLOW}Event 2: Production crash alert (EMERGENCY){RESET}")
    crash_event = BusEvent(
        source="monitoring",
        event_type="crash_alert",
        priority=Priority.EMERGENCY,
        payload={"title": "All pods down — OOM killer triggered on worker nodes", "urgent": True},
    )
    results = dispatcher.trigger(crash_event)
    for role_name, r in results.items():
        icon = f"{GREEN}PASS{RESET}" if r["accepted"] else f"{YELLOW}SKIP{RESET}"
        print(f"    {icon} {role_name}: {r['reason']}")

    # Event 3: Architecture proposal — only architect should care
    print(f"\n  {YELLOW}Event 3: Architecture migration proposal (NORMAL){RESET}")
    arch_event = BusEvent(
        source="confluence",
        event_type="proposal",
        priority=Priority.NORMAL,
        payload={"title": "迁移单体计费模块到微服务架构的设计方案", "author": "alice"},
    )
    results = dispatcher.trigger(arch_event)
    for role_name, r in results.items():
        icon = f"{GREEN}PASS{RESET}" if r["accepted"] else f"{YELLOW}SKIP{RESET}"
        print(f"    {icon} {role_name}: {r['reason']}")

    # Event 4: Low-priority spam — no one should care
    print(f"\n  {YELLOW}Event 4: Random Slack message (LOW){RESET}")
    spam_event = BusEvent(
        source="slack",
        event_type="channel_message",
        priority=Priority.LOW,
        payload={"text": "Anyone up for lunch?", "channel": "#random"},
    )
    results = dispatcher.trigger(spam_event)
    for role_name, r in results.items():
        icon = f"{GREEN}PASS{RESET}" if r["accepted"] else f"{YELLOW}SKIP{RESET}"
        print(f"    {icon} {role_name}: {r['reason']}")

    # Stats
    ds = dispatcher.get_stats()
    print(f"\n  {MAGENTA}Dispatcher stats:{RESET} {ds['total_events']} events → "
          f"{ds['total_tasks_created']} tasks across {ds['roles_activated']} role-activations")

    # Wait for queued tasks to complete
    time.sleep(10)

    # ── Final Status ────────────────────────────────────────
    header("Final Status")
    status = pool.get_status()
    for name, s in status.items():
        print(f"  {name:12} busy={s['busy']}  queue={s['queue_depth']}")

    print(f"\n  {GREEN}✓ All tasks processed concurrently across 3 roles.{RESET}")

    # ── Shutdown ───────────────────────────────────────────
    pool.shutdown()
    print(f"\n{BOLD}{GREEN}Role Demo Complete.{RESET}\n")


if __name__ == "__main__":
    main()
