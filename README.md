# Shift & Event-Driven Agent Scheduler

> 项目还在开发中，按照 DeepSeek 的说法运行起来是没有问题的。但没有进行广泛测试。
> 项目基于 Hermes + DeepSeek 开发，会优先适配 DeepSeek。现阶段还在搭框架与测试的早期阶段，后续会逐步建立并完善各模块。
> 下面的部分是 Hermes 写的。其中带引用部分是作者补充的

基于**企业作息与事件驱动**理念的多角色 AI Agent 调度框架。

打破传统 Agent `while(true)` 循环，解决"长任务 Context 爆炸、状态不可恢复、Token 成本失控、权限无隔离"问题。

每个角色拥有**一台属于自己的个人电脑**（Podman 容器，同一自定义桥接网络内互通），
文件、任务、笔记、MCP 工具全部落在自己电脑上，权限天然隔离。

---

## 架构概览

```
┌──────────────────────────────────────────────────────────┐
│        TimeEventBus (时间 × 事件 深度绑定)                │
│   (2026-08 起 TimeManager 已并入 EventBus, 统一此名)      │
│   - 时钟/Tick/天 (1 Tick = 10 分钟换算, 上班 0~60 Tick)    │
│   - 3 层过滤管线 (状态掩码 → 显著性 → 唤醒)               │
│   - 事件调度表: register_event(ev, tick) 定时触发          │
│   - Tick 事件驱动: 全角色空闲才快进 (有任务跳任务, 没任务跳下班/次日上班) │
└──────────────┬───────────────────────────────────────────┘
               │  SHIFT_START/SHIFT_END/TASK_DUE 事件
               ▼
┌──────────────────────────────────────────────────────────┐
│              EventDispatcher (事件分发器)                  │
│   trigger(event) → fan-out to ALL roles                  │
│   Each role runs Layer 1-2-3 filter independently        │
└──────────────┬───────────────────────────────────────────┘
               │  PASS events become Tasks
               ▼
┌──────────────────────────────────────────────────────────┐
│              RolePool (角色线程池)                         │
│   ThreadPoolExecutor — 每个角色独立线程                   │
│   Priority Queue (heapq) — CRITICAL > HIGH > NORMAL      │
└──────────────┬───────────────────────────────────────────┘
               │  Task execution (原生 function calling)
               ▼
┌──────────────────────────────────────────────────────────┐
│      AgentRole + 个人电脑 + MCP 工具 + talk               │
│   LLM(Task) → tool_calls → execute → role:tool 回喂       │
│   - 个人电脑: podman 容器 maf-<role> (上班开机/下班关机)        │
│     容器内用户名 = 员工名字拼音 (guoxiaodong), 每员工独立 uid   │
│   - 企业云盘: 共享文件夹挂载 /mnt/drive (Public 777 + 员工目录 755) │
│   talk: inter-role communication                          │
└──────────────────────────────────────────────────────────┘
```

---

## 核心功能

### 1. TimeEventBus — 时间与事件深度绑定 (`src/core/time_manager.py`)

`TimeManager` 已合并进 `EventBus`（`TimeEventBus(EventBus)` 子类），既是时间源又是事件总线：

- **统一注册入口** `register_event(event, tick=None)`：
  - `tick=None` → 立即投递（进 3 层过滤管线）
  - `tick=N` → 存入事件调度表，时间线程到点自动投递
- 作息事件自动触发：每天 Tick 0 → `SHIFT_START`（上班），Tick 60 → `SHIFT_END`（下班）
- **Tick 事件驱动（不随真实时间流逝）**：角色忙碌期间 Tick 冻结（LLM 在 1 Tick 内跑完内容，不会因处理耗时错过未来 Tick 的任务）；全部角色空闲持续 60s 才快进——有任务跳任务 Tick，没任务跳当天下班，已下班跳次日上班
- 笔记与定时任务统一（统称笔记）：`write_note` 填 `remind_tick` = 带提醒的笔记，到点像任务一样发送提醒事件；底层 `schedule_task` 只保存任务列表；当天任务直接注册事件，隔天任务目标天上班时自动加载
- 兼容别名 `TimeManager` 已于 2026-08 移除（commit `2953835`）——统一使用 `TimeEventBus`

