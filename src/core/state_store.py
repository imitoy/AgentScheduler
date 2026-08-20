"""统一状态存储 (StateStore) — 结构化数据 + 对话内容 + 容器信息持久化.

把所有可序列化状态汇总到单个 JSON 文件 (默认 data/state.json):
  - 角色档案: 名称/职位/职责/性格/技能/关键词/状态
  - 任务历史: 每个角色已完成/失败的任务 (含 talk 消息与结果 = 对话/工作记录)
  - 未完成任务: 队列中的待办, 重启后继续处理
  - 电脑/容器信息: 容器类型/人名映射, 重启后绑定已存在的容器, 不重建
  - 时间进度: 第几天/Tick, 恢复后作息继续

用法 (main.py):
    store = StateStore()
    if store.exists():
        store.restore(system)          # 启动自动加载上次进度
    ...
    store.save(system)                 # 退出自动保存

存储: JSON 原子写 (tmp + rename), data/ 整体 gitignored 不入库.

接口文档 (模块结构与方法):

类与方法:
    StateStore:
        - exists(): 是否存在存档文件.
        - save(): 保存系统全部状态到 JSON (原子写: tmp + rename).
        - restore(): 从存档恢复系统状态 (角色档案/任务/容器/时间).
"""
from __future__ import annotations

import json
import logging
import time as time_module
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = "./data/state.json"


