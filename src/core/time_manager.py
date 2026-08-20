"""时间与事件总线 (TimeEventBus) — 以 Tick 为单位的作息时间 + 3 层过滤事件总线.

TimeEventBus 已并入 EventBus: TimeEventBus(EventBus) 既是时间源 (时钟/Tick/天),
又是事件总线 (3 层过滤管线 + 定时事件调度表). 时间与事件深度绑定:
向总线注册事件时可指定触发 Tick (不传默认立即触发).

规则:
  - 系统开始运行即为 Tick 0, 不依赖墙钟时间
  - 1 Tick = 10 分钟 (可配置)
  - 每天 = ticks_per_day 个 Tick (默认 144 = 24 小时), 系统启动当天为第 1 天
  - 每个工作日的 Tick 0 上班 (shift_start), Tick 60 下班 (shift_end)

事件触发 (独占后台线程):
  - 每天第 0 Tick   → 发送 SHIFT_START 事件 (优先级 EMERGENCY)
  - 每天第 60 Tick  → 发送 SHIFT_END   事件 (优先级 EMERGENCY)
  - 定时任务/定时事件到期 → 自动投递

用法:
    bus = TimeEventBus()
    bus.set_event_sender(dispatcher.trigger)   # 事件最终投递到各角色
    bus.start()                                # 启动时间线程 (记录启动时刻 = Tick 0)
    tick = bus.current_tick()                  # 当前 Tick (自启动累计)
    day = bus.day_number()                     # 当前第几天 (从 1 开始)

    # 注册事件: 立即触发 (不传 tick)
    bus.register_event(Event(source="email", event_type="urgent"))
    # 注册事件: 指定 Tick 触发 (到点由时间线程自动投递)
    bus.register_event(Event(...), tick=30)
    bus.stop()                                 # 停止线程

接口文档 (模块结构与方法):

类与方法:
    ScheduledTask:
        - absolute_fire_tick(): 计算绝对触发 Tick: (day-1)*ticks_per_day + target_tick.
    TimeEventBus:
        - register_event(): 向事件总线注册一个事件 (TimeEventBus 版).
        - set_event_sender(): 设置事件发送回调 (发送到事件分发器).
        - set_idle_checker(): 设置"全部角色是否空闲"判定回调.
        - set_fast_forward(): 开关快进功能.
        - set_clock(): 设置时间源 (默认 datetime.now). 用于模拟测试.
        - current_tick(): 获取当前 Tick 数 (自系统启动累计, 启动即为 0).
        - day_number(): 获取当前是第几天 (系统启动当天为第 1 天).
        - tick_of_day(): 获取今天内的 Tick 位置 (0 ~ ticks_per_day-1).
        - tick_to_time(): 将 Tick 转换为相对时钟 "HH:MM" (从每天第 0 Tick 起算).
        - is_working_hours(): 判断当前是否在上班时间内 (今日 Tick 在 [shift_start, shift_end) 之间).
        - ticks_until_shift_end(): 距下班还有多少 Tick (已下班返回 0).
        - get_shift_event(): 获取今日某个 Tick 位置对应的作息事件.
        - describe(): 返回当前作息状态的文字描述 (供工具/提示词使用).
        - start(): 启动时间线程 (独占线程, 周期性检查 Tick 并触发作息事件).
        - set_progress(): 设置恢复进度: 下次 start() 时把 Tick 直接设为 (day, tick_of_day).
        - stop(): 停止时间线程.
        - is_running(): 见方法源码
        - schedule_task(): 注册一个定时任务, 到达指定 Tick 时向事件总线发送提醒事件.
        - list_tasks(): 列出未触发的定时任务.
        - edit_task(): 编辑已有定时任务.
        - cancel_task(): 删除定时任务 (同时取消已注册的事件).
"""
from __future__ import annotations

import logging
import threading
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from src.core.event_bus import EventBus
from src.core.types import Event, Priority

logger = logging.getLogger(__name__)

# ── 作息关键事件 (大写统一) ─────────────────────────────────

