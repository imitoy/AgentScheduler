"""Hermes 工具类测试 (命令转译: 新建对话/发送对话).

覆盖:
  - hermes_new_conversation: 从 hermes chat 输出提取对话 id
  - hermes_send: 续接会话发送内容, 返回最终回答
  - 错误处理: 未配置模型 / 未安装 / 非法对话 id
"""

from __future__ import annotations

from src.core.roles import AgentRole, RolePool
from src.python_tools.hermes_toolkit import create_hermes_toolkit


class _FakeComputer:
    """模拟电脑: run_command 返回预设输出, 记录执行的命令."""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.commands: list[str] = []

    def run_command(self, command: str, timeout: int = 60) -> str:
        self.commands.append(command)
        assert timeout >= 300, "hermes 命令应给足等待超时"
        return self.outputs.pop(0) if self.outputs else "(无输出)"


def _mk_toolkit(computer: _FakeComputer):
    pool = RolePool()
    role = AgentRole(name="郭晓东", role_id="tester_1")
    pool.add_role(role)
    tk = create_hermes_toolkit()
    tk._hermes_holder["role"] = role  # type: ignore[attr-defined]
    role._computer = computer  # type: ignore[attr-defined]  # 注入 fake 电脑
    return pool, role, tk


def test_new_conversation_returns_sid(tmp_path, monkeypatch):
    """新建对话: 从 hermes chat 输出提取 session_id 返回."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    fake = _FakeComputer(["20260818_123456_a1b2c3"])  # 管道 grep/awk 后 = 纯 sid
    pool, role, tk = _mk_toolkit(fake)
    r = tk._tools["hermes_new_conversation"].handler({})
    assert "对话已创建" in r
    assert "20260818_123456_a1b2c3" in r
    assert "hermes chat -q" in fake.commands[0]
    pool.shutdown(wait=False)


def test_send_waits_and_returns_result(tmp_path, monkeypatch):
    """发送对话: 续接会话 (-r sid -Q), 返回 Hermes 最终回答."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    fake = _FakeComputer(["完成: 已生成 docker-compose.yml"])
    pool, role, tk = _mk_toolkit(fake)
    r = tk._tools["hermes_send"].handler({
        "conversation_id": "20260818_123456_a1b2c3",
        "content": "生成 docker-compose.yml",
    })
    assert "完成: 已生成 docker-compose.yml" in r
    cmd = fake.commands[0]
    assert "-r 20260818_123456_a1b2c3" in cmd
    assert "-Q" in cmd
    assert "生成 docker-compose.yml" in cmd  # 内容原样传入
    pool.shutdown(wait=False)


def test_send_rejects_bad_sid(tmp_path, monkeypatch):
    """非法对话 id 直接拒绝 (不执行命令)."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    fake = _FakeComputer([])
    pool, role, tk = _mk_toolkit(fake)
    r = tk._tools["hermes_send"].handler({
        "conversation_id": "not-a-sid",
        "content": "hi",
    })
    assert "对话 id 格式非法" in r
    assert fake.commands == []
    pool.shutdown(wait=False)


def test_missing_args_rejected(tmp_path, monkeypatch):
    """缺 conversation_id / content 报错."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    pool, role, tk = _mk_toolkit(_FakeComputer([]))
    assert "必填" in tk._tools["hermes_send"].handler({"content": "hi"})
    assert "必填" in tk._tools["hermes_send"].handler({"conversation_id": "x" * 20})
    pool.shutdown(wait=False)


def test_send_strips_session_noise(tmp_path, monkeypatch):
    """发送对话: 剥离 -Q 输出的杂音行 (↻ Resumed / session_id:), 只留回答."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    fake = _FakeComputer([
        "↻ Resumed session 20260818_123456_a1b2c3 (1 user message, 2 total)\n"
        "\n"
        "session_id: 20260818_123456_a1b2c3\n"
        "最终回答内容",
    ])
    pool, role, tk = _mk_toolkit(fake)
    r = tk._tools["hermes_send"].handler({
        "conversation_id": "20260818_123456_a1b2c3",
        "content": "hi",
    })
    assert r == "最终回答内容"
    assert "session_id" not in r and "↻" not in r
    pool.shutdown(wait=False)


def test_unconfigured_hermes_hint(tmp_path, monkeypatch):
    """未配置模型: 返回配置提示 (不是裸错误)."""
    monkeypatch.setattr("src.core.roles.JOURNAL_DIR", tmp_path / "journals")
    fake = _FakeComputer([
        "  The interactive wizard cannot be used here.\n"
        "  hermes config set model.provider custom\n"
        "  Or set OPENROUTER_API_KEY / OPENAI_API_KEY in your environment.",
    ])
    pool, role, tk = _mk_toolkit(fake)
    r = tk._tools["hermes_new_conversation"].handler({})
    assert "尚未配置模型" in r
    pool.shutdown(wait=False)
