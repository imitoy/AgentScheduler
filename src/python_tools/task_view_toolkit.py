"""任务列表工具类 (Task View ToolKit) — 查看分配给我的任务.

包含:
  - my_tasks: 列出当前角色的任务列表:
      * 待处理 (队列中): 已派发、还没开始处理的任务
      * 历史 (最近 N 条): 已完成 / 失败的任务 (含结果摘要与 token 消耗)

与 Todo 清单的区别: todo 是角色自己规划/跟踪的待办事项 (自管理);
my_tasks 是系统/同事派发给角色的任务流 (只读视图).

用法:
    from src.python_tools.task_view_toolkit import create_task_view_toolkit
    role.add_toolkit(create_task_view_toolkit())

接口文档 (模块结构与方法):

模块级函数:
    - create_task_view_toolkit(): 创建任务列表工具类.
    - bind_role_to_toolkit(): 将角色绑定到任务列表工具类 (由 AgentRole.add_toolkit 内部调用).
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 10  # 历史任务最多展示条数


def create_task_view_toolkit() -> ToolKit:
    """创建任务列表工具类.

    返回:
        包含 my_tasks 的 ToolKit.
    """

    tk = ToolKit(name="task_view",
                 description="任务列表工具类: 查看分配给我的任务 (队列+历史)")

    # 工具类持有 role 引用 (由 AgentRole.add_toolkit 注入)
    tk._role_holder = {"role": None}  # type: ignore[attr-defined]

    def _get_role() -> Any:
        role = getattr(tk, "_role_holder", {}).get("role")
        if role is None:
            raise RuntimeError("任务列表工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return role

    def _my_tasks(args: dict[str, Any]) -> str:
        """列出我的任务列表.

        参数:
            args: {"scope": 范围(可选: all/pending/done/failed, 默认 all)}

        返回:
            任务列表 (待处理队列 + 最近历史).
        """
        role = _get_role()
        scope = (args.get("scope") or "all").strip().lower()

        # 1) 待处理队列 (role._queue 是 Task 列表)
        queue = list(role._queue)
        pending_lines = []
        for t in queue:
            pending_lines.append(
                f"- [id={t.task_id}] 紧急度={abs(t.urgency)} | {t.description[:120]}")

        # 2) 历史 (最近 HISTORY_LIMIT 条)
        history = list(role._task_history[-HISTORY_LIMIT:])
        hist_lines = []
        for t in reversed(history):
            mark = "✅" if t.status == "done" else "❌"
            hist_lines.append(
                f"- {mark} [{t.status}, {t.tokens_consumed} tokens] "
                f"{t.description[:100]}")

        parts = []
        if scope in ("all", "pending"):
            head = f"📥 待处理 (队列 {len(pending_lines)} 个)"
            parts.append(head + ("\n" + "\n".join(pending_lines) if pending_lines else " — 空"))
        if scope in ("all", "done", "failed"):
            if scope in ("done", "failed"):
                hist_lines = [l for l in hist_lines if f"[{scope}," in l]
            head = f"📋 最近任务 ({len(hist_lines)} 条)"
            parts.append(head + ("\n" + "\n".join(hist_lines) if hist_lines else " — 空"))
        return "\n\n".join(parts)

    tk.add_python_tool(
        name="my_tasks",
        description=(
            "查看分配给我的任务列表: 待处理队列 (系统/同事派发、还没开始) "
            "+ 最近完成/失败的任务历史 (含结果与 token 消耗). "
            "想了解自己当前有什么活没干、干过什么时用这个. "
            "scope 可选: all(默认)/pending(只看队列)/done/failed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "范围: all/pending/done/failed (可选)"},
            },
        },
        handler=_my_tasks,
    )
    return tk


def bind_role_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将角色绑定到任务列表工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: task_view 工具类实例.
        role:    AgentRole 实例 (提供任务队列与历史).
    """
    toolkit._role_holder["role"] = role  # type: ignore[attr-defined]
