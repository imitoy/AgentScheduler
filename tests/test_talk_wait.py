"""talk 工具 wait=true 同步等待回复测试.

覆盖:
  - wait 往返: A wait B → B 回复 → A 收到回复并恢复原状态
  - 双向互等拆解: A 等 B 时 B 再 wait A → 直接作为回复投递, B 不进入等待
  - 环形等待拒绝: A 等 B, B 等 C, C 等 A → 再发起 wait 被拒绝 (防死锁)
  - 无限等待 + 等待提示: 消息告知对方"提问者正在等待", 无超时限制
  - 非等待对象消息: 他人发给 WAIT 角色 → 正常入队, 不误投递为回复

注: 直接调用 talk 处理器闭包 (与 ToolRegistry.call_tool 走同一逻辑),
绕过 TextContent 包装层 — 该层与等待语义无关, 且依赖 mcp 版本.
"""

from __future__ import annotations

import threading
import time

from src.core.roles import AgentRole, RolePool
from src.python_tools import talk_toolkit
from src.python_tools.talk_toolkit import create_talk_toolkit


def _setup_roles(tmp_path, monkeypatch, *role_ids):
    """构造若干角色 + 各自的 talk 工具类, 返回 (pool, {rid: toolkit})."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    pool = RolePool()
    toolkits = {}
    for rid in role_ids:
        role = AgentRole(name=f"角色{rid}", role_id=rid)
        pool.add_role(role)
        tk = create_talk_toolkit(pool)
        tk._role_holder = {"role": role}  # type: ignore[attr-defined]
        toolkits[rid] = tk
    return pool, toolkits


def _talk(toolkits, sender_id: str, target: str, message: str, wait=False) -> str:
    """调用发送方 talk 处理器 (与 call_tool 同一逻辑)."""
    args: dict[str, object] = {"target": target, "message": message}
    if wait:
        args["wait"] = True
    return toolkits[sender_id]._tools["talk"].handler(args)


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── 人名暴露 (面向 LLM 的工具不暴露 role_id) ───────────────

def test_roster_hides_role_id(tmp_path, monkeypatch):
    """花名册只显示人名, 不暴露内部 role_id (索引)."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    pool = RolePool()
    pool.add_role(AgentRole(name="张三", role_id="dev_1"))
    pool.add_role(AgentRole(name="李四", role_id="dev_2"))
    roster = talk_toolkit.build_team_roster(pool)
    assert "张三" in roster and "李四" in roster
    assert "dev_1" not in roster and "dev_2" not in roster   # role_id 不出现
    assert "role_id" not in roster                           # 字段名也不出现


def test_talk_by_person_name(tmp_path, monkeypatch):
    """talk 的 target 用人名即可送达 (内部映射到 role_id)."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B")
    role_b = pool.get_role("B")
    # 发送方 A 用"角色B"(人名)作 target
    result = _talk(tks, "A", "角色B", "按名字发送测试")
    assert "消息已发送给 角色B" in result
    assert role_b.queue_depth == 1


def test_talk_unknown_name_gives_hint(tmp_path, monkeypatch):
    """target 找不到时返回错误并提示先 list_roles."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B")
    result = _talk(tks, "A", "不存在的名字", "hi")
    assert "找不到" in result
    assert "list_roles" in result


# ── 1) wait 往返 ──────────────────────────────────────────