### 2. 事件 3 层过滤 (`src/core/roles.py` — `AgentRole.evaluate_event`)

0 Token 消耗拦截低价值事件。**过滤是每角色独立的**（角色差异化：各自的
`interest_keywords`/`skills`/状态），由 `EventDispatcher.trigger` 分发时调用；
`EventBus` 只做定时事件调度表，不含过滤管线（2026-08 收敛）。

| 层 | 名称 | 机制 | Token |
|----|------|------|-------|
| Layer 1 | State Mask | OFF_DUTY 状态拦截非 EMERGENCY 事件 | 0 |
| Layer 2 | Salience Evaluator | 角色关键词命中 + 优先级加权（`priority*0.4 + relevance*0.6`） | 0 |
| Layer 3 | Wake | 通过前两层的事件转 Task 入该角色队列 | 按需 |

系统时间事件（`source="time"`，如 SHIFT_START/END）绕过 Layer 2 直接通过。

> 这部分后续可能会训练一个小模型来完成过滤，目前训练貌似没什么价值。

### 3. 快进功能 (`src/core/time_manager.py`)

真实时间模式下不必干等：全部角色空闲（无任务处理/排队）持续 **1 分钟**后，
时钟自动跳到下一个事件 Tick（定时事件 / 定时任务 / 下班；已下班则跳到次日上班）。
`set_idle_checker` / `set_fast_forward(enabled, idle_seconds)` 可配。

### 4. 多角色并发任务调度 (`src/core/roles.py`)

- 每个角色独立线程 + 独立锁 + 独立 LLM 实例
- 优先级任务队列（heapq）：CRITICAL(10) > HIGH(6) > NORMAL(3) > LOW(1)
- `RolePool.add_role_and_start()` 动态入职（HR 招聘即上岗）
- `RolePool.remove_role()` 离职（自动关机 + 移出团队）

### 5. 原生 function calling (`src/core/llm.py` + `src/core/roles.py`)

- 请求带 `tools` 声明 + `tool_choice:"auto"`，判定靠响应 `message.tool_calls` 结构化字段
- 工具结果以 `role:"tool"` + `tool_call_id` 回喂
- 循环有保护上限：最多 20 轮工具调用 / 单任务累计 200K tokens，超限任务标记 failed
  （防止 LLM 陷入反复调工具的退化循环无限烧 Token）；API 超时/错误文本
  （`[API timeout]` / `[API error: ...]`）同样判失败，不当成功结果
