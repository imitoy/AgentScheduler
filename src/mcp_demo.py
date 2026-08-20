#!/usr/bin/env python3
"""MCP 演示脚本 (mcp_demo.py): 演示 MCP 服务器安装与工具调用.

模块级函数:
    - header() / ok() / info(): 终端 UI 打印辅助
    - main(): 装配单个角色电脑 → 安装 MCP filesystem → 调用工具"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
os.environ.setdefault("DEEPSEEK_THINKING", "true")

from src.core.roles import AgentRole, RolePool, Task, Urgency
from src.python_tools.mcp_toolkit import MCPToolLoader

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

BOLD = "\033[1m"; GREEN = "\033[32m"; CYAN = "\033[36m"
BLUE = "\033[34m"; MAGENTA = "\033[35m"; YELLOW = "\033[33m"; RED = "\033[31m"; RESET = "\033[0m"

# 授权目录: 默认项目根 (filesystem 服务器只允许访问授权目录内的文件)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_DIR = os.environ.get("MCP_FS_ALLOWED_DIR", str(PROJECT_ROOT))
FILESYSTEM_PKG = "@modelcontextprotocol/server-filesystem"


def header(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'═' * 62}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'═' * 62}{RESET}\n")


def ok(text: str) -> None:
    print(f"  {GREEN}✓ {text}{RESET}")


def info(text: str) -> None:
    print(f"  {text}")


def main() -> None:
    header("真实 MCP 文件工具 — 官方 filesystem 服务器")

    def call(file_tools, name: str, args: dict) -> str:
        """直接调用工具组内某个工具 (确定性验证用)."""
        td = file_tools.get_tool(name)
        return td.handler(args) if td else f"(工具 {name} 未加载)"

    # ── 1. 加载文件工具 (npx 启动服务器) ────────────────────
    print(f"  授权目录: {ALLOWED_DIR}")
    loader = MCPToolLoader(server_args={FILESYSTEM_PKG: [ALLOWED_DIR]})
    toolkits = loader.load()

    file_tools = toolkits.get("file_ops")
    if file_tools is None:
        print(f"  {RED}✗ file_ops 工具组加载失败 (服务器可能未连接){RESET}")
        loader.close()
        sys.exit(1)

    print(f"\n  {GREEN}file_ops 工具组已加载, 共 {file_tools.tool_count} 个工具:{RESET}")
    for name in file_tools.tool_names:
        print(f"    - {name}")
    print(f"  (来自 {FILESYSTEM_PKG})")

    # ── 2. 直接调用工具验证 (确定性) ────────────────────────
    header("直接调用文件工具 (确定性验证)")

    # 2a. write_file: 写入演示文件
    demo_file = str(PROJECT_ROOT / "data" / "mcp_demo_note.md")
    Path(PROJECT_ROOT / "data").mkdir(exist_ok=True)
    r = call(file_tools, "write_file", {"path": demo_file, "content": "MCP 文件工具演示\n- 第 1 行\n"})
    info(f"write_file → {r[:80]}")
    ok("write_file 写入成功") if "wrote" in r.lower() or "成功" in r else info("write_file 返回: " + r[:60])

    # 2b. read_file: 读回
    r = call(file_tools, "read_file", {"path": demo_file})
    ok(f"read_file 读回: {r.splitlines()[0][:40]}") if "MCP 文件工具演示" in r else info(r[:80])

    # 2c. edit_file: 编辑追加 (返回 diff 格式)
    r = call(file_tools, "edit_file", {"path": demo_file, "edits": [{"oldText": "- 第 1 行", "newText": "- 第 1 行 (已编辑)"}]})
    ok("edit_file 编辑成功 (返回 diff)") if "Index:" in r or "成功" in r else info("edit_file 返回: " + r[:60])

    # 2d. search_files: 查找 (注意: pattern 是文件名 glob, 如 *.py)
    r = call(file_tools, "search_files", {"path": str(PROJECT_ROOT / "src" / "core"), "pattern": "*.py"})
    hit = "time_manager.py" in r
    ok(f"search_files 找到 time_manager.py (glob *.py): {r[:60]}") if hit else info(r[:80])

    # 2e. list_directory: 列目录
    r = call(file_tools, "list_directory", {"path": str(PROJECT_ROOT / "src" / "core")})
    ok("list_directory 列出 core 目录") if "time_manager.py" in r else info(r[:80])

    # 2f. get_file_info
    r = call(file_tools, "get_file_info", {"path": str(PROJECT_ROOT / "README.md")})
    ok("get_file_info 获取 README 信息") if "README.md" in r else info(r[:80])

    # 2g. 清理演示文件 (官方服务器刻意不含 delete_file, 本地清理)
    Path(demo_file).unlink(missing_ok=True)
    ok("演示文件已清理 (本地; 官方 filesystem 服务器无 delete_file, 属安全设计)")

    # ── 3. 注册到角色 + LLM 使用 ─────────────────────────────
    header("角色使用文件工具 (LLM 自主调用)")

    assistant = AgentRole(
        name="赵强",
        role_id="ops",
        title="SRE 工程师",
        personality="冷静果断，先止损再排查。擅长在压力下快速定位问题。",
        skills=["Kubernetes", "Linux", "Python", "日志分析"],
    )
    assistant.add_toolkit(file_tools)
    print(f"  角色 {assistant.name} ({assistant.role_id}) 工具: {assistant.mcp_tool_names}")

    pool = RolePool()
    pool.add_role(assistant)

    def on_done(role: AgentRole, task: Task) -> None:
        status_icon = f"{GREEN}✓{RESET}" if task.status == "done" else f"{RED}✗{RESET}"
        print(f"\n  {BLUE}[{role.role_id}]{RESET} {status_icon} done ({task.tokens_consumed}t)")
        print(f"  {MAGENTA}→{RESET} {task.result[:400]}")

    assistant.on_task_done = on_done
    pool.start()

    pool.assign_task("ops", Task(
        urgency=Urgency.HIGH,
        description=(
            "请使用文件工具完成以下工作:\n"
            "1. 用 read_file 读取 README.md 的前几行, 了解项目是什么\n"
            "2. 在 data/ 目录下用 write_file 创建 ops_report.md, 记录项目的核心功能\n"
            "3. 用 search_files (pattern 是文件名 glob) 在 src/core/ 下查找 *.py 文件, 找出含 AgentSystem 类的文件\n"
            "完成后用一句话汇报结果"
        ),
    ))

    time.sleep(60)  # 等 LLM 完成 (npx 服务器已连, 工具调用较快)

    # ── 4. 收尾 ─────────────────────────────────────────────
    pool.shutdown()
    loader.close()
    print(f"\n{BOLD}{GREEN}MCP 文件工具演示完成 ✓{RESET}\n")


if __name__ == "__main__":
    main()
