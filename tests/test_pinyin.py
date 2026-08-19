"""角色拼音用户名 + uid 分配测试.

背景: 容器用户名 = 员工名字的汉语拼音 (如 郭晓东 → guoxiaodong),
每员工一个固定 uid (1100+注册序) — 用于公司云盘 (/mnt/drive) 文件权限管理.
"""

from __future__ import annotations

from src.core.pinyin_map import NAME_PINYIN
from src.core.roles import AgentRole, RolePool
from src.core.role_templates import TEMPLATES


def test_all_templates_have_pinyin_username():
    """48 个模板角色都有拼音 username (非空, ASCII 小写)."""
    for key, fn in TEMPLATES.items():
        r = fn()
        assert r.username, f"{key} 缺拼音 username"
        assert r.username.isascii() and r.username.islower(), \
            f"{key} 的 username '{r.username}' 非法"


def test_pinyin_map_covers_default_roster():
    """拼音表覆盖全部默认角色名字."""
    for key, fn in TEMPLATES.items():
        r = fn()
        assert r.name in NAME_PINYIN, f"{key} 名字 '{r.name}' 未收录拼音表"


def test_unknown_name_falls_back_to_role_id():
    """未知名字回退 role_id (ASCII 安全)."""
    r = AgentRole(name="外星人", role_id="alien_1")
    assert r.username == "alien_1"


def test_explicit_username_wins():
    """显式指定 username 优先于拼音表."""
    r = AgentRole(name="郭晓东", role_id="tester_1", username="gxd")
    assert r.username == "gxd"


def test_uid_assigned_by_registration_order(tmp_path, monkeypatch):
    """注册序分配 uid (1100+), 不同角色不同 uid; 显式指定不被覆盖."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    a = AgentRole(name="郭晓东", role_id="tester_1")
    b = AgentRole(name="王建国", role_id="architect")
    pool.add_role(a)
    pool.add_role(b)
    assert a.uid == 1101 and b.uid == 1102
    c = AgentRole(name="林总", role_id="CEO", uid=1200)
    pool.add_role(c)
    assert c.uid == 1200


def test_system_prompt_mentions_cloud_drive(tmp_path, monkeypatch):
    """系统提示词包含公司云盘与 Git 项目管理说明."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    # local 电脑: build_system_prompt → note_store → computer 会触发开机,
    # podman 会走完整容器初始化 (apt+hermes, 数分钟) — 测试用本地模拟
    r = AgentRole(name="郭晓东", role_id="tester_1", computer_kind="local")
    pool.add_role(r)
    prompt = r.build_system_prompt()
    # 云盘
    assert "/mnt/drive" in prompt
    assert "Public" in prompt
    assert "/mnt/drive/郭晓东" in prompt      # 自己的个人目录
    assert "只读" in prompt or "只有你能写入" in prompt
    # Git 项目管理
    assert "Git" in prompt
    assert "git pull" in prompt
    assert "commit" in prompt and "push" in prompt
    assert "合并" in prompt


def test_release_manager_prompt_mentions_project_dir(tmp_path, monkeypatch):
    """版本管理角色提示词: 项目保存在 Public/work/, 新项目直接在其中创建 git 仓库."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool = RolePool()
    r = TEMPLATES["release_manager"]()
    r.computer_kind = "local"  # 避免 build_system_prompt 触发 podman 开机
    pool.add_role(r)
    prompt = r.build_system_prompt()
    assert "/mnt/drive/Public/work/" in prompt
    assert "git init" in prompt
    assert "创建 git 仓库" in prompt or "创建 git 项目" in prompt
