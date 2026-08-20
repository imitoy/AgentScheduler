#!/usr/bin/env python3
"""系统主入口: 启动完整的多角色 AI 团队模拟 (main.py).

职责:
    1. 创建 AgentSystem, 装配 46 个默认工程角色 (含 CEO/COO/HR/CTO/负责人)
    2. 从 StateStore 恢复上次进度 (角色/任务/时间/电脑绑定)
    3. 启动时间引擎 + 角色线程池, 循环跑日 (上班 → 派任务 → 下班)
    4. 每天结束时自动保存状态, 提供交互式运行控制

模块级函数:
    - header() / step() / info() / ok() / warn(): 终端 UI 打印辅助
    - wait_until(t, timeout): 等待所有角色空闲或超时
    - run_one_day(system, day): 跑一天 (上班 → 等任务完成 → 下班)
    - main(): 主流程 (恢复状态 → 循环天数 → 保存状态)"""
from __future__ import annotations

import logging
import sys
import time as time_module
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 支持 `python src/main.py` 直接启动: 把项目根目录加入 sys.path,
# 否则 sys.path[0] 是 src/ 目录, `from src.core...` 导入会失败
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.agent_system import AgentSystem
from src.core.role_templates import DEFAULT_ROLES
from src.core.state_store import StateStore
from src.core.types import AgentState, Event, Priority
from src.python_tools.client_toolkit import create_client_toolkit
from src.python_tools.hr_toolkit import create_hr_toolkit

# 调试期: DEBUG 日志永久启用 (临时). 打印 LLM 追加内容/工具调用全链路.
# 第三方库 (requests/urllib3/httpcore) 的 DEBUG 刷屏太多, 压回 WARNING.
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
for _noisy in ("requests", "urllib3", "httpcore", "httpx", "openai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# 打印信息同步写入日志文件 (data/main_run.log), 控制台与文件各一份.
# 轮转: 单文件最大 20MB, 保留 3 个备份 — 多日运行日志不会无限增长.
LOG_FILE = PROJECT_ROOT / "data" / "main_run.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=20 * 1024 * 1024,
                                    backupCount=3, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                             datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_file_handler)

# 控制台打印 → 同时写日志文件 (供事后回看, 去掉 ANSI 颜色码)
def _console_print(msg: str, stream=None) -> None:
    """打印到控制台, 并把纯文本 (去 ANSI) 同步写日志文件."""
    print(msg, file=stream)
    clean = _strip_ansi(msg)
    if clean.strip():
        logging.getLogger("console").info(clean.rstrip())

BOLD = "\033[1m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
RED = "\033[31m"; CYAN = "\033[36m"; MAGENTA = "\033[35m"; RESET = "\033[0m"

import re as _re

