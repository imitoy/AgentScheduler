"""Hermes 工具类 (Hermes ToolKit) — 调用角色电脑上安装的 Hermes Agent.

每台员工电脑 (容器) 都安装了独立的 Hermes Agent (curl -fsSL .../install.sh),
通过命令行对话接口 (hermes chat) 与电脑上的 Hermes 交互:

  - hermes_new_conversation: 新建对话 → 返回对话 id (session_id)
      `hermes chat -q "<初始内容>"` → 从输出提取 `hermes --resume <sid>`
  - hermes_send: 向指定对话发送内容, 同步等待 Hermes 跑完 (含全部工具
      调用) 拿到最终回答再返回
      `hermes chat -q "<内容>" -r <sid> -Q`  (阻塞到输出最终回答)

命令以员工用户身份在容器内执行 (run_command), 等待超时 600s
(Hermes 长任务可能跑数分钟, 与文档 6.4 后台长任务一致).

用法:
    from src.python_tools.hermes_toolkit import create_hermes_toolkit
    role.add_toolkit(create_hermes_toolkit())   # binder 自动绑定 role (提供电脑)

接口文档 (模块结构与方法):

模块级函数:
    - create_hermes_toolkit(): 创建 Hermes 工具类.
    - bind_hermes_to_toolkit(): 将角色绑定到 Hermes 工具类 (由 AgentRole.add_toolkit 内部调用).
"""
from __future__ import annotations

import logging
import re
import shlex
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)

# Hermes 对话命令等待超时 (秒): 发送对话后阻塞等待最终回答,
# Hermes 可能跑多个工具调用, 给足时间 (LLM 输出时间不可预测)
HERMES_TIMEOUT = 600

# 新建对话初始内容 (无实质任务, 仅用于建立会话拿到 session_id)
_INIT_PROMPT = "你好，开始一个新的对话。"
# 从 hermes chat 输出提取 session_id (文档 6.2 实测格式)
_SID_RE = re.compile(r"hermes --resume ([0-9a-f_]+)")
# session_id 本身格式: 十六进制 + 下划线 (如 20260818_123456_a1b2c3)
_SID_ONLY_RE = re.compile(r"^[0-9a-f_]+$")


def create_hermes_toolkit() -> ToolKit:
    """创建 Hermes 工具类.

    返回:
        包含 hermes_new_conversation / hermes_send 的 ToolKit.
    """

    tk = ToolKit(name="hermes", description="Hermes 工具类: 调用电脑上的 Hermes Agent")

    # 工具类持有 role 引用 (由 AgentRole.add_toolkit 注入, 提供个人电脑)
    tk._hermes_holder = {"role": None}  # type: ignore[attr-defined]

    def _get_role() -> Any:
        return getattr(tk, "_hermes_holder", {}).get("role")

    def _computer() -> Any:
        role = _get_role()
        if role is None:
            raise RuntimeError("Hermes 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return role.computer

    def _hermes_new_conversation(args: dict[str, Any]) -> str:
        """新建一个 Hermes 对话, 返回对话 id (后续用 hermes_send 续接)."""
        try:
            comp = _computer()
        except RuntimeError as exc:
            return f"错误: {exc}"
        # 建会话: 输出含 "hermes --resume <sid>" 行 (文档 6.2).
        # 不用管道 grep (会过滤掉 stderr 错误文本), 完整输出拿回后在
        # Python 端提取 sid / 检测错误; 2>&1 保留 stderr 供诊断
        cmd = (
            f"hermes chat -q {shlex.quote(_INIT_PROMPT)} 2>&1 | tail -40"
        )
        out = comp.run_command(cmd, timeout=HERMES_TIMEOUT)
        if out.startswith(("[exit", "错误")):
            return _error_hint(out)
        m = _SID_RE.search(out)
        if m:
            sid = m.group(1)
            return f"对话已创建, 对话 id: {sid} (用 hermes_send 发送内容)"
        # 提取失败: 未配置模型 (无 key) / hermes 未安装 / 其他错误
        return _error_hint(out or "(无输出)")

    def _hermes_send(args: dict[str, Any]) -> str:
        """向指定对话发送内容, 同步等待 Hermes 返回全部结果后再返回."""
        cid = (args.get("conversation_id") or "").strip()
        content = args.get("content", "")
        if not cid:
            return "错误: 'conversation_id' (对话 id) 为必填参数."
        if not content:
            return "错误: 'content' (发送内容) 为必填参数."
        if not _SID_ONLY_RE.fullmatch(cid):
            return f"错误: 对话 id 格式非法: '{cid}' (应为 hermes_new_conversation 返回的 id)"
        try:
            comp = _computer()
        except RuntimeError as exc:
            return f"错误: {exc}"
        # 续接会话发送内容: -r <sid> -Q 静默输出最终回答;
        # 命令阻塞到 Hermes 全部跑完 (含工具调用), 退出码 0 = 成功.
        # 2>&1 保留 stderr (失败时输出错误提示供诊断)
        cmd = (
            f"hermes chat -q {shlex.quote(content)} "
            f"-r {shlex.quote(cid)} -Q 2>&1"
        )
        out = comp.run_command(cmd, timeout=HERMES_TIMEOUT, max_chars=100_000)
        if out.startswith(("[exit", "错误")):
            return _error_hint(out)
        if not out.strip():
            return "(Hermes 未返回内容)"
        # 剥离 -Q 输出的杂音行 (↻ Resumed session... / session_id: ...),
        # 只留 Hermes 的最终回答
        lines = [ln for ln in out.splitlines() if ln.strip()]
        cleaned = [ln for ln in lines
                   if not ln.startswith("↻")
                   and not ln.startswith("session_id:")]
        return "\n".join(cleaned) if cleaned else out.strip()

    def _error_hint(raw: str) -> str:
        """把 hermes 错误转成给角色的可读提示 (未配置/未安装常见)."""
        text = raw[:300]
        if "Configure Hermes" in text or "wizard" in text.lower() \
                or "model.provider" in text:
            return (f"错误: 电脑上的 Hermes 尚未配置模型, 无法对话: "
                    f"({text[:100].strip()}) 需要先在电脑上配置模型/API key")
        if "not found" in text.lower() or "No such file" in text:
            return f"错误: 电脑上未安装 Hermes Agent: {text[:120]}"
        if not text.strip():
            return "错误: Hermes 调用失败 (无输出)"
        return f"错误: Hermes 调用失败: {text}"

    tk.add_python_tool(name="hermes_new_conversation", description=(
        "在电脑上的 Hermes Agent 中新建一个对话, 返回对话 id. "
        "之后用 hermes_send 向该对话发送内容并拿回复."),
        input_schema={"type": "object", "properties": {}},
        handler=_hermes_new_conversation)
    tk.add_python_tool(name="hermes_send", description=(
        "向指定 Hermes 对话 (conversation_id) 发送一段内容, "
        "然后同步等待 Hermes 完成全部处理 (含工具调用) 并返回最终结果. "
        "适合委托电脑上的 Hermes 独立完成一个子任务."),
        input_schema={"type": "object", "properties": {
            "conversation_id": {"type": "string",
                                "description": "对话 id (hermes_new_conversation 返回)"},
            "content": {"type": "string", "description": "要发送的内容/任务描述"},
        }, "required": ["conversation_id", "content"]},
        handler=_hermes_send)

    return tk


def bind_hermes_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将角色绑定到 Hermes 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: hermes 工具类实例.
        role:    绑定的 AgentRole (提供个人电脑, 命令在容器内以员工身份执行).
    """
    toolkit._hermes_holder["role"] = role  # type: ignore[attr-defined]
