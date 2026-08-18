"""Todo 清单 + 任务列表 工具测试.

覆盖:
  - TodoStore CRUD (add/list/update/delete, 状态校验, 持久化)
  - todo 工具 (todo_add/list/update/delete) 直接调 handler
  - my_tasks 工具 (队列 + 历史视图)
"""

from __future__ import annotations

import pytest

from src.core.roles import AgentRole, RolePool, Task, Urgency
from src.core.todo_store import TodoStore
from src.python_tools.task_view_toolkit import create_task_view_toolkit
from src.python_tools.todo_toolkit import create_todo_toolkit


# ── TodoStore 存储层 ─────────────────────────────────────


def test_todo_store_crud(tmp_path):
    """add → list → update → delete 完整流程 + 持久化."""
    store = TodoStore(role_id="tester_1", path=str(tmp_path / "todos.json"))
    item = store.add("写周报", "本周工作小结")
    assert item["status"] == "pending"
    assert item["id"] and item["title"] == "写周报"

    # 重新加载 (持久化生效)
    store2 = TodoStore(role_id="tester_1", path=str(tmp_path / "todos.json"))
    assert len(store2.list()) == 1

    # 状态更新
    updated = store.update(item["id"], "in_progress")
    assert updated["status"] == "in_progress"
    assert store.update(item["id"], "completed")["status"] == "completed"

    # 状态过滤
    assert len(store.list(status="pending")) == 0
    assert len(store.list(status="completed")) == 1

    # 删除
    assert store.delete(item["id"]) is True
    assert store.delete(item["id"]) is False  # 已删
    assert store.list() == []


def test_todo_store_invalid_status(tmp_path):
    """非法状态 → ValueError."""
    store = TodoStore(role_id="r", path=str(tmp_path / "t.json"))
    item = store.add("x")
    with pytest.raises(ValueError):
        store.update(item["id"], "bogus")


# ── todo 工具层 ──────────────────────────────────────────


def _bind_todo(role: AgentRole, path=None) -> object:
    """绑定 TodoStore (默认隔离路径, 避免读到真实运行残留)."""
    from src.core.todo_store import TodoStore
    tk = create_todo_toolkit()
    tk._todo_holder["store"] = TodoStore(role_id=role.role_id, path=path)  # type: ignore[attr-defined]
    return tk


def test_todo_tools_via_handler(tmp_path, monkeypatch):
    """todo_add → todo_list → todo_update → todo_delete 全链路."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    role = AgentRole(name="测试", role_id="tester_1")
    pool.add_role(role)
    tk = _bind_todo(role, str(tmp_path / "todos.json"))
    h = tk._tools["todo_add"].handler

    # 缺标题报错
    assert "必填" in h({"detail": "x"})
    # 添加
    r1 = h({"title": "写周报", "detail": "本周小结"})
    assert "已添加待办 [ID=" in r1
    tid = r1.split("ID=")[1].split("]")[0]
    # 列表
    lst = tk._tools["todo_list"].handler({})
    assert "写周报" in lst and "pending" in lst
    # 更新
    r2 = tk._tools["todo_update"].handler({"todo_id": tid, "status": "in_progress"})
    assert "→ in_progress" in r2
    # 删除
    assert "已删除" in tk._tools["todo_delete"].handler({"todo_id": tid})
    assert "(暂无待办" in tk._tools["todo_list"].handler({})


# ── 任务列表工具 ─────────────────────────────────────────


def test_my_tasks_tool(tmp_path, monkeypatch):
    """my_tasks: 展示队列 + 历史."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    role = AgentRole(name="测试", role_id="tester_1")
    pool.add_role(role)
    tk = create_task_view_toolkit()
    tk._role_holder["role"] = role  # type: ignore[attr-defined]

    # 空状态
    out = tk._tools["my_tasks"].handler({})
    assert "待处理 (队列 0 个)" in out

    # 队列 1 个 + 历史 2 条
    role.add_task(Task(urgency=Urgency.NORMAL, description="还没开始的任务"))
    role._task_history.append(Task(
        urgency=Urgency.HIGH, description="已完成的任务", status="done",
        tokens_consumed=123))
    role._task_history.append(Task(
        urgency=Urgency.NORMAL, description="失败的任务", status="failed",
        result="[ERROR] x"))
    out = tk._tools["my_tasks"].handler({})
    assert "还没开始的任务" in out
    assert "已完成的任务" in out and "123 tokens" in out
    assert "失败的任务" in out

    # scope=pending 只看队列
    out_p = tk._tools["my_tasks"].handler({"scope": "pending"})
    assert "还没开始的任务" in out_p and "已完成的任务" not in out_p