EVENT_SHIFT_START = "SHIFT_START"   # 上班事件
EVENT_SHIFT_END = "SHIFT_END"       # 下班事件
EVENT_TASK_DUE = "TASK_DUE"         # 定时任务提醒事件

MINUTES_PER_TICK = 10       # 每 Tick 10 分钟
TICKS_PER_DAY = 144         # 每天 144 Tick (24 小时)
SHIFT_START_TICK = 0        # 上班: 每天第 0 Tick
SHIFT_END_TICK = 60         # 下班: 每天第 60 Tick (10 小时工作制)
TASK_TICK_MIN = 0           # 任务 Tick 范围下限
TASK_TICK_MAX = 60          # 任务 Tick 范围上限 (工作时间 0~60)

DEFAULT_CHECK_INTERVAL = 30  # 线程检查间隔 (秒)
FAST_FORWARD_IDLE_SECONDS = 60  # 全角色空闲多少秒后快进 (默认 1 分钟)


@dataclass
class ScheduledTask:
    """定时任务 (注册到 TimeEventBus, 到达指定 Tick 触发提醒事件).

    参数:
        task_id:      任务 ID (自动生成)
        owner_role:   所属角色 role_id (事件只投递给该角色)
        description:  任务内容描述
        target_tick:  目标 Tick (今日 0~60 范围内)
        day:          计划在哪一天触发 (默认创建时的当天)
        payload:      附加数据
        fired:        是否已触发 (触发后自动移除)
    """
    description: str
    owner_role: str
    target_tick: int
    day: int = 1
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time_module.time)
    fired: bool = False
    event_id: str = ""  # 已注册到事件调度表的事件 ID (空 = 未注册, 防重复注册)

    def absolute_fire_tick(self, ticks_per_day: int) -> int:
        """计算绝对触发 Tick: (day-1)*ticks_per_day + target_tick."""
        return (self.day - 1) * ticks_per_day + self.target_tick


