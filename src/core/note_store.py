"""笔记存储 (NoteStore) — 基于文件的笔记与日记存储.

每个 Role 绑定一个 NoteStore 实例, 内容按角色隔离:
    data/notes/<role_id>/<标题>.md          # 普通笔记
    data/notes/<role_id>/_summary_<日期>.md # 每日总结 (下一天注入提示词)

支持:
  - write_note: 写笔记 (标题 + 内容)
  - edit_note:  编辑已有笔记
  - list_notes: 列出所有笔记标题
  - read_note:  读取笔记内容
  - save_summary / get_latest_summary: 每日总结 (作息系统用)

接口文档 (模块结构与方法):

类与方法:
    NoteStore:
        - get_reminder(): 查询笔记的提醒信息 (未触发).
        - write_note(): 写笔记. 已存在则覆盖.
        - edit_note(): 编辑已有笔记 (覆盖内容). 不存在则创建.
        - list_notes(): 列出所有笔记标题 (不含每日总结). 按文件名排序.
        - read_note(): 读取笔记内容.
        - delete_note(): 删除笔记 (真实删除文件 + 取消关联提醒). 返回是否删除成功.
        - save_summary(): 保存某一天的总结.
        - get_summary(): 读取指定天的总结.
        - get_latest_summary(): 读取最近一次总结 (用于下一天冷启动).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class NoteStore:
    """文件型笔记存储. 每个角色实例独立目录.

    参数:
        base_dir: 存储根目录 (默认 ./data/notes) — 无 computer 时的本地回退路径
        role_id:  角色标识, 用于隔离目录 (可为空, 由 AgentRole 传入)
        computer: 个人电脑实例 (可选). 提供后笔记/总结读写到电脑工作目录
                  <workdir>/notes/ 下 (默认 Podman 电脑), 否则落到本地 base_dir.
        time_manager: 共享 TimeEventBus (可选). 提供后笔记支持"提醒时间":
                  write_note 填入 remind_tick → 到点向该角色发送提醒事件
                  (笔记与定时任务统一为"笔记"概念, 提醒 = 带时间的笔记).
    """

    def __init__(self, base_dir: str = "./data/notes", role_id: str = "",
                 computer: Any = None, time_manager: Any = None):
        self._base = Path(base_dir)
        self.role_id = role_id
        self._computer = computer
        self._time_manager = time_manager
        self._local_dir = self._base / (role_id or "shared")
        self._local_dir.mkdir(parents=True, exist_ok=True)

    # ── 路径工具 ──────────────────────────────────────────

    @staticmethod
    def _sanitize_title(title: str) -> str:
        """清洗标题为合法文件名. 非法字符替换为下划线.

        除常规文件名非法字符外, 还替换 shell 元字符 (单引号/反引号/$/分号/&):
        标题会拼进电脑端 shell 命令, 不转义可被 LLM 注入任意命令 (High-3).
        """
        # 非 raw 字符串: \\s 是正则空白, \u0060 是反引号, \\\\ 是反斜杠
        cleaned = re.sub("[\\\\/:*?\"<>|#%\\s'\u0060$;&]+", "_", title.strip())
        return cleaned or "untitled"

    @property
    def _dir(self) -> Path:
        """当前使用的目录 (电脑 workdir/notes 或本地)."""
        if self._computer is not None:
            return Path(self._computer.workdir) / "notes"
        return self._local_dir

    def _note_path(self, title: str) -> str:
        """笔记路径 (字符串, 供 computer 文件接口使用)."""
        return str(self._dir / f"{self._sanitize_title(title)}.md")

    def _summary_path(self, day: int) -> str:
        return str(self._dir / f"_summary_day_{day}.md")

    # ── 底层读写 (走电脑或本地) ──────────────────────────

    def _write(self, path: str, content: str) -> None:
        if self._computer is not None:
            result = self._computer.write_file(path, content)
            # Medium-6 修复: 电脑写入失败 (电脑未开机/命令失败) 必须显式抛出,
            # 否则上层会向 LLM 谎报"已保存"而数据实际没落盘
            if isinstance(result, str) and (result.startswith("错误:")
                                            or result.startswith("[exit ")):
                raise IOError(f"电脑写入失败: {result}")
        else:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    def _read(self, path: str) -> Optional[str]:
        if self._computer is not None:
            r = self._computer.read_file(path)
            if r.startswith("文件不存在") or r.startswith("错误:"):
                return None
            return r
        p = Path(path)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def _list_md_files(self) -> list[Path]:
        """列出 notes 目录下所有 .md 文件 (仅文件名, 按名排序)."""
        if self._computer is not None:
            listing = self._computer.list_dir(str(self._dir))
            names = []
            for line in listing.splitlines():
                # ls 输出取最后一列文件名
                name = line.split()[-1] if line.split() else ""
                if name.endswith(".md"):
                    names.append(Path(name))
            return sorted(names)
        if not self._dir.exists():
            return []
        return sorted(p for p in self._dir.glob("*.md"))

    # ── 笔记操作 ──────────────────────────────────────────

    # ── 提醒 (笔记 = 任务统一概念: 带提醒时间的笔记到点发事件) ──

    def _schedule_reminder(self, title: str, tick: int,
                           day: Optional[int] = None) -> Any:
        """注册笔记提醒: 到指定 Tick 向本角色发送提醒事件 (复用定时任务机制).

        参数:
            title: 笔记标题 (提醒事件描述与 payload 携带).
            tick:  目标 Tick (0~60, 一天内).
            day:   触发天 (默认当天).

        返回:
            ScheduledTask 实例.

        异常:
            ValueError: 未绑定 TimeEventBus 或 tick 超范围.
        """
        if self._time_manager is None:
            raise ValueError(
                "当前笔记存储未绑定 TimeEventBus, 无法注册提醒 "
                "(请通过 AgentRole.note_store 使用)")
        return self._time_manager.schedule_task(
            description=f"[笔记提醒] {title}",
            owner_role=self.role_id,
            target_tick=int(tick),
            day=day,
            payload={"note_title": title},
        )

    def _cancel_reminder(self, title: str) -> bool:
        """取消该标题笔记的提醒 (编辑覆盖/删除笔记时调用)."""
        if self._time_manager is None:
            return False
        removed = False
        for t in self._time_manager.list_tasks(owner_role=self.role_id):
            if t.payload.get("note_title") == title:
                self._time_manager.cancel_task(t.task_id)
                removed = True
        return removed

    def get_reminder(self, title: str) -> Optional[dict[str, int]]:
        """查询笔记的提醒信息 (未触发).

        参数:
            title: 笔记标题.

        返回:
            {"day": 第几天, "tick": 目标Tick}; 无提醒返回 None.
        """
        if self._time_manager is None:
            return None
        for t in self._time_manager.list_tasks(owner_role=self.role_id):
            if t.payload.get("note_title") == title:
                return {"day": t.day, "tick": t.target_tick}
        return None

    def write_note(self, title: str, content: str,
                   remind_tick: Optional[int] = None,
                   remind_day: Optional[int] = None) -> str:
        """写笔记. 已存在则覆盖.

        笔记与定时任务已统一: 填入 remind_tick (可选) 后, 到指定 Tick 系统
        会像任务一样向本角色发送提醒事件 (提醒 = 带时间的笔记).

        参数:
            title:      笔记标题
            content:    笔记内容
            remind_tick: 提醒 Tick (可选, 0~60 一天内; None = 普通笔记)
            remind_day:  提醒触发天 (可选, 默认当天)

        返回:
            保存路径.

        异常:
            ValueError: 填了 remind_tick 但未绑定 TimeEventBus / tick 超范围.
        """
        path = self._note_path(title)
        self._write(path, content)
        if remind_tick is not None:
            task = self._schedule_reminder(title, remind_tick, remind_day)
            logger.info("[%s] 笔记已写入并设置提醒: %s (第 %d 天 Tick %d)",
                        self.role_id, Path(path).name, task.day, task.target_tick)
        else:
            logger.info("[%s] 笔记已写入: %s", self.role_id, Path(path).name)
        return path

    def edit_note(self, title: str, content: str,
                  remind_tick: Optional[int] = None,
                  remind_day: Optional[int] = None) -> str:
        """编辑已有笔记 (覆盖内容). 不存在则创建.

        提供 remind_tick 时重置提醒 (旧提醒取消, 注册新提醒);
        不提供 remind_tick 则保持原提醒不变.

        参数:
            title:      笔记标题
            content:    新内容
            remind_tick: 新的提醒 Tick (可选; 提供则重置提醒)
            remind_day:  新的提醒触发天 (可选, 默认当天)

        返回:
            保存路径.
        """
        path = self._note_path(title)
        self._write(path, content)
        if remind_tick is not None:
            self._cancel_reminder(title)
            task = self._schedule_reminder(title, remind_tick, remind_day)
            logger.info("[%s] 笔记已编辑并重置提醒: %s (第 %d 天 Tick %d)",
                        self.role_id, Path(path).name, task.day, task.target_tick)
        else:
            logger.info("[%s] 笔记已编辑: %s", self.role_id, Path(path).name)
        return path

    def list_notes(self) -> list[str]:
        """列出所有笔记标题 (不含每日总结). 按文件名排序.

        返回:
            标题字符串列表.
        """
        titles = []
        for p in self._list_md_files():
            if p.name.startswith("_summary_"):
                continue  # 跳过总结文件
            titles.append(p.stem)
        return titles

    def read_note(self, title: str) -> Optional[str]:
        """读取笔记内容.

        参数:
            title: 笔记标题

        返回:
            内容字符串, 不存在返回 None.
        """
        path = self._note_path(title)
        return self._read(path)

    def delete_note(self, title: str) -> bool:
        """删除笔记 (真实删除文件 + 取消关联提醒). 返回是否删除成功."""
        # 先取消提醒 (笔记与任务统一: 删笔记即取消其定时提醒)
        self._cancel_reminder(title)
        path = self._note_path(title)
        if self._read(path) is None:
            return False
        if self._computer is not None:
            result = self._computer.delete_file(path)
            if isinstance(result, str) and result.startswith("错误:"):
                logger.warning("[%s] 删除笔记失败: %s", self.role_id, result)
                return False
        else:
            p = Path(path)
            if p.exists():
                p.unlink()
        logger.info("[%s] 笔记已删除: %s", self.role_id, Path(path).name)
        return True

    # ── 每日总结 (作息系统, 按天序号存储) ─────────────────

    def save_summary(self, content: str, day: Optional[int] = None) -> str:
        """保存某一天的总结.

        参数:
            content: 总结内容
            day:     第几天 (默认 1)

        返回:
            保存路径.
        """
        d = day or 1
        path = self._summary_path(d)
        self._write(path, content)
        logger.info("[%s] 第 %d 天总结已保存: %s", self.role_id, d, Path(path).name)
        return path

    def get_summary(self, day: Optional[int] = None) -> Optional[str]:
        """读取指定天的总结.

        参数:
            day: 第几天 (默认 1)

        返回:
            总结内容, 不存在返回 None.
        """
        d = day or 1
        return self._read(self._summary_path(d))

    def get_latest_summary(self, before_day: Optional[int] = None) -> Optional[str]:
        """读取最近一次总结 (用于下一天冷启动).

        参数:
            before_day: 截止天数 (只找严格早于该天的总结, 默认不限)

        返回:
            最近总结内容, 没有则返回 None.
        """
        # 按天数值降序 (文件名 _summary_day_<N>.md 字典序会排错: day_9 > day_10)
        candidates = []
        for p in self._list_md_files():
            if not p.name.startswith("_summary_day_"):
                continue
            try:
                d = int(p.name[len("_summary_day_"):-len(".md")])
            except ValueError:
                continue
            candidates.append((d, p))
        candidates.sort(key=lambda x: x[0], reverse=True)
        for d, p in candidates:
            if before_day is None or d < before_day:
                content = self._read(str(self._dir / p.name))
                if content is not None:
                    return content
        return None