- `max_tokens` 默认无上限（长内容 JSON 不被截断成非法 JSON；`None` 时不传该字段）
- 文本协议（```tool_call 块 + `_parse_tool_calls` 正则）已随 commit `2953835` 删除，只有原生 function calling

### 6. 个人电脑体系 (`src/core/computer.py`)

每个角色一台独立电脑，默认 **Podman 容器**（镜像 `node:22-alpine`，名 `maf-<role_id>`）：

- 容器挂载宿主机目录 `data/computers/<role>` ↔ 容器内 `/home/agent`（同一份文件，双向可见）
- **上班自动开机**（SHIFT_START）、**下班自动关机**（summary 总结后）
- 同一自定义桥接网络 `maf-net`：电脑间可互相通信（`lan_devices` 工具查人名/电脑名/IP）
- `ComputerManager`（全局单例）管理分配/销毁
- **Podman 是硬要求**：本机无 podman 时 `PodmanComputer` 构造直接抛 `RuntimeError`
  （commit `2953835` 移除了自动降级）——需要本地模拟请显式设 `computer_kind="local"`
- 另有 `SSHComputer`（远程主机，需显式指定 host）
- **跨天自动重连 MCP 服务器**：每天下班 `podman stop` 会杀死容器内 MCP 服务器的
  stdio 管道，次日上班开机时自动探测会话存活并重建（否则第 2 天起文件工具全失效）

### 7. MCP 工具 — 服务器跑在角色电脑容器内

- **每台电脑一个独立 MCP filesystem 服务器**，通过 `podman exec -i` 在容器内启动，
  授权目录 = `/home/agent`（与 LLM 看到的工作目录字面一致，无路径空间错位）
- 容器创建时自动 `npm install -g @modelcontextprotocol/server-filesystem` 预装
- `DEFAULT_MCP_GROUPS = ("file_ops",)`：角色加入/新入职时自动把文件操作工具装到个人电脑
- `MCPManager`（全局共享）：`mcp_search/mcp_list/mcp_add/mcp_remove/mcp_my_tools`
  供 LLM 自助安装其它工具组的工具

### 8. 默认工具（`src/python_tools/`）

| 工具类 | 工具 | 说明 |
|--------|------|------|
| memory | summary / write_note / edit_note / list_notes / read_note | 记忆 + 下班总结（自动关机） |
| time | get_time / take_rest | 作息 |
| task | create_task / list_tasks / edit_task / delete_task | 定时任务（Tick 提醒，持久化到电脑 tasks/） |
| computer | run_command / computer_status / lan_devices / reboot | 个人电脑操作 |
| mcp_manager | mcp_search / mcp_list / mcp_add / mcp_remove / mcp_my_tools | MCP 自助管理 |
| talk / list_roles | 角色间通信 | pool.start() 自动注入 |
| MCP file_ops | read_file / write_file / edit_file / ... | 默认 MCP 组，自动安装到电脑 |

专属工具：CEO 有 `talk_to_client`（甲方交流），HR 有 `post_job_posting` / `list_candidates`。

### 9. 招聘即入职 (`src/core/hr_toolkit.py` + `src/core/roles.py`)

HR 发布招聘 → 后台 `RoleFactory` 生成新人 → **立即加入运行中团队并启动 worker**
（`add_role_and_start`）→ 新人自动获得全部默认工具 + MCP file_ops + 独立电脑。
无面试环节；入职后 HR 通知 COO。

### 10. 昨日记忆注入 (`src/core/note_store.py` + `src/core/roles.py`)

- 每天下班调 `summary` 保存当日总结（`_summary_day_<N>.md`，落在角色电脑 notes/）
- 第二天 `build_system_prompt()` 自动注入 `[昨日总结]`（严格早于今天的最近一篇）
- 前提是电脑已开机 —— SHIFT_START 上班自动开机保证了这点

> 记忆系统依赖天循环：作为人来说，昨天上班的碎片化记忆就是今天的记忆。所以每天都会有总结。
> 同时，记忆还包括笔记、任务和工作区的文件等。这些是外部记忆，如果不主动翻是不知道的。

### 11. 12 个预定义角色模板 (`src/core/role_templates.py`)

**管理层（默认角色）**：

| 角色 | 姓名 | 职责 |
|------|------|------|
| CEO | 林总 | 接收用户需求→战略目标→汇总报告 |
| COO | 陈总 | 拆解目标→盘点员工→发起招聘 |
| HR | 王人事 | 招聘申请→发布招聘→新人即入职（无面试） |
| CFO | 钱财 | 预算批复→Token 限额→高风险审批（模板保留，暂不启用） |

**技术团队**：architect(王建国) / fullstack_dev(李明) / reviewer(张伟) / qa_engineer(刘洋) / ops_engineer(赵强)

**业务团队**：content_marketer(陈静) / data_analyst(孙晓) / support_agent(周梅)

### 12. 动态角色工厂 (`src/core/role_factory.py`)

用人需求 → LLM 生成角色配置 → 新 AgentRole → 入职上岗。自动分配不重名人名（24 人名字库）。

---

## 项目结构

```
maf_scheduler/
├── src/
│   ├── core/
│   │   ├── types.py           # Event, AgentState, Priority 等数据类型
│   │   ├── event_bus.py       # EventBus: 定时事件调度表 (_tick_schedule)
│   │   ├── time_manager.py    # TimeEventBus: 时间(时钟/Tick/天) + 事件总线 + 快进
│   │   ├── roles.py           # AgentRole + RolePool（多角色线程池 + 动态入职/离职）
│   │   ├── agent_system.py    # 统一管理: TimeEventBus + RolePool + 事件分发
│   │   ├── computer.py        # Computer 基类 + Podman/Local/SSH + ComputerManager
│   │   ├── note_store.py      # 笔记 + 每日总结 (按天存储, 走角色电脑)
│   │   ├── role_templates.py  # 12 个预定义角色模板
│   │   ├── role_factory.py    # LLM 驱动动态创建角色
│   │   ├── dispatcher.py      # 事件广播到所有角色
│   │   ├── tools.py           # ToolRegistry: 工具注册 + to_openai_tools
│   │   └── llm.py             # DeepSeek API 客户端 (原生 function calling)
│   ├── python_tools/          # Python 工具类 (DEFAULT_TOOLKITS)
│   │   ├── memory_toolkit.py  # summary (总结+下班+关机) / 笔记
│   │   ├── time_toolkit.py    # get_time / take_rest
│   │   ├── task_toolkit.py    # 定时任务 CRUD (持久化到电脑 tasks/)
│   │   ├── computer_toolkit.py# run_command / computer_status / lan_devices / reboot
│   │   ├── mcp_manager.py     # MCPManager: mcp_search/add/remove (安装到个人电脑)
│   │   ├── mcp_toolkit.py     # MCPServer: 服务器连接 (支持自定义启动命令)
│   │   ├── hr_toolkit.py      # post_job_posting (招聘即入职) / list_candidates
│   │   ├── client_toolkit.py  # talk_to_client (甲方交流, CEO 专属)
│   │   └── talk_toolkit.py    # talk / list_roles (角色间通信)
│   ├── config/
│   │   └── mcp_group_rules.json  # MCP 服务器与工具分组配置
│   ├── main.py                # 主入口: 多日循环 (自动进入第二天)
│   └── mcp_demo.py            # MCP 工具调用演示
└── data/
    ├── main_run.log           # 运行日志
    └── computers/<role>/      # 角色电脑 (宿主机侧挂载目录)