@dataclass
class TimeEventBus(EventBus):
    """时间与事件深度绑定的事件总线 (TimeEventBus 并入 EventBus).

    继承 EventBus 的 3 层过滤管线, 并承担原 TimeEventBus 的全部职责:
      - 以 Tick 为单位的作息时间 (时钟/tick/天/上下班)
      - 时间线程 (周期性检查, 触发作息事件与到期定时事件)
      - 定时任务管理 (schedule_task 等, 底层复用 register_event 调度表)

    注册事件统一走 register_event(event, tick=None):
      - tick=None: 立即触发 (进入 3 层过滤管线)
      - tick=整数: 在指定绝对 Tick 触发 (时间线程到期自动投递)

    Tick 推进规则 (事件驱动, 不随真实时间流逝):
      - Tick 只在"全部角色空闲持续 idle_seconds 秒"时快进跳变 (有任务跳任务,
        没任务跳下班/次日上班); 角色忙碌期间 Tick 冻结 — LLM 在 1 Tick 内
        跑完内容, 不会因处理耗时错过未来 Tick 的任务.
      - 系统启动时刻记为 Tick 0 / 第 1 天.

    参数:
        minutes_per_tick: 每个 Tick 的分钟数 (默认 10, 仅用于 tick↔时钟换算)
        shift_start_tick: 上班 Tick (默认 0)
        shift_end_tick:   下班 Tick (默认 60)
        ticks_per_day:    每天总 Tick 数 (默认 144)
        check_interval:   时间线程检查间隔秒数 (默认 30)
    """

    minutes_per_tick: int = MINUTES_PER_TICK
    shift_start_tick: int = SHIFT_START_TICK
    shift_end_tick: int = SHIFT_END_TICK
    ticks_per_day: int = TICKS_PER_DAY
    check_interval: float = DEFAULT_CHECK_INTERVAL  # 支持小数秒 (测试用短间隔加速)

    # 内部状态
    _tick: int = field(default=0, repr=False, init=False)  # 当前绝对 Tick (显式状态, 快进时跳变)
    _thread: Optional[threading.Thread] = field(default=None, repr=False, init=False)
    _running: bool = field(default=False, repr=False, init=False)
    _event_sender: Optional[Callable[[Event], None]] = field(default=None, repr=False, init=False)
    _clock: Callable[[], datetime] = field(default=datetime.now, repr=False, init=False)
    _fired_day: int = field(default=0, repr=False, init=False)      # 已触发事件的天
    _fired_start: bool = field(default=False, repr=False, init=False)
    _fired_end: bool = field(default=False, repr=False, init=False)
    _pending_progress: Optional[tuple[int, int]] = field(default=None, repr=False, init=False)  # 恢复进度 (day, tick_of_day)
    _tasks: dict[str, ScheduledTask] = field(default_factory=dict, repr=False, init=False)  # 定时任务表

    # 快进功能: 全角色空闲时跳过等待, 直接跳到下一个事件 Tick
    _idle_checker: Optional[Callable[[], bool]] = field(default=None, repr=False, init=False)
    _fast_forward: bool = field(default=True, repr=False, init=False)  # 快进开关
    _idle_since: Optional[float] = field(default=None, repr=False, init=False)  # 全空闲起始墙钟时间
    _idle_seconds: float = field(default=FAST_FORWARD_IDLE_SECONDS, repr=False, init=False)

    def __post_init__(self) -> None:
        """dataclass 初始化后: 调用父类 EventBus 的 __init__ 建立过滤管线.

        TimeEventBus 是 dataclass, 其生成的 __init__ 会覆盖 EventBus 手动写的
        __init__, 因此这里显式调用 super().__init__() 恢复 EventBus 的
        buffer / state_getter / relevance_fn / stats / _tick_schedule 等字段.
        """
        EventBus.__init__(self)

    # ── 事件注册 (时间与事件深度绑定) ─────────────────────

    def register_event(self, event: Event, tick: Optional[int] = None) -> str:
        """向事件总线注册一个事件 (TimeEventBus 版).

        tick 语义:
          - tick=None (默认): 立即触发 — 走事件发送回调 (EventDispatcher),
            进入各角色 3 层过滤管线.
          - tick=整数:        在指定绝对 Tick 触发 — 存入调度表,
                              由时间线程到期自动投递 (同样走发送回调).

        参数:
            event: 要注册的 Event.
            tick:  触发 Tick (绝对 Tick, 自系统启动累计). None = 立即.

        返回:
            事件 ID.
        """
        if tick is None:
            self._dispatch(event)
        else:
            event.trigger_tick = tick
            self._tick_schedule[event.id] = (tick, event)
            logger.info("TimeEventBus 注册定时事件: id=%s type=%s → tick %d",
                        event.id, event.event_type, tick)
        return event.id

    def _dispatch(self, event: Event) -> None:
        """把事件交给下游 (事件发送回调 → EventDispatcher)."""
        if self._event_sender is not None:
            try:
                self._event_sender(event)
            except Exception:
                logger.exception("TimeEventBus 事件发送失败: %s", event.event_type)
        else:
            logger.debug("TimeEventBus 无事件发送回调, 事件未投递: %s", event.event_type)

    # ── 配置 ──────────────────────────────────────────────

    def set_event_sender(self, fn: Callable[[Event], None]) -> None:
        """设置事件发送回调 (发送到事件分发器).

        参数:
            fn: 接收 Event 对象的回调函数, 如 EventDispatcher.trigger.
        """
        self._event_sender = fn

    # ── 快进功能 (全角色空闲时跳过等待) ───────────────────

    def set_idle_checker(self, fn: Callable[[], bool]) -> None:
        """设置"全部角色是否空闲"判定回调.

        参数:
            fn: 返回 True 表示所有角色都空闲 (无任务在处理/排队).
        """
        self._idle_checker = fn

    def set_fast_forward(self, enabled: bool, idle_seconds: float = 60.0) -> None:
        """开关快进功能.

        参数:
            enabled:      True = 启用 (默认), False = 禁用.
            idle_seconds: 全角色空闲持续多少秒后快进 (默认 60 = 1 分钟).
        """
        self._fast_forward = enabled
        self._idle_seconds = idle_seconds
        if not enabled:
            self._idle_since = None

    def _check_fast_forward(self) -> None:
        """检查是否满足快进条件并执行跳转 (由时间线程周期性调用).

        条件: 快进开启 + 已注入空闲判定 + 全部角色空闲持续 idle_seconds 秒.
        跳转: 把时钟基准前移, 使 current_tick() 直接到达下一个事件 Tick
        (调度表定时事件 / 定时任务 / 下班 SHIFT_END; 都没有则跳到当天上班后的
        下一个作息边界 — 下班优先).
        """
        if not self._fast_forward or self._idle_checker is None:
            return
        try:
            all_idle = self._idle_checker()
        except Exception:
            logger.exception("快进: 空闲判定回调异常")
            return

        if not all_idle:
            self._idle_since = None  # 有人忙, 重置计时
            return

        if self._idle_since is None:
            self._idle_since = time_module.time()  # 开始计时
            logger.debug("全部角色空闲, 开始快进计时 (%.0fs 后跳转)",
                         self._idle_seconds)
            return

        if time_module.time() - self._idle_since < self._idle_seconds:
            return  # 还没到 1 分钟, 继续等

        # 满足条件: 跳到下一个事件 Tick
        target = self._next_event_tick()
        now = self.current_tick()
        if target is None or target <= now:
            self._idle_since = time_module.time()  # 无目标, 重新计时
            logger.debug("全部角色空闲 %.0fs, 但无下一个事件 Tick, 继续等待",
                         self._idle_seconds)
            return

        # 时钟基准前移: 使 elapsed 恰好等于 target Tick 对应的秒数
        # (Tick 是显式状态 — 直接跳到目标, 不依赖真实时间流逝)
        self._tick = target
        self._idle_since = None
        logger.debug("全部角色已空闲 %.0fs, 快进到下一个事件 Tick %d (原 %d)",
                     self._idle_seconds, target, now)
        logger.info("⚡ 快进: 全部角色空闲 ≥%.0fs, 时钟跳到 Tick %d (原 %d)",
                    self._idle_seconds, target, now)

    def _next_event_tick(self) -> Optional[int]:
        """计算下一个事件触发 Tick (绝对 Tick).

        候选:
          1. 调度表中的定时事件 (register_event(tick=N))
          2. 定时任务 (schedule_task)
          3. 下班 SHIFT_END (当天 shift_end_tick 的绝对位置)
          4. 下一天上班 SHIFT_START (已过当天下班后, 快进到次日上班)

        返回:
            大于当前 Tick 的最小候选绝对 Tick; 没有则返回 None.
        """
        now = self.current_tick()
        day = self.day_number()
        candidates: list[int] = []

        # 1) 调度表定时事件
        for tk, _ in self._tick_schedule.values():
            candidates.append(tk)
        # 2) 定时任务
        for task in self._tasks.values():
            candidates.append(task.absolute_fire_tick(self.ticks_per_day))
        # 3) 当天下班
        candidates.append(
            (day - 1) * self.ticks_per_day + self.shift_end_tick)
        # 4) 已过当天下班 → 下一个事件是次日上班 SHIFT_START
        tod = self.tick_of_day()
        if tod >= self.shift_end_tick:
            candidates.append(day * self.ticks_per_day + self.shift_start_tick)

        future = [c for c in candidates if c > now]
        if not future:
            return None
        return min(future)

    def set_clock(self, fn: Callable[[], datetime]) -> None:
        """设置时间源 (默认 datetime.now). 用于模拟测试.

        参数:
            fn: 返回当前时间的无参函数.
        """
        self._clock = fn

    # ── 核心方法 ──────────────────────────────────────────

    def current_tick(self) -> int:
        """获取当前 Tick 数 (自系统启动累计, 启动即为 0).

        Tick 是显式状态, 不随真实时间流逝自动推进; 仅在全部角色空闲时
        由快进机制跳变到下一个事件 Tick.

        返回:
            当前 Tick 数.
        """
        return self._tick

    def day_number(self) -> int:
        """获取当前是第几天 (系统启动当天为第 1 天).

        返回:
            天序号 (>= 1).
        """
        return self._tick // self.ticks_per_day + 1

    def tick_of_day(self) -> int:
        """获取今天内的 Tick 位置 (0 ~ ticks_per_day-1).

        返回:
            今日内 Tick 数.
        """
        return self._tick % self.ticks_per_day

    def tick_to_time(self, tick: int) -> str:
        """将 Tick 转换为相对时钟 "HH:MM" (从每天第 0 Tick 起算).

        参数:
            tick: Tick 数.

        返回:
            相对时钟字符串, 如 tick 30 → "05:00", tick 60 → "10:00".
        """
        total_minutes = tick * self.minutes_per_tick
        total_minutes %= 24 * 60
        h, m = divmod(total_minutes, 60)
        return f"{h:02d}:{m:02d}"

    def is_working_hours(self) -> bool:
        """判断当前是否在上班时间内 (今日 Tick 在 [shift_start, shift_end) 之间).

        返回:
            True 表示在上班时间内.
        """
        tod = self.tick_of_day()
        return self.shift_start_tick <= tod < self.shift_end_tick

    def ticks_until_shift_end(self) -> int:
        """距下班还有多少 Tick (已下班返回 0)."""
        return max(0, self.shift_end_tick - self.tick_of_day())

    # ── 作息事件 ─────────────────────────────────────────

    def get_shift_event(self, tick_of_day: int) -> Optional[str]:
        """获取今日某个 Tick 位置对应的作息事件.

        参数:
            tick_of_day: 今日内 Tick 位置.

        返回:
            "SHIFT_START" (上班) / "SHIFT_END" (下班) / None (普通时间).
        """
        if tick_of_day == self.shift_start_tick:
            return EVENT_SHIFT_START
        if tick_of_day >= self.shift_end_tick:
            return EVENT_SHIFT_END
        return None

    def describe(self) -> str:
        """返回当前作息状态的文字描述 (供工具/提示词使用).

        返回:
            状态描述字符串: 第几天, Tick 数, 上班/下班状态.
        """
        tick = self.current_tick()
        day = self.day_number()
        tod = self.tick_of_day()
        if tod >= self.shift_end_tick:
            return f"第 {day} 天, Tick {tick} (已下班 {tod - self.shift_end_tick} Ticks)"
        return f"第 {day} 天, Tick {tick} (上班中, 距下班还有 {self.shift_end_tick - tod} Ticks)"

    # ── 时间线程 (独占) ───────────────────────────────────

    def start(self) -> None:
        """启动时间线程 (独占线程, 周期性检查 Tick 并触发作息事件).

        系统启动时刻记为 Tick 0 / 第 1 天:
          - 每天首次检测到今日 Tick == shift_start_tick → 发送 SHIFT_START
          - 每天首次检测到今日 Tick >= shift_end_tick   → 发送 SHIFT_END
        若已 set_progress 设置了恢复进度, 启动时直接应用 (时钟前移 + 事件
        标志按恢复点设置, 不重放已发生的事件).
        """
        if self._running:
            return
        self._tick = 0
        self._running = True
        self._fired_day = 0
        self._fired_start = False
        self._fired_end = False
        # 恢复上次进度 (StateStore): Tick 直接跳到恢复点, 事件标志按
        # 恢复点设置 — 当天上班视为已发生, 下班按 tick 位置决定
        if self._pending_progress is not None:
            day, tod = self._pending_progress
            self._pending_progress = None
            self._tick = (day - 1) * self.ticks_per_day + tod
            self._fired_day = day
            self._fired_start = True   # 恢复点必在上班区间内 (上班已发生)
            self._fired_end = tod >= self.shift_end_tick
            logger.info("TimeEventBus: 已恢复上次进度 → 第 %d 天 Tick %d", day, tod)
        self._thread = threading.Thread(
            target=self._tick_loop, name="time-manager", daemon=True,
        )
        self._thread.start()
        logger.info("TimeEventBus 时间线程已启动 (启动时刻 = Tick 0 / 第 1 天, 检查间隔 %ds)",
                    self.check_interval)

    def set_progress(self, day: int, tick_of_day: int) -> None:
        """设置恢复进度: 下次 start() 时把 Tick 直接设为 (day, tick_of_day).

        用于 StateStore 重启恢复上次进度. 必须在 start() 前调用.

        参数:
            day: 第几天 (>= 1).
            tick_of_day: 当天内 Tick 位置 (0 ~ ticks_per_day-1).
        """
        self._pending_progress = (int(day), int(tick_of_day))

    def stop(self) -> None:
        """停止时间线程."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        logger.info("TimeEventBus 时间线程已停止")

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ── 定时任务管理 ───────────────────────────────────────

    def schedule_task(
        self,
        description: str,
        owner_role: str,
        target_tick: int,
        day: Optional[int] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> ScheduledTask:
        """注册一个定时任务, 到达指定 Tick 时向事件总线发送提醒事件.

        参数:
            description: 任务内容描述
            owner_role:  所属角色 role_id (提醒事件只投递给该角色)
            target_tick: 目标 Tick, 范围 0~60 (今天内)
            day:         触发日期 (默认当天)
            payload:     附加数据 (可选)

        返回:
            创建的 ScheduledTask.

        异常:
            ValueError: target_tick 超出 [0, 60] 范围.
        """
        if not (TASK_TICK_MIN <= target_tick <= TASK_TICK_MAX):
            raise ValueError(
                f"target_tick 必须在 {TASK_TICK_MIN}~{TASK_TICK_MAX} 范围内, 得到 {target_tick}"
            )
        task = ScheduledTask(
            description=description,
            owner_role=owner_role,
            target_tick=target_tick,
            day=day if day is not None else self.day_number(),
            payload=payload or {},
        )
        self._tasks[task.task_id] = task

        # 只保存任务列表; 当天任务直接注册事件, 隔天任务等目标天上班时自动加载
        self._register_task_event_if_today(task)
        logger.info("TimeEventBus: 定时任务已注册 [%s] %s → tick %d (day %d), 所有者 %s",
                    task.task_id, description, target_tick, task.day, owner_role)
        return task

    # ── 任务 ↔ 事件调度表桥接 ─────────────────────────────

    def _register_task_event_if_today(self, task: ScheduledTask) -> bool:
        """当天任务直接注册 TASK_DUE 事件到调度表 (隔天任务不注册).

        注册时机保证"只注册一次":
          - 创建时 (schedule_task): 当天任务立即注册; 隔天任务仅保存.
          - 目标天上班 (_load_today_tasks_to_bus): 只补注册创建时是隔天、
            尚未注册 (event_id 为空) 的任务.

        参数:
            task: ScheduledTask.

        返回:
            True = 已注册 (当天/已过期), False = 隔天, 仅保存.
        """
        if task.day > self.day_number():
            logger.info("TimeEventBus: 任务 [%s] 是隔天任务 (day %d), 仅保存, "
                        "目标天上班时自动加载到事件总线", task.task_id, task.day)
            return False
        if task.event_id and task.event_id in self._tick_schedule:
            # 兜底: 正常流程任务只注册一次 (创建时当天注册 / 隔天到期补注册),
            # 走到这里说明发生了重复注册, 属于运行异常 — 报警不静默
            logger.warning(
                "TimeEventBus: 任务 [%s] 已注册事件 %s, 跳过重复注册 "
                "(正常流程任务只注册一次, 请检查是否重复调用 schedule)",
                task.task_id, task.event_id)
            return True
        eid = self.register_event(
            self._task_to_event(task),
            tick=task.absolute_fire_tick(self.ticks_per_day),
        )
        task.event_id = eid
        return True

    def _task_to_event(self, task: ScheduledTask) -> Event:
        """构造任务的 TASK_DUE 提醒事件."""
        return Event(
            source="task",
            event_type=EVENT_TASK_DUE,
            priority=Priority.NORMAL,
            target_role=task.owner_role,
            payload={
                "task_id": task.task_id,
                "description": task.description,
                "tick": task.target_tick,
                "day": task.day,
                "owner_role": task.owner_role,
                **task.payload,  # 透传附加信息 (笔记提醒: note_title 等)
            },
        )

    def _load_today_tasks_to_bus(self) -> None:
        """目标天上班 (SHIFT_START) 时: 把到期任务加载到事件调度表.

        只加载创建时是隔天、尚未注册事件的任务 (event_id 为空, 现在已到期):
        当天任务在创建时已注册过一次, 这里绝不重复注册 (保证只注册一次).
        """
        today = self.day_number()
        loaded = 0
        for task in list(self._tasks.values()):
            if task.fired:
                continue
            # 只补注册"创建时是隔天、现已到期且从未注册"的任务
            if task.day <= today and not task.event_id:
                if self._register_task_event_if_today(task):
                    loaded += 1
        if loaded:
            logger.info("TimeEventBus: 上班加载 %d 个到期任务到事件总线", loaded)

    def _cancel_task_event(self, task_id: str) -> bool:
        """从事件调度表取消某任务对应的事件 (任务被删除/编辑时)."""
        for eid, (_, ev) in list(self._tick_schedule.items()):
            if ev.payload.get("task_id") == task_id:
                del self._tick_schedule[eid]
                return True
        return False

    def list_tasks(self, owner_role: Optional[str] = None) -> list[ScheduledTask]:
        """列出未触发的定时任务.

        参数:
            owner_role: 只列某角色的任务 (None = 全部).

        返回:
            未触发任务列表 (按触发顺序排序).
        """
        tasks = [t for t in self._tasks.values() if not t.fired]
        if owner_role is not None:
            tasks = [t for t in tasks if t.owner_role == owner_role]
        return sorted(tasks, key=lambda t: t.absolute_fire_tick(self.ticks_per_day))

    def edit_task(
        self,
        task_id: str,
        description: Optional[str] = None,
        target_tick: Optional[int] = None,
        day: Optional[int] = None,
    ) -> Optional[ScheduledTask]:
        """编辑已有定时任务.

        参数:
            task_id:      任务 ID
            description:  新描述 (可选)
            target_tick:  新目标 Tick 0~60 (可选)
            day:          新触发日 (可选)

        返回:
            更新后的任务, 不存在返回 None.

        异常:
            ValueError: target_tick 超出范围.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # 先校验新值, 再应用 (校验失败不产生副作用)
        new_day = task.day if day is None else day
        new_tick = task.target_tick if target_tick is None else target_tick
        if not (TASK_TICK_MIN <= new_tick <= TASK_TICK_MAX):
            raise ValueError(
                f"target_tick 必须在 {TASK_TICK_MIN}~{TASK_TICK_MAX} 范围内, 得到 {new_tick}"
            )
        # 防呆: 不能把任务改到已过去的时间 — 过期绝对 tick 会被立即触发
        # (当前绝对 tick 由派生时钟算出, 改到过去 = 编辑后瞬间到期)
        if (new_day - 1) * self.ticks_per_day + new_tick < self.current_tick():
            raise ValueError(
                f"不能把任务 [ID={task_id}] 改到过去的时间: "
                f"第 {new_day} 天 Tick {new_tick} 已过期 (当前绝对 Tick {self.current_tick()})"
            )
        # 编辑前取消旧的事件注册 (若已注册)
        self._cancel_task_event(task_id)
        if description is not None:
            task.description = description
        task.target_tick = new_tick
        task.day = new_day
        # 编辑后重新注册 (当天任务) — 隔天任务仅保存, 上班时自动加载
        self._register_task_event_if_today(task)
        logger.info("TimeEventBus: 定时任务已编辑 [%s] → tick %d (day %d)", task_id, task.target_tick, task.day)
        return task

    def cancel_task(self, task_id: str) -> bool:
        """删除定时任务 (同时取消已注册的事件).

        参数:
            task_id: 任务 ID.

        返回:
            是否删除成功.
        """
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._cancel_task_event(task_id)
            logger.info("TimeEventBus: 定时任务已删除 [%s]", task_id)
            return True
        return False

    def _tick_loop(self) -> None:
        """时间线程主循环: 周期性检查 Tick, 触发作息事件与到期定时事件.

        任务提醒已统一走事件调度表 (schedule_task 当天注册 / 隔天上班加载),
        不再有独立的任务到期检查.
        """
        logger.debug("TimeEventBus 线程循环开始")
        while self._running:
            try:
                self._check_and_fire()
                # 调度表中的定时事件 (register_event(tick=N)) 到期投递
                for ev in self._check_due_events(self.current_tick()):
                    logger.info("TimeEventBus 定时事件到期投递: id=%s type=%s",
                                ev.id, ev.event_type)
                    # 任务提醒触发后标记 fired, 防止隔天 SHIFT_START 重新注册重复触发
                    if ev.event_type == EVENT_TASK_DUE:
                        tid: Optional[str] = ev.payload.get("task_id")
                        task = self._tasks.get(tid or "")
                        if task is not None:
                            task.fired = True
                    self._dispatch(ev)
                # 快进: 全部角色空闲时跳到下一个事件 Tick
                self._check_fast_forward()
            except Exception:
                logger.exception("TimeEventBus 检查异常")
            time_module.sleep(self.check_interval)
        logger.debug("TimeEventBus 线程循环结束")

    def _check_and_fire(self) -> None:
        """检查当前 Tick 并触发对应事件 (按天重置, 每天各触发一次)."""
        day = self.day_number()
        tod = self.tick_of_day()

        # 新的一天 → 重置当天的事件标志
        if day != self._fired_day:
            self._fired_day = day
            self._fired_start = False
            self._fired_end = False
            logger.info("TimeEventBus: 进入第 %d 天", day)

        # 上班事件 (每天触发一次; 条件用区间而非严格 ==, 避免错过 tick 0 窗口
        # 导致当天 SHIFT_START 永久丢失 — 模拟时钟大步跳/系统挂起恢复时会发生)
        if (not self._fired_start
                and self.shift_start_tick <= tod < self.shift_end_tick):
            self._fired_start = True
            # 上班: 把今天到期的隔天任务加载到事件调度表
            self._load_today_tasks_to_bus()
            self._fire_event(EVENT_SHIFT_START)

        # 下班事件 (每天达到 shift_end_tick 后触发一次)
        if not self._fired_end and tod >= self.shift_end_tick:
            self._fired_end = True
            self._fire_event(EVENT_SHIFT_END)

    def _fire_event(self, event_type: str) -> None:
        """构造并发送作息事件到事件总线.

        参数:
            event_type: "SHIFT_START" 或 "SHIFT_END"
        """
        tick = self.current_tick()
        day = self.day_number()
        tod = self.tick_of_day()

        # 下班事件附带指示: 角色应调用 summary 工具总结并进入 OFF_DUTY
        if event_type == EVENT_SHIFT_END:
            instruction = (
                "下班时间到: 请调用 summary 工具总结今天的工作, "
                "总结完成后你将自动进入 OFF_DUTY 状态."
            )
        else:
            instruction = "上班时间到: 查看昨日总结, 开始今天的工作."

        event = Event(
            source="time",
            event_type=event_type,
            priority=Priority.EMERGENCY,  # 作息事件必须能穿透所有过滤
            payload={
                "tick": tick,
                "day": day,
                "time": self.tick_to_time(tod),
                "shift": event_type,
                "instruction": instruction,
            },
        )
        logger.info("TimeEventBus 触发事件: %s (day=%d, tick=%d, time=%s, priority=%s)",
                    event_type, day, tick, event.payload["time"], event.priority.name)

        self._dispatch(event)
