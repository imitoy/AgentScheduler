"""Todo 清单存储 (TodoStore) — 角色个人待办清单 (JSON 持久化).

每个角色一份独立清单: data/todos/<role_id>.json (data/ 整体 gitignored).

支持:
  - add:    添加待办 (标题 + 可选详情), 返回带 id 的条目
  - list:   列出待办 (可按状态过滤 pending/in_progress/completed)
  - update: 更新状态 (pending → in_progress → completed)
  - delete: 删除待办

条目结构:
    {"id": "8字符", "title": "...", "detail": "...", "status": "pending",
     "created_at": 时间戳, "updated_at": 时间戳}

用法:
    store = TodoStore(role_id="tester_1")
    store.add("写周报", "本周工作小结")
    store.list(status="pending")

接口文档 (模块结构与方法):

类与方法:
    TodoStore:
        - add(): 添加待办.
        - list(): 列出待办 (按创建时间排序).
        - update(): 更新待办状态.
        - delete(): 删除待办. 返回是否删除成功.
"""
from __future__ import annotations

import json
import logging
import time as time_module
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 合法状态 (与 Hermes todo 工具一致)
TODO_STATUSES = ("pending", "in_progress", "completed")


class TodoStore:
    """角色个人 Todo 清单 (JSON 文件, 原子写).

    参数:
        role_id: 角色标识 (清单隔离键).
        path:    存储文件路径 (默认 data/todos/<role_id>.json).
    """

    def __init__(self, role_id: str = "", path: Optional[str] = None):
        self.role_id = role_id
        self._path = Path(path) if path else (
            Path("./data/todos") / f"{role_id or 'shared'}.json")

    # ── 底层读写 ──────────────────────────────────────────

    def _load(self) -> list[dict[str, Any]]:
        """读取清单 (文件不存在返回空列表)."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("TodoStore[%s] 读取失败: %s", self.role_id, exc)
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        """原子写 (tmp + rename)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._path)

    # ── CRUD ──────────────────────────────────────────────

    def add(self, title: str, detail: str = "") -> dict[str, Any]:
        """添加待办.

        参数:
            title:  待办标题 (必填).
            detail: 待办详情 (可选).

        返回:
            新条目 dict (含 id/status/created_at).
        """
        now = time_module.time()
        item = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "detail": detail,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
        items = self._load()
        items.append(item)
        self._save(items)
        logger.info("TodoStore[%s] 已添加待办 [%s]: %s", self.role_id, item["id"], title)
        return item

    def list(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        """列出待办 (按创建时间排序).

        参数:
            status: 状态过滤 (pending/in_progress/completed; None = 全部).

        返回:
            条目列表.
        """
        items = self._load()
        if status is not None:
            items = [i for i in items if i.get("status") == status]
        return items

    def update(self, todo_id: str, status: str) -> Optional[dict[str, Any]]:
        """更新待办状态.

        参数:
            todo_id: 待办 id.
            status:  新状态 (pending/in_progress/completed).

        返回:
            更新后的条目; id 不存在返回 None.

        异常:
            ValueError: status 非法.
        """
        if status not in TODO_STATUSES:
            raise ValueError(
                f"非法状态 '{status}', 可选: {', '.join(TODO_STATUSES)}")
        items = self._load()
        for item in items:
            if item.get("id") == todo_id:
                item["status"] = status
                item["updated_at"] = time_module.time()
                self._save(items)
                logger.info("TodoStore[%s] 待办 [%s] → %s", self.role_id, todo_id, status)
                return item
        return None

    def delete(self, todo_id: str) -> bool:
        """删除待办. 返回是否删除成功."""
        items = self._load()
        kept = [i for i in items if i.get("id") != todo_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        logger.info("TodoStore[%s] 待办已删除 [%s]", self.role_id, todo_id)
        return True
