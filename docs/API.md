# Shift & Event-Driven Agent Scheduler 接口文档

**多角色、事件驱动、Tick 作息制的 Agent 调度框架**（Shift & Event-Driven Agent Scheduler）。
本文档覆盖全部核心类与工具类的接口、用途、参数与使用示例。

---

## 目录

1. [架构总览](#架构总览)
2. [核心类型 types.py](#核心类型)
3. [时间系统 time_manager.py](#时间系统)
4. [存储 NoteStore](#存储)
5. [角色系统 roles.py](#角色系统)
6. [事件系统 EventBus / EventDispatcher](#事件系统)
7. [系统管理 AgentSystem](#系统管理)
8. [角色模板与工厂](#角色模板与工厂)
9. [工具系统 tools.py](#工具系统)
10. [Python 工具类 python_tools/](#python-工具类)
11. [MCP 加载器 mcp_toolkit.py](#mcp-加载器)
12. [LLM 客户端 llm.py](#llm-客户端)
13. [完整示例](#完整示例)

---

## 架构总览

```
┌─────────────────────────────────────────────┐
│ AgentSystem (统一入口)                       │
│  ├── TimeEventBus   (独占线程, Tick 制)      │
│  ├── RolePool         (每角色独立线程)       │
│  └── EventDispatcher  (事件广播/定向投递)    │
└──────────────┬──────────────────────────────┘
               ▼
  ┌─────────────────────────┐
  │ Event (target_role?)    │
  └────────────┬────────────┘
               ▼
  每个角色 AgentRole: Layer1 状态掩码 → Layer2 显著性 → 转 Task 入队
               ▼
  角色 worker 线程: LLM + 工具循环 (ToolRegistry)
               ▼
  ToolKit 集合: talk / hr / memory / time / task / client / MCP 分组
```

---

## 核心类型

文件: `src/core/types.py`

### `Priority(IntEnum)`
事件优先级，数值越高越紧急。

| 成员 | 值 | 用途 |
|------|-----|------|
| `LOW` | 1 | 闲聊、无关信息 |
| `NORMAL` | 3 | 常规任务、作息事件 |
| `HIGH` | 6 | 工作工单 |
| `EMERGENCY` | 10 | 紧急事件（穿透所有过滤） |

### `AgentState(str, Enum)`
角色生命周期状态。

| 成员 | 含义 |
|------|------|
| `OFF_DUTY` | 下班。非 EMERGENCY 事件被 Layer 1 拦截 |
| `ON_DUTY_IDLE` | 上班空闲（默认） |
| `ON_DUTY_BUSY` | 上班忙碌（执行任务中） |
| `WRAPPING_UP` | 收尾中（预留） |

### `Event`（dataclass）
```python
Event(
    id: str = 自动生成,          # 事件 ID
    source: str = "",            # 来源: github/slack/time/task...
    event_type: str = "",        # new_pr/SHIFT_START/TASK_DUE...
    priority: Priority = NORMAL,
    payload: dict = {},          # 载荷
    timestamp: datetime = now,
    target_role: Optional[str] = None,  # 定向投递: 只发给该角色; None=广播
    trigger_tick: Optional[int] = None, # 触发绝对 Tick (None=立即, 由 TimeEventBus 设置)
)
```

---

## 时间系统

文件: `src/core/time_manager.py`

### `ScheduledTask`（dataclass）
定时任务，注册到 TimeEventBus。

| 字段 | 说明 |
|------|------|
| `description` | 任务内容 |
| `owner_role` | 所属角色（提醒只投递给它） |
| `target_tick` | 目标 Tick，范围 **0~60** |
| `day` | 第几天触发（默认当天） |
| `task_id` | 自动生成 |
| `fired` | 是否已触发 |

方法: `absolute_fire_tick(ticks_per_day)` → `(day-1)*144 + target_tick`

### `TimeEventBus`
作息时间管理器（= 事件总线子类，`EventBus` + 时间线程），**独占一个后台线程**。
（旧名 `TimeManager` 兼容别名已于 2026-08 移除，统一用 `TimeEventBus`。）

**常量**: `MINUTES_PER_TICK=10`、`TICKS_PER_DAY=144`、`SHIFT_START_TICK=0`、`SHIFT_END_TICK=60`、`EVENT_SHIFT_START/SHIFT_END/TASK_DUE`、`TASK_TICK_MIN/MAX=0/60`

**构造**:
```python
TimeEventBus(minutes_per_tick=10, shift_start_tick=0, shift_end_tick=60,
             ticks_per_day=144, check_interval=30)
```

**配置**:
| 方法 | 参数 | 用途 |
|------|------|------|
| `set_event_sender(fn)` | `fn(Event)` | 设置事件发送回调（接入事件总线） |
| `set_clock(fn)` | `fn() -> datetime` | 注入时间源（默认 `datetime.now`，测试用模拟时钟） |

**时间查询**:
| 方法 | 返回 | 说明 |
|------|------|------|
| `current_tick()` | int | 自系统启动累计 Tick（启动=0） |
| `day_number()` | int | 第几天（启动当天=1） |
| `tick_of_day()` | int | 今日内 Tick 位置 0~143 |
| `tick_to_time(tick)` | str | Tick → 相对时钟 "HH:MM" |
| `is_working_hours()` | bool | 今日是否在上班区间 |
| `ticks_until_shift_end()` | int | 距下班还有多少 Tick |
| `describe()` | str | "第 X 天, Tick Y (上班中/已下班)" |
| `get_shift_event(tick_of_day)` | str\|None | SHIFT_START / SHIFT_END / None |

**定时任务**:
| 方法 | 参数 | 返回 |
|------|------|------|
| `schedule_task(description, owner_role, target_tick, day=None, payload=None)` | — | `ScheduledTask`，tick 越界抛 `ValueError` |
| `list_tasks(owner_role=None)` | 按触发顺序 | `list[ScheduledTask]` |
| `edit_task(task_id, description=None, target_tick=None, day=None)` | — | `Optional[ScheduledTask]` |
| `cancel_task(task_id)` | — | bool |

**生命周期**: `start()` 启动线程（Tick 0 / 第 1 天）、`stop()` 停止、`is_running` 属性。
Tick 为显式状态：不随真实时间流逝，仅在全部角色空闲时快进跳变（`set_fast_forward(enabled, idle_seconds)` 可调）。

**自动事件**: 每天第 0 Tick → `SHIFT_START`（EMERGENCY）；每天 ≥60 Tick → `SHIFT_END`（EMERGENCY，instruction 提示调 summary）；到期任务 → `TASK_DUE`（NORMAL，`target_role=owner`）。

```python
from src.core.time_manager import TimeEventBus
tm = TimeEventBus(check_interval=1)
tm.set_event_sender(lambda ev: dispatcher.trigger(ev))  # 接入总线
tm.start()
print(tm.describe())          # 第 1 天, Tick 0 (上班中...)
task = tm.schedule_task("写周报", owner_role="CEO", target_tick=45)
tm.cancel_task(task.task_id)
tm.stop()
```

---

## 存储

### `NoteStore` — 笔记 (含提醒 = 定时任务统一) 与总结
文件: `src/core/note_store.py`。每角色独立目录（有个人电脑时在电脑工作目录
`<workdir>/notes/`，否则本地 `data/notes/<role_id>/`）。

**笔记与定时任务已统一**: 笔记 = 内容 + 可选提醒时间。`write_note` 填入
`remind_tick` 后, 到点系统像任务一样向该角色发送提醒事件 (TASK_DUE)。

```python
NoteStore(base_dir="./data/notes", role_id="", computer=None, time_manager=None)
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `write_note(title, content, remind_tick=None, remind_day=None)` | str,str,int\|None,int\|None | str(路径) | 写笔记（存在则覆盖；`remind_tick` 填了则注册提醒，到点发事件） |
| `edit_note(title, content, remind_tick=None, remind_day=None)` | str,str,int\|None,int\|None | str(路径) | 编辑笔记（提供 `remind_tick` 则重置提醒，否则保留原提醒） |
| `list_notes()` | — | list[str] | 笔记标题列表（不含总结） |
| `read_note(title)` | str | Optional[str] | 读笔记，不存在返回 None |
| `delete_note(title)` | str | bool | 删笔记（真实删除文件 + 取消关联提醒） |
| `get_reminder(title)` | str | Optional[dict] | 查询笔记提醒 `{"day", "tick"}`，无提醒返回 None |
| `save_summary(content, day=None)` | str,int\|None | str(路径) | 保存第 N 天总结 `_summary_day_N.md` |
| `get_summary(day=None)` | int\|None | Optional[str] | 读指定天总结 |
| `get_latest_summary(before_day=None)` | int\|None | Optional[str] | 最近一次总结（严格早于 before_day） |

---

## 角色系统

文件: `src/core/roles.py`

### `Urgency(IntEnum)`
`LOW=1 / NORMAL=3 / HIGH=6 / CRITICAL=8`（任务队列按紧急度排序）。

### `Task`（dataclass）
`Task(description, urgency=Urgency.NORMAL, source="", payload={})`
字段: `task_id`（自动）、`status`（pending/running/done/failed）、`result`、`tokens_consumed`、`assigned_role`。

### `AgentRole`（dataclass）
角色定义 + 任务队列 + LLM 绑定。

**角色属性**:
| 字段 | 说明 |
|------|------|
| `name` | 人物姓名（张三/李四…） |
| `role_id` | 职能职位（coder/reviewer/ceo…） |
| `title` | 职位名称 |
| `responsibilities` | 职责描述 |
| `personality` | 性格 |
| `skills` | 技能列表 |
| `is_default` | 是否默认角色 |
| `state` | AgentState（默认 ON_DUTY_IDLE） |
| `interest_keywords` | 事件过滤关键词 |

**事件过滤**: `evaluate_event(event) -> (bool, reason)` 三层过滤；`event_to_task(event) -> Task`。
**提示词**: `build_system_prompt() -> str`（自动注入"今天是第 X 天" + 昨日总结）。
**队列**: `add_task(task)` / `pop_task()` / `peek_next_urgency()` / `queue_depth` / `current_task` / `is_busy`。
**存储与时间**: `note_store` 属性（惰性 NoteStore）、`get_latest_summary(before_day=None)`、`time_manager` 属性、`bind_time_manager(tm)`。
**活动日志**: `journal(entry)` — 追加一行到 `data/journals/<role_id>.md`（角色上下文更新自动写入：收到任务/执行/工具调用/笔记/消息）。
**工具**: `add_mcp_tool(name, description, input_schema, handler)`、`add_toolkit(toolkit) -> int`、`mcp_tool_names`。
**交流**: `talk_to(target, message, urgency="NORMAL") -> str`（编程式跨角色消息）; LLM 经 talk 工具（target/message/urgency/wait/attachment）通信, **target 用成员人名**（花名册不暴露 role_id, 内部自动映射）; `wait=true` 时发送方进入 WAIT 状态同步等待对方回复（消息附带"提问者正在等待"提示, 等待无时间限制, 互等死锁自动检测拒绝）; `attachment` 为公司云盘文件路径（/mnt/drive 下, 发送前校验存在且可读, 对方可直接读取）。

### `RolePool`
多角色并发管理，**每角色独立线程 + 独立锁 + 独立 LLM 客户端**（默认 DeepSeekLLM，`llm_provider="ollama"` 时用本地 OllamaLLM）。

```python
RolePool(llm_api_key=None, llm_model=None, llm_provider=None)  # llm_provider 默认读环境变量 LLM_PROVIDER
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `add_role(role)` | AgentRole | None | 注册（须在 start 前） |
| `get_role(name)` | str | AgentRole | 不存在抛 KeyError |
| `all_roles()` | — | list[AgentRole] | 全部角色 |
| `list_roles()` | — | list[str] | role_id 列表 |
| `start()` | — | None | 启动全部 worker 线程 + 自动注册 talk 工具 |
| `shutdown(wait=True)` | bool | None | 停止 |
| `assign_task(role_name, task)` | str, Task | None | 投递任务 |
| `journal_all(entry)` | str | None | 全局通知，写入每个角色的活动日志 |
| `get_status()` | — | dict | {role_id: {busy, queue_depth, current_task, next_urgency}} |

```python
from src.core.roles import AgentRole, RolePool, Task, Urgency
pool = RolePool()
coder = AgentRole(name="张三", role_id="coder", title="后端工程师",
                  personality="严谨", skills=["Python"])
pool.add_role(coder)
pool.start()
pool.assign_task("coder", Task(description="修复登录bug", urgency=Urgency.HIGH))
print(pool.get_status())
pool.shutdown()
```

---

## 事件系统

### `EventBus` — 定时事件调度表
文件: `src/core/event_bus.py`

只承担"定时事件注册 / 取消 / 到期取出"的调度表职责；**不含过滤管线**
（3 层过滤是每角色独立的 `AgentRole.evaluate_event`，见角色系统章）。

```python
EventBus()   # TimeEventBus 是其子类 (time_manager.py), 提供时间线程
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `register_event(event, tick)` | Event, int | str(事件ID) | 注册定时事件到调度表（tick 必须显式；立即触发用 TimeEventBus） |
| `cancel_event(event_id)` | str | bool | 取消定时事件 |
| `list_scheduled_events()` | — | list[dict] | 待触发事件（按 tick 排序） |
| `_check_due_events(current_tick)` | int | list[Event] | 取出到期事件（时间线程调用） |

### `EventDispatcher` — 多角色广播/定向分发
文件: `src/core/dispatcher.py`

```python
EventDispatcher(pool: RolePool)
```

`trigger(event) -> dict[role_id, {"accepted", "reason", "task_id"}]`
- **广播**（`target_role=None`）：所有角色各自跑 3 层过滤
- **定向**（`target_role=xxx`）：只投递给目标角色（直接接受），其余跳过

```python
dispatcher = EventDispatcher(pool)
results = dispatcher.trigger(Event(source="github", event_type="new_pr",
                                   priority=Priority.HIGH, payload={}))
# {"coder": {"accepted": True, "task_id": "..."}, ...}
```

---

## 系统管理

文件: `src/core/agent_system.py` — **推荐统一入口**

```python
AgentSystem(roles=None, role_ids=None, check_interval=30, auto_toolkits=True)
```
- `roles`：预构建 AgentRole 列表；`role_ids`：模板 id 列表
- `auto_toolkits=True`：自动注册 memory/time/task 工具 + 绑定共享 TimeEventBus

| 方法/属性 | 说明 |
|-----------|------|
| `add_role(role)` | 注册角色（绑定共享时间源 + 自动工具） |
| `add_default_roles()` | 注册 4 个默认管理角色，返回列表 |
| `get_role(role_id)` | 获取角色 |
| `get_status()` | 状态快照 |
| `trigger(event)` | 投递事件（广播/定向） |
| `assign_task(role_id, task)` | 直接分配任务 |
| `start()` | 启动角色池 + 时间线程（Tick 0 / 第 1 天） |
| `stop()` | 停止一切 |
| `tick` / `day` | 当前 Tick / 第几天 |
| `describe()` | 作息描述 |
| `pool` / `time_manager` / `dispatcher` | 底层组件访问 |

```python
from src.core.agent_system import AgentSystem
system = AgentSystem(role_ids=["ceo", "coo", "hr", "cfo"], check_interval=1)
system.start()
print(system.describe())   # 第 1 天, Tick 0
system.trigger(Event(...))
system.stop()
```

---

## 角色模板与工厂

文件: `src/core/role_templates.py`

- `TEMPLATES`：12 个模板（4 管理 + 8 业务）
- `DEFAULT_ROLES = ["CEO", "COO", "HR"]`（CFO 模板保留但暂不默认启用，后续再加）
- `get_template(name) -> AgentRole`：克隆模板
- `create_all_roles() -> list[AgentRole]`：全部 12 个
- `create_default_roles() -> list[AgentRole]`：默认角色（当前为 CEO/COO/HR，CFO 后续再加）

文件: `src/core/role_factory.py`

```python
RoleFactory(llm=None)
factory.create_role(requirement: str) -> AgentRole
# 用人需求 → LLM 生成角色配置(role_id/title/responsibilities/...) → 建角色
```
`create_role` 会从姓名池分配未使用的人名，并注册到角色模板池。

---

## 工具系统

文件: `src/core/tools.py`

### `ToolDef`（dataclass）
`{name, description, input_schema, handler, source("python"/"mcp:包名"), mcp_tool}`

### `ToolKit` — 工具类（一组相关工具）
```python
ToolKit(name, description="")
tk.add_python_tool(name, description, input_schema, handler) -> ToolDef
tk.tool_names / tk.tool_count / tk.get_tool(name) / __iter__ / __contains__
```
内置: `create_coding_toolkit()`（read_file/edit_file/run_cmd）、`create_web_toolkit()`（http_get/http_post）。

### `ToolRegistry` — 角色级工具注册表
| 方法 | 参数 | 返回 |
|------|------|------|
| `add_toolkit(toolkit)` | ToolKit | int（新增数，重名跳过） |
| `remove_toolkit(name)` | str | int |
| `add_tool(...)` / `remove_tool(name)` | — | — |
| `list_tools()` | — | list[dict]（name/description/input_schema） |
| `call_tool(name, arguments)` | str, dict | CallToolResult |
| `get_tools_prompt()` | — | str（LLM 可读的工具说明） |
| `tool_names` / `toolkit_names` / `tool_count` | — | — |

---

## Python 工具类

目录: `src/python_tools/`。角色通过 `role.add_toolkit(create_xxx_toolkit())` 导入，自动绑定。

| 工具类 | 工具 | 用途 |
|--------|------|------|
| `talk_toolkit` | `talk(target, message, urgency)` | 角色间异步通信（投递到对方队列） |
| `hr_toolkit` | `post_job_posting(requirement)` / `list_candidates()` | 发布招聘启事 / 列出候选人（后台完成新角色创建与入职登记） |
| `memory_toolkit` | `summary(content, day)` / `write_note(title, content, remind_tick, remind_day)` / `edit_note` / `list_notes` / `read_note` | 每日总结（保存后切 OFF_DUTY）+ 笔记（**带 remind_tick = 定时提醒**，已合并原定时任务工具） |
| `todo_toolkit` | `todo_add(title, detail)` / `todo_list(status?)` / `todo_update(todo_id, status)` / `todo_delete(todo_id)` | Todo 清单（个人待办, id+状态 pending/in_progress/completed, 持久化 data/todos/<role_id>.json） |
| `task_view_toolkit` | `my_tasks(scope?)` | 任务列表（待处理队列 + 最近完成/失败历史, 只读视图） |
| `hermes_toolkit` | `hermes_new_conversation()` / `hermes_send(conversation_id, content)` | 调用电脑上安装的 Hermes Agent：新建对话返回对话 id, 发送对话同步等待 Hermes 跑完返回全部结果 |
| `time_toolkit` | `get_time()` / `take_rest()` | 查看作息时间 / 休息（无参数, 进入 ON_DUTY_IDLE, 事件自动唤醒） |
| `mcp_manager` | `mcp_search(keyword)` / `mcp_list()` / `mcp_add(tool_name)` / `mcp_remove(tool_name)` / `mcp_my_tools()` | MCP 工具自助管理（搜索/添加/移除本地 MCP 工具，角色自动装配） |
| `client_toolkit` | `talk_to_client(message)` | 与甲方实时交流（阻塞等待用户输入） |

```python
ceo = system.get_role("ceo")
ceo.add_toolkit(create_client_toolkit())          # 甲方工具
ceo.add_toolkit(create_memory_toolkit())          # 总结/笔记 (add_toolkit 自动绑定)
```

---

## MCP 加载器

文件: `src/python_tools/mcp_toolkit.py`。**不自动安装**——用户用 npx 准备服务器，配置只写包名。

### `MCPServer(package, args=None)`
单个服务器连接。`connect()` / `close()` / `list_tools()` / `call_tool(name, args)`。

### `MCPToolLoader(rules_file=None, server_args=None)`
| 方法 | 说明 |
|------|------|
| `load() -> dict[str, ToolKit]` | 连接所有服务器 → 拉工具 → 按规则分组 |
| `list_loaded_tools()` | 已加载工具明细 |
| `close()` | 关闭全部连接 |

### 配置 `src/config/mcp_group_rules.json`
```json
{
  "servers": ["@modelcontextprotocol/server-memory"],
  "groups": [{"name": "memory_ops", "match": ["*entity*", "create_relations"]}],
  "default_group": "default"
}
```

```python
from src.python_tools.mcp_toolkit import MCPToolLoader, load_mcp_toolkits
toolkits = load_mcp_toolkits()                    # 一键加载
dev.add_toolkit(toolkits["file_ops"])             # 导入某组
# 或管理生命周期:
loader = MCPToolLoader()
toolkits = loader.load()
loader.close()
```

## LLM 客户端

文件: `src/core/llm.py`

```python
DeepSeekLLM(api_key=None, model=None)   # 默认从环境变量 DEEPSEEK_API_KEY / DEEPSEEK_MODEL
OllamaLLM(model=None)                   # 本地 Ollama, 默认 gemma4-16k:latest, 免 API Key
```

| 方法 | 参数 | 返回 |
|------|------|------|
| `chat(system, user, max_tokens=512)` | str, str, int | `(text, tokens)` |
| `summarize(text)` | str | `(summary, tokens)` |
| `chat_with_tools(messages, tools)` | list, list | `(content, tool_calls, usage)` |

环境变量: `DEEPSEEK_API_KEY`（必填）、`DEEPSEEK_MODEL`（默认 deepseek-v4-flash）、`DEEPSEEK_THINKING`（默认 true，思考模式）;
`LLM_PROVIDER`（deepseek/ollama，切换后端）、`OLLAMA_BASE_URL`（默认 http://localhost:11434）、`OLLAMA_MODEL`（默认 gemma4-16k:latest）。

---

## 完整示例

### 最小多角色系统
```python
import os
os.environ["DEEPSEEK_API_KEY"] = "sk-xxx"

from src.core.agent_system import AgentSystem
from src.core.types import Event, Priority

system = AgentSystem(role_ids=["ceo", "coo", "hr", "cfo"])
system.start()                                   # Tick 0 / 第 1 天, SHIFT_START 全员上班

system.trigger(Event(source="github", event_type="new_pr",
                     priority=Priority.HIGH,
                     payload={"pr_number": 188, "title": "fix: NPE"}))

# 定向投递给 CEO
system.trigger(Event(source="client", event_type="requirements",
                     priority=Priority.HIGH, target_role="ceo",
                     payload={"instruction": "收集需求"}))

# CEO 笔记提醒: 第 2 天 Tick 30 (笔记与定时任务统一)
system.get_role("ceo").note_store.write_note(
    "开始写周报", "周报模板与本周工作小结", remind_tick=30, remind_day=2)

print(system.describe(), system.get_status())
system.stop()
```

### 添加自定义 Python 工具
```python
from src.core.tools import ToolKit

tk = ToolKit("my_tools", "自定义工具")
def _ping(args):
    return "pong"
tk.add_python_tool("ping", "测试工具", {"type": "object", "properties": {}}, _ping)

role = system.get_role("coo")
role.add_toolkit(tk)     # 或 role.add_mcp_tool(...) 单个注册
```

### 加载 MCP 工具
```bash
npx -y @modelcontextprotocol/server-memory    # 用户先准备服务器
```
```python
from src.python_tools.mcp_toolkit import load_mcp_toolkits
toolkits = load_mcp_toolkits()
system.get_role("coo").add_toolkit(toolkits.get("memory_ops"))
```

### 运行完整演示
```bash
cd maf_scheduler && source .venv/bin/activate
DEEPSEEK_API_KEY=sk-xxx python -m src.main        # 多日循环演示
DEEPSEEK_API_KEY=sk-xxx python -m src.role_demo   # 角色并发演示
DEEPSEEK_API_KEY=sk-xxx python -m src.talk_demo   # 角色通信演示
```
