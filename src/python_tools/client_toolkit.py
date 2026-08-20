"""甲方交流工具类 (Client ToolKit) — 与甲方(用户)实时交流.

包含:
  - talk_to_client: 与甲方交流. 触发时向用户请求文本输入,
                    用户输入的内容会返回给 LLM 继续处理.

用于 CEO 等对外角色: 从用户那里收集需求、确认方案、汇报结果.

用法:
    from src.python_tools.client_toolkit import create_client_toolkit
    ceo.add_toolkit(create_client_toolkit())

接口文档 (模块结构与方法):

模块级函数:
    - create_client_toolkit(): 创建甲方交流工具类.
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_client_toolkit() -> ToolKit:
    """创建甲方交流工具类.

    返回:
        包含 talk_to_client 工具的 ToolKit.
    """

    tk = ToolKit(name="client", description="甲方交流工具类: 与甲方(用户)实时交流")

    def _talk_to_client(args: dict[str, Any]) -> str:
        """与甲方交流: 向用户请求输入, 返回用户输入内容.

        参数:
            args: {"message": 想对甲方说的话 (可选)}

        返回:
            甲方(用户)的回复文本.
        """
        question = args.get("message", "").strip()

        # 控制台交互: 先打印 CEO 说的话, 再阻塞等待用户输入
        if question:
            print(f"\n  {BOLD}[CEO] {question}{RESET}", flush=True)
        else:
            # LLM 未传 message 时也提示一句, 避免控制台静默
            print(f"\n  {BOLD}[CEO] (发来一条消息, 请回复){RESET}", flush=True)
        prompt_text = "  [甲方] 请输入你的回复: "

        try:
            reply = input(prompt_text).strip()
        except EOFError:
            return "错误: 无法获取用户输入 (非交互环境)."
        except KeyboardInterrupt:
            return "错误: 用户中断了输入."

        if not reply:
            return "甲方未输入内容 (空回复)."

        logger.info("甲方回复: %s", reply[:80])
        return f"甲方回复: {reply}"

    tk.add_python_tool(
        name="talk_to_client",
        description=(
            "与甲方(用户)实时交流. 调用此工具会暂停并请求用户输入文本, "
            "用户输入的内容会作为结果返回给你. "
            "用于: 收集需求, 确认方案, 汇报进度, 提出疑问. "
            "如果需要向用户展示信息, 请先通过 message 参数说明."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "想对甲方说的话 (可选, 如提问/汇报内容)"},
            },
        },
        handler=_talk_to_client,
    )

    return tk


# 终端配色 (复用 main.py 风格)
BOLD = "\033[1m"
RESET = "\033[0m"