```

---

## 快速开始

### 前置条件

- Python 3.10+，`pip install -r requirements.txt`（或使用项目自带 `.venv/`）
- [podman](https://podman.io/)（每角色电脑的容器运行时，**必须安装**；本地模拟请显式使用 `computer_kind="local"`）
- DeepSeek API Key（环境变量 `DEEPSEEK_API_KEY`）

```bash
cd maf_scheduler
source .venv/bin/activate

# 设置 API Key (必填, 源码不再硬编码)
export DEEPSEEK_API_KEY="sk-..."

# 运行完整作息演示 (多日循环, 自动进入第二天)
python src/main.py
```

### 编程式使用

```python
from src.core.agent_system import AgentSystem
from src.core.types import Event, Priority

# 统一管理 TimeEventBus + RolePool + 事件分发
system = AgentSystem(role_ids=["CEO", "COO", "HR"])
system.start()   # 启动角色线程 + 时间线程 (Tick 0 / 第 1 天)

# 投递事件 (SHIFT_START/SHIFT_END 由时间线程自动触发)
system.trigger(Event(source="github", event_type="new_pr",
                     priority=Priority.HIGH, payload={"pr_number": 188}))

# 注册定时事件 (指定 Tick 触发; 不传 tick 则立即投递)
system.time_manager.register_event(
    Event(source="meeting", event_type="standup", priority=Priority.HIGH),
    tick=30,
)

print(system.describe())   # 第 X 天, Tick Y (上班中/已下班)
system.stop()
```

### 手动组合

```python
from src.core.dispatcher import EventDispatcher
from src.core.roles import RolePool
from src.core.role_templates import get_template
from src.core.time_manager import TimeEventBus

pool = RolePool()
pool.add_role(get_template("CEO"))

