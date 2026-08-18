"""企业云盘 (共享文件夹 /mnt/drive) + 拼音用户名 + drive 工具测试.

覆盖:
  - 拼音映射: 48 个模板角色都有拼音 username (非空 ASCII), 未知名字回退
  - uid 分配: 注册序号 1100+ (每个员工固定 uid)
  - drive 工具 (LocalComputer 降级环境): 上传/读取/列表/删除/重命名/
    复制/移动/查找/设权限 + 路径安全
  - talk attachment: 无效附件拒绝, 有效附件随消息携带
"""

from __future__ import annotations

import pytest

from src.core.role_templates import TEMPLATES
from src.core.pinyin_map import NAME_PINYIN
from src.core.roles import AgentRole, RolePool
from src.python_tools.drive_toolkit import create_drive_toolkit
from src.python_tools.talk_toolkit import create_talk_toolkit


# ── 拼音用户名 ────────────────────────────────────────────


def test_all_templates_have_pinyin_username():
    """48 个模板角色都有拼音 username (非空, ASCII 小写)."""
    for key, fn in TEMPLATES.items():
        r = fn()
        assert r.username, f"{key} 缺拼音 username"
        assert r.username.isascii() and r.username.islower(), \
            f"{key} 的 username '{r.username}' 非法"


def test_unknown_name_falls_back_to_role_id():
    """未知名字回退 role_id (ASCII 安全)."""
    r = AgentRole(name="外星人", role_id="alien_1")
    assert r.username == "alien_1"


def test_explicit_username_wins():
    """显式指定 username 优先于拼音表."""
    r = AgentRole(name="郭晓东", role_id="tester_1", username="gxd")
    assert r.username == "gxd"


def test_pinyin_map_covers_default_roster():
    """拼音表覆盖全部默认角色名字."""
    for key, fn in TEMPLATES.items():
        r = fn()
        assert r.name in NAME_PINYIN, f"{key} 名字 '{r.name}' 未收录拼音表"


# ── uid 分配 ──────────────────────────────────────────────


def test_uid_assigned_by_registration_order(tmp_path, monkeypatch):
    """注册序分配 uid (1100+), 不同角色不同 uid."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    a = AgentRole(name="郭晓东", role_id="tester_1")
    b = AgentRole(name="王建国", role_id="architect")
    pool.add_role(a)
    pool.add_role(b)
    assert a.uid == 1101 and b.uid == 1102
    # 显式指定 uid 不被覆盖
    c = AgentRole(name="林总", role_id="CEO", uid=1200)
    pool.add_role(c)
    assert c.uid == 1200


# ── drive 工具 (LocalComputer 降级) ──────────────────────


def _mk_role(tmp_path, monkeypatch, name: str, rid: str, kind="local"):
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    role = AgentRole(name=name, role_id=rid, computer_kind=kind)
    pool.add_role(role)
    tk = create_drive_toolkit()
    tk._drive_holder["role"] = role  # type: ignore[attr-defined]
    return pool, role, tk


def test_drive_upload_read_list(tmp_path, monkeypatch):
    """上传/读取/列表 (本人目录 + Public)."""
    pool, role, tk = _mk_role(tmp_path, monkeypatch, "郭晓东", "tester_1")
    # 上传到自己目录 (返回保存路径)
    r = tk._tools["drive_upload"].handler({"path": "郭晓东/设计稿.md", "content": "内容v1"})
    assert "设计稿.md" in r
    # 读取
    assert "内容v1" in tk._tools["drive_read"].handler({"path": "郭晓东/设计稿.md"})
    # 上传到 Public
    tk._tools["drive_upload"].handler({"path": "Public/公告.md", "content": "公告"})
    # 列表根目录: 显示 Public + 郭晓东
    listing = tk._tools["drive_list"].handler({})
    assert "Public" in listing and "郭晓东" in listing
    pool.shutdown(wait=False)


def test_drive_other_role_readonly(tmp_path, monkeypatch):
    """他人目录默认只读 (Local 降级为宿主机单用户, 权限由容器内 uid 生效)."""
    pool, role_a, tk_a = _mk_role(tmp_path, monkeypatch, "郭晓东", "tester_1")
    tk_a._tools["drive_upload"].handler({"path": "郭晓东/设计稿.md", "content": "秘密"})
    pool2, role_b, tk_b = _mk_role(tmp_path, monkeypatch, "王建国", "architect")
    # 王建国可读郭晓东的文件
    assert "秘密" in tk_b._tools["drive_read"].handler({"path": "郭晓东/设计稿.md"})
    pool.shutdown(wait=False)
    pool2.shutdown(wait=False)


def test_drive_ops_and_path_safety(tmp_path, monkeypatch):
    """重命名/复制/移动/删除/查找 + 路径穿越拒绝."""
    pool, role, tk = _mk_role(tmp_path, monkeypatch, "郭晓东", "tester_1")
    tk._tools["drive_upload"].handler({"path": "郭晓东/a.md", "content": "A"})
    # 重命名
    r = tk._tools["drive_rename"].handler({"path": "郭晓东/a.md", "new_name": "b.md"})
    assert "已重命名" in r
    # 复制到 Public
    tk._tools["drive_copy"].handler({"src": "郭晓东/b.md", "dst": "Public/b副本.md"})
    assert "A" in tk._tools["drive_read"].handler({"path": "Public/b副本.md"})
    # 移动
    tk._tools["drive_move"].handler({"src": "Public/b副本.md", "dst": "Public/归档/b副本.md"})
    assert "A" in tk._tools["drive_read"].handler({"path": "Public/归档/b副本.md"})
    # 查找
    hits = tk._tools["drive_search"].handler({"keyword": "副本"})
    assert "副本" in hits
    # 删除
    tk._tools["drive_delete"].handler({"path": "Public/归档"})
    # 路径穿越拒绝
    assert "非法路径" in tk._tools["drive_upload"].handler(
        {"path": "../秘密.md", "content": "x"})
    assert "非法路径" in tk._tools["drive_upload"].handler(
        {"path": "/绝对.md", "content": "x"})
    pool.shutdown(wait=False)


def test_drive_set_permission_only_self(tmp_path, monkeypatch):
    """drive_set_permission 只能设置自己名字的目录."""
    pool, role, tk = _mk_role(tmp_path, monkeypatch, "郭晓东", "tester_1")
    r_self = tk._tools["drive_set_permission"].handler(
        {"target_name": "郭晓东", "writable": True})
    assert "已设置" in r_self
    r_other = tk._tools["drive_set_permission"].handler(
        {"target_name": "王建国", "writable": True})
    assert "只能设置自己目录" in r_other
    pool.shutdown(wait=False)


# ── talk attachment ───────────────────────────────────────


def test_talk_attachment_validated_and_carried(tmp_path, monkeypatch):
    """talk attachment: 无效附件拒绝; 有效附件随消息携带并提示对方."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    a = AgentRole(name="郭晓东", role_id="tester_1", computer_kind="local")
    b = AgentRole(name="王建国", role_id="architect", computer_kind="local")
    pool.add_role(a)
    pool.add_role(b)
    # 郭晓东上传附件
    a.computer.write_file(f"{a.computer.drive_root}/郭晓东/设计稿.md", "附件内容")
    a.computer.run_command(f"mkdir -p {a.computer.drive_root}/郭晓东")

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
    assert "drive_read" in task.description
    assert task.context.get("attachment") == "郭晓东/设计稿.md"
    pool.shutdown(wait=False)