def _strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码, 得到纯文本 (写日志文件用)."""
    return _re.sub(r"\x1b\[[0-9;]*m", "", text)

ROLE_IDS = sorted(DEFAULT_ROLES)   # 默认团队: 管理层 + 工程团队 (每个角色都有专属活动日志)

# 时间参数 (真实时间, 分钟/小时)
TICK_MINUTES = 10        # 1 Tick = 10 真实分钟
TICK1_MINUTES = 10       # 第 1 Tick = 10 分钟后
SHIFT_END_HOURS = 10     # 下班 = 10 小时后 (Tick 60)
DAY_BOUNDARY_HOURS = 24  # 跨天 = 24 小时后 (144 Tick)


def header(text: str) -> None:
    _console_print(f"\n{BOLD}{CYAN}{'═' * 62}{RESET}")
    _console_print(f"{BOLD}{CYAN}  {text}{RESET}")
    _console_print(f"{BOLD}{CYAN}{'═' * 62}{RESET}\n")


def step(text: str) -> None:
    _console_print(f"{MAGENTA}▶ {text}{RESET}")


def info(text: str) -> None:
    _console_print(f"  {text}")


def ok(text: str) -> None:
    _console_print(f"  {GREEN}✓ {text}{RESET}")


def warn(text: str) -> None:
    _console_print(f"  {YELLOW}⚠ {text}{RESET}")


def wait_until(desc: str, predicate, timeout_seconds: float) -> bool:
    """轮询等待条件满足 (真实时间).

    参数:
        desc:             等待说明 (打印用)
        predicate:        无参布尔函数
        timeout_seconds:  最长等待秒数

    返回:
        True=条件满足, False=超时.
    """
    info(f"等待: {desc} (最长 {timeout_seconds/60:.0f} 分钟)...")
    deadline = time_module.time() + timeout_seconds
    while time_module.time() < deadline:
        if predicate():
            ok(f"{desc} ✓")
            return True
        time_module.sleep(5)
    warn(f"等待超时: {desc}")
    return False


def run_one_day(system: AgentSystem, day: int, with_client_task: bool) -> None:
    """运行一天的完整流程 (真实时间, 由 TimeEventBus 自动走时).

    参数:
        system:           AgentSystem 实例
        day:              当前第几天
        with_client_task: 是否安排 CEO 与甲方沟通任务 (仅第 1 天为 True)
    """
    header(f"第 {day} 天")

    # ── 新的一天: 等待跨天边界 (day_number 变化 → SHIFT_START 自动触发) ──
    if day > 1:
        wait_until(
            f"第 {day} 天开始 (约 {DAY_BOUNDARY_HOURS*(day-1)} 小时后, SHIFT_START 自动触发)",
            lambda: system.day >= day,
            timeout_seconds=DAY_BOUNDARY_HOURS * 3600,
        )

    ok(f"当前: {system.describe()}")

    # ── 第 1 天: CEO 与甲方沟通 (仅此一次) ─────────────────
    if with_client_task:
        step("CEO 注册开局笔记提醒: Tick 1 (10 分钟后) 与用户沟通项目要求...")
        ceo_note = system.get_role("CEO").note_store.write_note(
            title="第1天-收集项目需求",
            content="与用户沟通项目要求, 收集今天要开发的项目需求",
            remind_tick=1,
            remind_day=day,
        )
        ok(f"笔记+提醒已注册: {ceo_note} (第 {day} 天 Tick 1 → CEO)")

        step("等待 Tick 1 触发 (CEO 任务 → 与用户沟通)...")
        fire_tick = (day - 1) * 144 + 1
        wait_until(
            f"Tick {fire_tick} 到达 (CEO 任务触发)",
            lambda: system.time_manager.current_tick() >= fire_tick,
            timeout_seconds=(TICK1_MINUTES + 5) * 60,
        )
        info("请在上方 [CEO] 提示处输入项目要求 (例如: 帮我开发一个支付系统)")
        # 等待 CEO 任务被处理 (用户输入后 LLM 继续)
        time_module.sleep(10)
    else:
        # 第 2 天起: 不再安排甲方沟通, 直接进入日常工作
        step("今天没有甲方沟通任务, 直接进入日常工作...")
        time_module.sleep(5)

    # ── 白天工作事件 ───────────────────────────────────────
    step("投递 LOW 事件 (闲聊, 应被显著性过滤, 0 Token)...")
    spam = Event(source="slack", event_type="chat", priority=Priority.LOW,
                 payload={"text": "中午吃什么?", "channel": "#random"})
    results = system.trigger(spam)
    info(f"LOW 过滤结果: { {k: v['accepted'] for k, v in results.items()} }")

    """
    step("投递 HIGH 工作工单 (新 PR 待处理)...")
    work = Event(source="github", event_type="new_pr", priority=Priority.HIGH,
                 payload={"pr_number": 188, "title": "fix: login token NPE", "urgent": True})
    results = system.trigger(work)
    accepted = [rid for rid, r in results.items() if r["accepted"]]
    info(f"HIGH 工单被接受: {accepted}")
    """

    # ── 等待下班 (Tick 60 = 10 小时后, SHIFT_END 自动触发) ─
    step("等待下班... (Tick 60 = 10 小时后, SHIFT_END 自动触发)")
    wait_until(
        "下班时刻到达 (SHIFT_END 触发)",
        lambda: system.time_manager.tick_of_day() >= 60,
        timeout_seconds=(SHIFT_END_HOURS + 1) * 3600,
    )
    time_module.sleep(5)  # 给角色收尾一小段时间

    step("等待角色调用 summary 工具 (40 角色并发, 最长 600 秒)...")
    deadline = time_module.time() + 600
    while time_module.time() < deadline:
        if all(system.get_role(rid).state == AgentState.OFF_DUTY for rid in ROLE_IDS):
            break
        time_module.sleep(5)

    # ── 检查下班状态与总结 ─────────────────────────────────
    step("检查下班状态...")
    off_duty = [rid for rid in ROLE_IDS
                if system.get_role(rid).state == AgentState.OFF_DUTY]
    if off_duty:
        ok(f"OFF_DUTY 角色: {len(off_duty)}/{len(ROLE_IDS)}"
           f" ({', '.join(off_duty[:6])}{'...' if len(off_duty) > 6 else ''})")
    else:
        warn("角色仍未全部 OFF_DUTY")

    for rid in ROLE_IDS:
        role = system.get_role(rid)
        summary = role.note_store.get_summary(day=day)
        if summary is None:
            # summary 工具保存后立即关机, 电脑回读路径失效 ("电脑未开机").
            # 直接读宿主机挂载目录 (Podman/Local 的 host_dir 都是
            # data/computers/<rid>, 关机也能读到; SSH 无映射则跳过).
            host_dir = role.computer.host_dir
            if host_dir:
                host_summary = Path(host_dir) / "notes" / f"_summary_day_{day}.md"
                if host_summary.exists():
                    summary = host_summary.read_text(encoding="utf-8")
        if summary:
            ok(f"[{rid}] 第{day}天总结已保存: {summary[:50]}...")
        else:
            info(f"[{rid}] 暂无总结")


def main() -> None:
    header("作息系统演示 — 真实时间流动 (1 Tick = 10 分钟)")

    # ── 1. 开局: 默认团队 (管理层 + 工程团队, 每个角色都有专属活动日志) ──
    step(f"创建 AgentSystem, 加入 {len(ROLE_IDS)} 个默认角色...")
    system = AgentSystem(role_ids=ROLE_IDS)
    system.get_role("CEO").add_toolkit(create_client_toolkit())
    system.get_role("HR").add_toolkit(create_hr_toolkit())
    ok(f"角色就绪: {len(system.pool.list_roles())} 人 (CEO/COO/HR + 工程团队)")
    ok("CEO 已装备 talk_to_client (与甲方实时交流)")
    ok("HR 已装备招聘工具 (post_job_posting / list_candidates)")

    # ── 0. 恢复上次进度 (StateStore: 角色档案/任务/对话/容器/时间) ──
    store = StateStore()
    restored = store.restore(system) if store.exists() else 0
    if restored:
        ok(f"已从存档恢复 {restored} 个角色 → {system.describe()}")
    else:
        ok("无存档, 从第 1 天 Tick 0 开始")

    # ── 2. 启动系统 (真实时钟, Tick 0 = 第 1 天上班) ───────
    system.start()
    ok(f"系统已启动: {system.describe()}")
    ok(f"时间规则: 1 Tick = {TICK_MINUTES} 分钟; 下班 = {SHIFT_END_HOURS} 小时后; "
       f"第 2 天 = {DAY_BOUNDARY_HOURS} 小时后")
    time_module.sleep(3)  # 等 SHIFT_START (Tick 0) 触发
    from collections import Counter
    states = Counter(system.get_role(rid).state.value for rid in ROLE_IDS)
    info(f"角色状态: {dict(states)}")

    # ── 3. 多日循环: 第 1 天有甲方沟通, 之后自动进入下一天 ──
    day = system.day  # 恢复存档后从上次的天数继续
    try:
        while True:
            run_one_day(system, day, with_client_task=(day == 1))

            # 一天结束: 自动进入第二天 (不再询问用户), 打印醒目横幅
            next_day = day + 1
            _console_print(f"\n{BOLD}{GREEN}{'═' * 62}{RESET}")
            _console_print(f"{BOLD}{GREEN}  🎉 第 {day} 天结束!{RESET}")
            _console_print(f"{BOLD}{GREEN}  已自动进入第 {next_day} 天: 将于约 "
                           f"{DAY_BOUNDARY_HOURS - SHIFT_END_HOURS} 小时后上班 "
                           f"(SHIFT_START 自动触发){RESET}")
            _console_print(f"{BOLD}{GREEN}{'═' * 62}{RESET}\n")
            day = next_day
    except KeyboardInterrupt:
        warn("\n收到 Ctrl+C, 正在保存进度并退出...")
    finally:
        # 退出自动保存: 角色档案/任务/对话/容器/时间进度 → data/state.json
        store.save(system)
        system.stop()
        _console_print(f"\n{BOLD}{GREEN}演示结束 ✓ (运行到第 {day} 天, 进度已保存){RESET}\n")


if __name__ == "__main__":
    sys.exit(main())
