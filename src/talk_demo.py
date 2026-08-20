#!/usr/bin/env python3
"""通信演示脚本 (talk_demo.py): 演示多角色 talk 协作链.

4 个角色协作链: CEO → 产品 → 开发 → 测试, 用 talk 工具跨角色沟通.

模块级函数:
    - header(): 终端 UI 打印辅助
    - main(): 装配 4 角色 → 依次派任务 → 观察 talk 消息流转"""
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

BOLD = "\033[1m"; GREEN = "\033[32m"; CYAN = "\033[36m"
BLUE = "\033[34m"; MAGENTA = "\033[35m"; YELLOW = "\033[33m"; RED = "\033[31m"; RESET = "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 60}{RESET}\n")


def main():
    header("Inter-Role Communication — talk Tool Demo")

    # ── Define Roles ────────────────────────────────────────
    coder = AgentRole(
        name="李明",
        role_id="coder",
        title="Senior Backend Engineer",
        personality="严谨细致，写完代码后主动找 reviewer 审查。遇到架构问题会咨询 architect。",
        skills=["Python", "Go", "PostgreSQL", "Kubernetes"],
        interest_keywords={"bug", "fix", "crash", "code", "implement"},
    )

    reviewer = AgentRole(
        name="张伟",
        role_id="reviewer",
        title="Code Review Lead",
        personality="审查代码时发现架构隐患会立即通知 architect。对安全问题零容忍。",
        skills=["Code Review", "Security Audit"],
        interest_keywords={"pr", "review", "security", "code"},
    )

    architect = AgentRole(
        name="王建国",
        role_id="architect",
        title="System Architect",
        personality="收到咨询后给出简洁方案。如果需要代码实现，会委托 coder 执行。",
        skills=["System Design", "Microservices", "DDD"],
        interest_keywords={"architecture", "design", "migration", "架构"},
    )

    # ── Start Pool ──────────────────────────────────────────
    pool = RolePool()
    pool.add_role(coder)
    pool.add_role(reviewer)
    pool.add_role(architect)

    def on_start(role: AgentRole, task: Task) -> None:
        urg = Urgency(-task.urgency)
        color = {Urgency.CRITICAL: RED, Urgency.HIGH: YELLOW}.get(urg, "")
        sender = task.context.get("sender", "")
        via = f" {MAGENTA}← {sender}{RESET}" if sender else ""
        print(f"\n  {BLUE}[{role.name}]{RESET}{via} {color}▶ {urg.name}{RESET} — {task.description[:100]}")

    def on_done(role: AgentRole, task: Task) -> None:
        icon = f"{GREEN}✓{RESET}" if task.status == "done" else f"{RED}✗{RESET}"
        print(f"  {BLUE}[{role.name}]{RESET} {icon} done ({task.tokens_consumed}t)")
        # Show first 200 chars of result
        result_preview = task.result[:200].replace('\n', ' ')
        print(f"  {MAGENTA}→{RESET} {result_preview}...")

    coder.on_task_start = on_start
    coder.on_task_done = on_done
    reviewer.on_task_start = on_start
    reviewer.on_task_done = on_done
    architect.on_task_start = on_start
    architect.on_task_done = on_done

    pool.start()

    tools_summary = {name: role.mcp_tool_names for name, role in pool._roles.items()}
    print(f"\n  {GREEN}Auto-registered tools per role:{RESET}")
    for name, tools in tools_summary.items():
        print(f"    {name}: {tools}")

    # ════════════════════════════════════════════════════════════
    #  Collaboration chain: coder implements → reviewer audits
    #  → reviewer finds issue → alerts architect → architect delegates back to coder
    # ════════════════════════════════════════════════════════════
    header("Collaboration Chain: Coder → Reviewer → Architect → Coder")

    print(f"  {YELLOW}Starting: Coder implements a feature, should ask reviewer to review{RESET}\n")

    pool.assign_task("coder", Task(
        urgency=Urgency.HIGH,
        description=(
            "我刚实现了一个 JWT refresh token 轮换功能。代码在 PR #188。\n"
            "请使用 talk 工具通知 reviewer 进行代码审查，urgency 设为 HIGH。\n"
            "先简单描述你实现了什么，然后调用 talk 发送审查请求。"
        ),
    ))

    # Wait for the chain to complete
    time.sleep(20)

    # ── Direct talk (non-LLM path) ──────────────────────────
    header("Direct talk: Architect asks Coder a question")

    coder.talk_to("architect", "需要确认一下：新的 API gateway 应该用 REST 还是 gRPC？", "NORMAL")

    time.sleep(10)

    # ── Final Status ────────────────────────────────────────
    header("Final Status")
    for name, s in pool.get_status().items():
        print(f"  {name:12} busy={s['busy']}  queue={s['queue_depth']}")

    pool.shutdown()
    print(f"\n{BOLD}{GREEN}Talk Demo Complete.{RESET}\n")


if __name__ == "__main__":
    main()
