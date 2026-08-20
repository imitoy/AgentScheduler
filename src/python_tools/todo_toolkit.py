"""Todo 清单工具类 (Todo ToolKit) — 角色个人待办清单管理.

包含:
  - todo_add:    添加待办 (标题 + 可选详情), 返回待办 id
  - todo_list:   列出待办 (可按状态过滤)
  - todo_update: 更新待办状态 (pending / in_progress / completed)
  - todo_delete: 删除待办

与 Hermes 自身的 todo 工具同款语义: 每项待办有唯一 id 与状态
(pending → in_progress → completed), 持久化到 data/todos/<role_id>.json.

用法:
    from src.python_tools.todo_toolkit import create_todo_toolkit
    role.add_toolkit(create_todo_toolkit())   # 自动绑定角色与存储

接口文档 (模块结构与方法):

模块级函数:
    - create_todo_toolkit(): 创建 Todo 清单工具类.
    - bind_todo_to_toolkit(): 将 TodoStore 绑定到工具类 (由 AgentRole.add_toolkit 内部调用).
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_todo_toolkit() -> ToolKit:
    """创建 Todo 清单工具类.

    返回:
        包含 todo_add / todo_list / todo_update / todo_delete 的 ToolKit.
    """

    tk = ToolKit(name="todo", description="Todo 清单工具类: 管理自己的待办事项")

    # 工具类持有 store 引用 (由 AgentRole.add_toolkit 注入)
    tk._todo_holder = {"store": None}  # type: ignore[attr-defined]

    def _get_store() -> Any:
        store = getattr(tk, "_todo_holder", {}).get("store")
        if store is None:
            raise RuntimeError("Todo 工具类尚未绑定存储, 请通过 role.add_toolkit() 注册")
        return store

    def _fmt(item: dict[str, Any]) -> str:
        """条目 → 展示文本."""
        mark = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}
        base = (f"[{item.get('id')}] {mark.get(item.get('status'), '⬜')} "
                f"{item.get('status')}: {item.get('title')}")
        if item.get("detail"):
            base += f" — {item['detail']}"
        return base

    def _todo_add(args: dict[str, Any]) -> str:
        """添加待办.

        参数:
            args: {"title": 待办标题, "detail": 详情(可选)}

        返回:
            创建结果与待办 id.
        """
        title = args.get("title", "").strip()
        detail = args.get("detail", "").strip()
        if not title:
            return "错误: 'title' (待办标题) 为必填参数."
        item = _get_store().add(title, detail)
        return f"已添加待办 [ID={item['id']}]: {item['title']} (状态 pending)"

    def _todo_list(args: dict[str, Any]) -> str:
        """列出待办.

        参数:
            args: {"status": 状态过滤(可选: pending/in_progress/completed)}

        返回:
            待办列表 (按创建顺序).
        """
        status = args.get("status") or None
        items = _get_store().list(status=status)
        if not items:
            hint = f" (状态={status})" if status else ""
            return f"(暂无待办{hint})"
        lines = [f"- {_fmt(i)}" for i in items]
        head = f"待办清单 ({len(items)} 项"
        if status:
            head += f", 状态={status}"
        return head + "):\n" + "\n".join(lines)

    def _todo_update(args: dict[str, Any]) -> str:
        """更新待办状态.

        参数:
            args: {"todo_id": 待办 id, "status": 新状态}

        返回:
            更新结果.
        """
        todo_id = args.get("todo_id", "").strip()
        status = args.get("status", "").strip()
        if not todo_id:
            return "错误: 'todo_id' (待办 id) 为必填参数."
        if not status:
            return "错误: 'status' (新状态) 为必填参数."
        try:
            item = _get_store().update(todo_id, status)
        except ValueError as exc:
            return f"错误: {exc}"
        if item is None:
            return f"待办不存在: {todo_id}"
        return f"待办已更新 [ID={todo_id}]: {item['title']} → {item['status']}"

    def _todo_delete(args: dict[str, Any]) -> str:
        """删除待办.

        参数:
            args: {"todo_id": 待办 id}

        返回:
            删除结果.
        """
        todo_id = args.get("todo_id", "").strip()
        if not todo_id:
            return "错误: 'todo_id' (待办 id) 为必填参数."
        if _get_store().delete(todo_id):
            return f"待办已删除: {todo_id}"
        return f"待办不存在: {todo_id}"

    tk.add_python_tool(
        name="todo_add",
        description="添加一条待办事项到我的 Todo 清单 (返回待办 id, 后续用它更新/删除).",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "待办标题"},
                "detail": {"type": "string", "description": "待办详情 (可选)"},
            },
            "required": ["title"],
        },
        handler=_todo_add,
    )
    tk.add_python_tool(
        name="todo_list",
        description="列出我的 Todo 待办清单 (可按状态过滤: pending/in_progress/completed).",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "状态过滤 (可选)"},
            },
        },
        handler=_todo_list,
    )
    tk.add_python_tool(
        name="todo_update",
        description="更新一条待办的状态: pending(未开始) → in_progress(进行中) → completed(已完成).",
        input_schema={
            "type": "object",
            "properties": {
                "todo_id": {"type": "string", "description": "待办 id (从 todo_list 获取)"},
                "status": {"type": "string", "description": "新状态: pending / in_progress / completed"},
            },
            "required": ["todo_id", "status"],
        },
        handler=_todo_update,
    )
    tk.add_python_tool(
        name="todo_delete",
        description="删除一条待办事项 (从清单移除).",
        input_schema={
            "type": "object",
            "properties": {
                "todo_id": {"type": "string", "description": "待办 id (从 todo_list 获取)"},
            },
            "required": ["todo_id"],
        },
        handler=_todo_delete,
    )
    return tk


def bind_todo_to_toolkit(toolkit: ToolKit, store: Any) -> None:
    """将 TodoStore 绑定到工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: todo 工具类实例.
        store:   TodoStore 实例.
    """
    toolkit._todo_holder["store"] = store  # type: ignore[attr-defined]