class StateStore:
    """统一状态存储: 角色档案 / 任务 / 对话记录 / 容器信息 / 时间进度.

    参数:
        path: 存档文件路径 (默认 data/state.json).
    """

    VERSION = 1

    def __init__(self, path: str | Path = DEFAULT_STATE_FILE):
        self._path = Path(path)

    # ── 基础 ──────────────────────────────────────────────

    def exists(self) -> bool:
        """是否存在存档文件."""
        return self._path.exists()

    def save(self, system: Any) -> str:
        """保存系统全部状态到 JSON (原子写: tmp + rename).

        参数:
            system: AgentSystem 实例 (含 RolePool/TimeEventBus/电脑).

        返回:
            存档文件路径.
        """
        data = self._collect(system)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        logger.info("StateStore: 状态已保存 → %s (角色 %d, 任务历史 %d)",
                    self._path, len(data["roles"]),
                    sum(len(r["history"]) for r in data["roles"]))
        return str(self._path)

    def restore(self, system: Any) -> int:
        """从存档恢复系统状态 (角色档案/任务/容器/时间).

        参数:
            system: 已创建但未 start 的 AgentSystem 实例 (角色已按模板注册).

        返回:
            恢复的角色数 (0 = 无存档).
        """
        if not self.exists():
            return 0
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("StateStore: 存档读取失败, 跳过恢复: %s", exc)
            return 0
        if data.get("version") != self.VERSION:
            logger.warning("StateStore: 存档版本 %s ≠ 当前 %s, 跳过恢复",
                           data.get("version"), self.VERSION)
            return 0
        return self._apply(data, system)

    # ── 收集 (内存 → dict) ───────────────────────────────

    def _collect(self, system: Any) -> dict[str, Any]:
        """从 AgentSystem 收集全部可序列化状态."""
        pool = system.pool
        tm = system.time_manager

        roles_data: list[dict[str, Any]] = []
        for role in pool.all_roles():
            roles_data.append(self._role_to_dict(role))

        # 电脑/容器信息: 已创建电脑的角色 (role._computer 非 None)
        computers: dict[str, dict[str, Any]] = {}
        from src.core.computer import _COMPUTER_MANAGER
        for role in pool.all_roles():
            comp = getattr(role, "_computer", None)
            if comp is None:
                continue
            computers[role.role_id] = {
                "kind": type(comp).__name__.replace("Computer", "").lower(),
                "auto_mcp": bool(getattr(comp, "_auto_mcp", False)),
                "name": _COMPUTER_MANAGER._names.get(role.role_id, role.name),
            }

        return {
            "version": self.VERSION,
            "saved_at": time_module.strftime("%Y-%m-%d %H:%M:%S"),
            "time": {
                "day": tm.day_number(),
                "tick_of_day": tm.tick_of_day(),
            },
            "roles": roles_data,
            "computers": computers,
        }

    @staticmethod
    def _role_to_dict(role: Any) -> dict[str, Any]:
        """角色档案 + 任务 (队列待办 + 历史) → dict."""
        return {
            "role_id": role.role_id,
            "name": role.name,
            "title": role.title,
            "responsibilities": role.responsibilities,
            "personality": role.personality,
            "skills": list(role.skills),
            "interest_keywords": sorted(role.interest_keywords),
            "system_prompt_extra": role.system_prompt_extra,
            "is_default": role.is_default,
            "state": role.state.value,
            "salience_threshold": role.salience_threshold,
            "computer_kind": role.computer_kind,
            "computer_kwargs": dict(role.computer_kwargs),
            # 队列中未完成任务 (重启后继续处理)
            "pending_tasks": [StateStore._task_to_dict(t) for t in role._queue],
            # 已完成/失败任务历史 (对话/工作记录)
            "history": [StateStore._task_to_dict(t) for t in role._task_history],
        }

    @staticmethod
    def _task_to_dict(t: Any) -> dict[str, Any]:
        """Task → dict (urgency 存正数, 加载时 __post_init__ 转负)."""
        return {
            "urgency": abs(int(t.urgency)),
            "task_id": t.task_id,
            "description": t.description,
            "source": t.source,
            "context": dict(t.context),
            "status": t.status,
            "result": t.result,
            "tokens_consumed": t.tokens_consumed,
            "created_at": t.created_at,
            "assigned_role": t.assigned_role,
        }

    # ── 应用 (dict → 内存) ───────────────────────────────

    def _apply(self, data: dict[str, Any], system: Any) -> int:
        """把存档 dict 应用回 AgentSystem."""
        from src.core.roles import Task

        pool = system.pool
        restored = 0

        # 1) 角色档案 + 任务
        for rdata in data.get("roles", []):
            role = self._restore_role(pool, rdata, system)
            if role is None:
                continue
            restored += 1
            # 队列中未完成任务 (add_task 会写日志, 正常留痕)
            for t in rdata.get("pending_tasks", []):
                role.add_task(Task(**self._task_from_dict(t)))
            # 历史
            role._task_history = [
                Task(**self._task_from_dict(t)) for t in rdata.get("history", [])
            ]

        # 2) 电脑/容器: 重建对象并绑定已存在的容器 (不重建容器)
        self._restore_computers(system, data.get("computers", {}))

        # 3) 时间进度 (start() 时应用)
        t = data.get("time", {})
        system.time_manager.set_progress(t.get("day", 1), t.get("tick_of_day", 0))

        logger.info("StateStore: 已恢复 %d 个角色 → %s", restored,
                    system.time_manager.describe())
        return restored

    def _restore_role(self, pool: Any, rdata: dict[str, Any],
                      system: Any) -> Optional[Any]:
        """按存档恢复单个角色: 已注册则覆盖字段, 未注册则重建并装配."""
        from src.core.roles import AgentRole
        from src.core.types import AgentState

        role_id = rdata.get("role_id", "")
        try:
            role = pool.get_role(role_id)
        except KeyError:
            # 存档里的角色不在当前模板池 (如 RoleFactory 招聘的新人): 重建并装配
            role = AgentRole(
                name=rdata.get("name", role_id),
                role_id=role_id,
                title=rdata.get("title", ""),
                responsibilities=rdata.get("responsibilities", ""),
                personality=rdata.get("personality", ""),
                skills=list(rdata.get("skills", [])),
                interest_keywords=set(rdata.get("interest_keywords", [])),
                system_prompt_extra=rdata.get("system_prompt_extra", ""),
                is_default=bool(rdata.get("is_default", False)),
                computer_kind=rdata.get("computer_kind", "podman"),
                computer_kwargs=dict(rdata.get("computer_kwargs", {})),
            )
            system.add_role(role)

        # 覆盖档案字段 (模板创建的角色以存档为准)
        for f in ("name", "title", "responsibilities", "personality",
                  "system_prompt_extra"):
            if f in rdata:
                setattr(role, f, rdata[f])
        role.skills = list(rdata.get("skills", role.skills))
        role.interest_keywords = set(rdata.get("interest_keywords", role.interest_keywords))
        role.salience_threshold = float(rdata.get("salience_threshold", role.salience_threshold))
        try:
            role.state = AgentState(rdata.get("state", role.state.value))
        except ValueError:
            pass
        return role

    def _restore_computers(self, system: Any, computers: dict[str, dict[str, Any]]) -> None:
        """重建电脑对象并绑定到角色 (容器已存在, _ensure_container 幂等)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from src.core.computer import _COMPUTER_MANAGER

        roles = {r.role_id: r for r in system.pool.all_roles()}
        todo = {rid: c for rid, c in computers.items() if rid in roles
                and getattr(roles[rid], "_computer", None) is None}
        if not todo:
            return

        def _rebuild(item: tuple[str, dict[str, Any]]) -> None:
            rid, cdata = item
            role = roles[rid]
            kind = cdata.get("kind", "podman")
            if kind not in ("podman", "local", "ssh"):
                kind = "podman"
            comp = _COMPUTER_MANAGER.create(
                kind=kind, role_id=rid,
                name=cdata.get("name", role.name),
                auto_mcp=bool(cdata.get("auto_mcp", True)),
            )
            role._computer = comp
            comp.power_on()  # 容器已存在 → 幂等启动 + MCP 重连

        with ThreadPoolExecutor(max_workers=min(10, len(todo)),
                                thread_name_prefix="state-computer") as ex:
            futures = {ex.submit(_rebuild, item): item[0] for item in todo.items()}
            for fut in as_completed(futures):
                rid = futures[fut]
                try:
                    fut.result()
                    logger.info("StateStore: 电脑已恢复绑定 → %s", rid)
                except Exception:
                    logger.exception("StateStore: 电脑恢复失败 → %s", rid)

    @staticmethod
    def _task_from_dict(d: dict[str, Any]) -> dict[str, Any]:
        """dict → Task 构造参数."""
        return {
            "urgency": int(d.get("urgency", 3)),
            "task_id": d.get("task_id", ""),
            "description": d.get("description", ""),
            "source": d.get("source", ""),
            "context": dict(d.get("context", {})),
            "status": d.get("status", "pending"),
            "result": d.get("result", ""),
            "tokens_consumed": int(d.get("tokens_consumed", 0)),
            "created_at": float(d.get("created_at", time_module.time())),
            "assigned_role": d.get("assigned_role", ""),
        }