def test_wait_roundtrip(tmp_path, monkeypatch):
    """A wait=true 发给 B, B 用 talk 回复 → A 收到回复, 状态恢复为原状态."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B")
    role_a = pool.get_role("A")

    result = {}

    def sender():
        result["text"] = _talk(tks, "A", "B", "请问进度?", wait=True)

    t = threading.Thread(target=sender)
    t.start()
    try:
        assert _wait_until(lambda: role_a.state.value == "WAIT"), "A 未进入 WAIT"
        # B 处理消息后回复 A (普通 talk, 不带 wait)
        reply = _talk(tks, "B", "A", "进度 80%")
        assert "已回复给正在等待的" in reply
        t.join(timeout=5)
    finally:
        t.join(timeout=1)
        pool.shutdown(wait=False)

    assert not t.is_alive()
    assert "已收到 角色B 的回复: 进度 80%" in result["text"]
    assert role_a.state.value == "ON_DUTY_IDLE"   # 状态恢复


def test_wait_roundtrip_with_real_names(tmp_path, monkeypatch):
    """真实 LLM 场景: name ≠ role_id (如 王建国/architect), target 用人名.

    回归: _begin_wait 曾存人名导致回复投递条件 (role_id 比较) 永不成立,
    A 干等 120s 超时 — 修复后等待链统一用 role_id, 人名仅作 LLM 参数.
    """
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path)
    pool = RolePool()
    a = AgentRole(name="王建国", role_id="architect")
    b = AgentRole(name="郭晓东", role_id="tester_1")
    pool.add_role(a)
    pool.add_role(b)
    tks = {}
    for r in (a, b):
        tk = create_talk_toolkit(pool)
        tk._role_holder = {"role": r}  # type: ignore[attr-defined]
        tks[r.role_id] = tk

    result = {}

    def sender():
        # A (architect/王建国) 用人名 "郭晓东" 发 wait=true
        result["text"] = _talk(tks, "architect", "郭晓东", "进度?", wait=True)

    t = threading.Thread(target=sender)
    t.start()
    try:
        assert _wait_until(lambda: a.state.value == "WAIT"), "A 未进入 WAIT"
        assert a._waiting_reply_from == "tester_1"   # 内部等待链存 role_id
        # B (tester_1/郭晓东) 用人名 "王建国" 回复 → 应直接投递唤醒
        reply = _talk(tks, "tester_1", "王建国", "进度 80%")
        assert "已回复给正在等待的" in reply
        t.join(timeout=5)
    finally:
        t.join(timeout=1)
        pool.shutdown(wait=False)

    assert not t.is_alive()
    assert "已收到 郭晓东 的回复: 进度 80%" in result["text"]
    assert a.state.value == "ON_DUTY_IDLE"


# ── 2) 双向互等拆解 ───────────────────────────────────────

def test_mutual_wait_decomposed(tmp_path, monkeypatch):
    """A 等 B 期间, B 也调 talk(A, wait=true) → 作为回复投递, B 不进入等待."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B")
    role_a, role_b = pool.get_role("A"), pool.get_role("B")

    role_a._begin_wait("B")   # A 正在等 B 的回复
    try:
        # B 回复时误带 wait=true → 应被当作回复投递 (忽略 wait), 不死锁
        result = _talk(tks, "B", "A", "收到, 马上处理", wait=True)
        assert "已回复给正在等待的" in result
        assert role_a._reply_box == "收到, 马上处理"   # 投递进 A 的信箱
        assert role_b.state.value == "ON_DUTY_IDLE"    # B 没有进入 WAIT
    finally:
        role_a._end_wait()


# ── 3) 环形等待拒绝 ───────────────────────────────────────