tm = TimeEventBus()
tm.set_event_sender(lambda ev: EventDispatcher(pool).trigger(ev))
tm.start()
pool.start()
```

---

## 使用示例

### 角色间通信

```python
coo = pool.get_role("COO")
coo.talk_to("HR", "请发布招聘: 需要一位精通 Rust 的后端工程师", "HIGH")
# → HR 队列收到: [FROM COO(陈总)] 请发布招聘...
```

### HR 招聘即入职

```python
hr = pool.get_role("HR")
result = hr._tools.call_tool("post_job_posting", {
    "requirement": "需要一位精通 Rust 的后端工程师, 熟悉 gRPC 和 PostgreSQL",
})
# → 后台生成新人 → 立即加入团队并上岗 (add_role_and_start)
# → 返回新人完整档案: role_id/姓名/技能, 状态 "已加入团队并上岗"
```

### 查看内网电脑设备

```python
dev = pool.get_role("CEO")
print(dev._tools.call_tool("lan_devices", {}))
# → 内网电脑设备:
#   - 林总 (CEO) | 电脑 maf-CEO | 10.89.0.2
#   - 陈总 (COO) | 电脑 maf-COO | 10.89.0.3
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| DEEPSEEK_API_KEY | (必填) | DeepSeek API 密钥 |
| DEEPSEEK_MODEL | deepseek-v4-flash | 模型名称 |
| DEEPSEEK_THINKING | true | 是否启用思考模式 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com | API 地址 |
| LLM_PROVIDER | deepseek | LLM 后端: `deepseek` (云端) / `ollama` (本地) |
| OLLAMA_BASE_URL | http://localhost:11434 | Ollama 服务地址 (OpenAI 兼容端点) |
| OLLAMA_MODEL | gemma4:e4b-it-q4_K_M | 本地 Ollama 模型标签 |

### 使用本地 Ollama 模型

项目默认走 DeepSeek 云端 API。要改用本地 Ollama (OpenAI 兼容端点, 免 API Key),
设置环境变量后照常运行即可, 角色线程与招聘流程 (`RolePool`/`RoleFactory`) 会自动
切换到 `OllamaLLM`:

```bash
export LLM_PROVIDER=ollama                     # 全局切换后端
export OLLAMA_MODEL=gemma4:e4b-it-q4_K_M       # 默认即此, 可省略
ollama serve                                    # 确保本地服务在跑
python src/role_demo.py                         # 示例: 多角色系统全走本地模型
```

也可代码级指定: `RolePool(llm_provider="ollama")` 或 `RoleFactory(provider="ollama")`。

### 状态持久化 (StateStore)

`main.py` 运行期间所有可序列化状态统一保存到 `data/state.json` (JSON 原子写, 不入库):

- **角色档案** — 名称/职位/职责/性格/技能/状态
- **任务历史** — 每个角色已完成/失败的任务 (含 talk 消息与结果 = 对话/工作记录)
- **未完成任务** — 队列待办, 重启后继续处理
- **电脑/容器信息** — podman 容器类型/人名映射, 重启后绑定已存在的容器, 不重建
- **时间进度** — 第几天/Tick, 恢复后作息继续

**退出时自动保存** (Ctrl+C 或正常结束), **启动时自动加载上次进度** (从上次的天数继续)。

```python
from src.core.state_store import StateStore
store = StateStore()
if store.exists():
    store.restore(system)   # 启动加载
store.save(system)          # 退出保存
```

### 角色活动日志

每个角色一份活动日志 (`data/journals/<role_id>.md`, 已 gitignore 不入库), 记录该角色
的上下文更新: 收到任务 / 开始执行 / 工具调用 / 笔记写入 / 消息收发 / WAIT 状态变化 /
事件接受与跳过。**全局通知 (SHIFT_START/SHIFT_END 作息事件、广播事件) 会写入每个角色
的日志**, 方便统一查看团队活动。

每行格式: `[D<第几天> T<Tick> HH:MM:SS] 内容`

```python
role.journal("任意活动记录")   # 写入该角色单独的文件
pool.journal_all("全局通知")   # 每个角色的日志都写一条
```