def test_deadlock_cycle_rejected(tmp_path, monkeypatch):
    """A 等 B, B 等 C, C 等 A (等待链成环) → A 再 wait B 被拒绝."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B", "C")
    role_a, role_b, role_c = pool.get_role("A"), pool.get_role("B"), pool.get_role("C")

    role_b._begin_wait("C")   # B 等 C
    role_c._begin_wait("A")   # C 等 A → 链: B→C→A
    try:
        result = _talk(tks, "A", "B", "有急事", wait=True)
        assert "死锁" in result          # wait 被拒绝
        assert role_a.state.value == "ON_DUTY_IDLE"   # A 未进入 WAIT
    finally:
        role_b._end_wait()
        role_c._end_wait()


# ── 4) 无限等待 + 等待提示 ────────────────────────────────

def test_wait_message_carries_waiting_hint(tmp_path, monkeypatch):
    """wait=true: 消息附带'提问者正在等待'提示; 等待无超时限制 (LLM 时间不可预测)."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B")
    role_a, role_b = pool.get_role("A"), pool.get_role("B")

    result = {}

    def sender():
        result["text"] = _talk(tks, "A", "B", "进度如何?", wait=True)

    t = threading.Thread(target=sender)
    t.start()
    try:
        assert _wait_until(lambda: role_b.queue_depth == 1), "B 未收到消息"
        # B 收到的任务附带"提问者正在等待"提示 (含提问者人名), 让目标知情
        task = role_b.pop_task()
        assert task is not None
        assert "正在等待你的回复" in task.description
        assert "角色A" in task.description          # 提示里带提问者人名
        assert task.context.get("waiting") is True
        # 无 120s 超时: 等 1.5s (旧版 1s 超时会返回错误) 仍在 WAIT
        time.sleep(1.5)
        assert role_a.state.value == "WAIT"
        # B 回复 → A 正常唤醒
        reply = _talk(tks, "B", "A", "进度 80%")
        assert "已回复给正在等待的" in reply
        t.join(timeout=5)
    finally:
        t.join(timeout=1)
        pool.shutdown(wait=False)

    assert not t.is_alive()
    assert "已收到 角色B 的回复: 进度 80%" in result["text"]
    assert role_a.state.value == "ON_DUTY_IDLE"   # 状态恢复


# ── 5) 非等待对象消息不投递 ───────────────────────────────

def test_third_party_message_not_delivered_as_reply(tmp_path, monkeypatch):
    """A 等 B 期间, C 发普通消息给 A → 正常入队, 不误投为回复, A 仍 WAIT."""
    pool, tks = _setup_roles(tmp_path, monkeypatch, "A", "B", "C")
    role_a = pool.get_role("A")

    role_a._begin_wait("B")
    try:
        result = _talk(tks, "C", "A", "普通消息")
        assert "消息已发送给" in result
        assert role_a.state.value == "WAIT"            # 仍在等待 B
        assert role_a._reply_box is None               # 没有收到"回复"
        assert role_a.queue_depth == 1                 # 消息入队, 恢复后处理
    finally:
        role_a._end_wait()


# ── 6) 附件 (公司云盘 /mnt/drive 文件) ───────────────────

def test_talk_attachment_validated_and_carried(tmp_path, monkeypatch):
    """talk attachment: 无效附件拒绝; 有效附件随消息携带并提示对方."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    a = AgentRole(name="郭晓东", role_id="tester_1", computer_kind="local")
    b = AgentRole(name="王建国", role_id="architect", computer_kind="local")
    pool.add_role(a)
    pool.add_role(b)
    # 郭晓东在云盘放附件 (Local 降级: drive_root = 本地 data/drive)
    a.computer.write_file(f"{a.computer.drive_root}/郭晓东/设计稿.md", "附件内容")

    tk_a = create_talk_toolkit(pool)
    tk_a._role_holder = {"role": a}  # type: ignore[attr-defined]
    tk_b = create_talk_toolkit(pool)
    tk_b._role_holder = {"role": b}  # type: ignore[attr-defined]

    # 无效附件 (不存在) → 拒绝
    r = tk_a._tools["talk"].handler(
        {"target": "王建国", "message": "看下", "attachment": "郭晓东/不存在.md"})
    assert "附件无效" in r
    assert b.queue_depth == 0
    # 有效附件 → 送达, 任务描述带附件提示
    r2 = tk_a._tools["talk"].handler(
        {"target": "王建国", "message": "看下设计稿", "attachment": "郭晓东/设计稿.md"})
    assert "消息已发送给 王建国" in r2
    task = b.pop_task()
    assert task is not None
    assert "[附件: 郭晓东/设计稿.md]" in task.description
    assert "mnt/drive" in task.description
    assert task.context.get("attachment") == "郭晓东/设计稿.md"
    pool.shutdown(wait=False)
