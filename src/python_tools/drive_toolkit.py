"""企业云盘工具类 (Drive ToolKit) — 共享文件夹 /mnt/drive 文件管理.

企业云盘 = 挂载到每台员工电脑 /mnt/drive/ 的共享文件夹 (宿主机 data/drive/):
  - Public/       公用共享文件夹 (777, 所有员工可读写, 属主 = CEO 的用户)
  - <员工名字>/   各员工个人目录 (755, 属主 = 对应员工: 本人可写, 他人只读)
  - 权限由 Linux 文件系统管理: 容器内用户名 = 员工拼音, 不同员工不同 uid,
    写操作是否允许由内核按文件权限判定 (无需应用层 ACL 表)

包含:
  - drive_list:   列出目录 (空 = 云盘根, 显示 Public + 各员工目录)
  - drive_upload: 上传文件 (写入本人目录 / Public; 他人目录默认只读会被拒)
  - drive_read:   读取文件 (所有员工目录默认可读)
  - drive_delete: 删除 (需写权限: 本人目录 / Public / 被授权的目录)
  - drive_rename: 重命名 (需写权限)
  - drive_copy:   复制 (读源 + 写目标, 两处权限分别判定)
  - drive_move:   移动 (源/目标都要写权限)
  - drive_search: 全盘查找文件名
  - drive_set_permission: 设置本人目录权限 (755=他人只读 / 777=全部可写)

路径格式: "Public/公告.md" 或 "郭晓东/设计稿.md" (相对云盘根 /mnt/drive).

用法:
    from src.python_tools.drive_toolkit import create_drive_toolkit
    role.add_toolkit(create_drive_toolkit())   # binder 自动绑定 role (提供电脑)
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)

# 路径安全: 只允许 角色名/Public 开头, 禁止 .. / 绝对路径 / 隐藏 ACL 文件
_PATH_OK = re.compile(r"^(Public|[\u4e00-\u9fff\w]+)(/[^/]+)*$")


def create_drive_toolkit() -> ToolKit:
    """创建企业云盘工具类 (通过角色电脑操作 /mnt/drive, 权限由文件系统判定).

    返回:
        包含 drive_* 系列工具的 ToolKit.
    """

    tk = ToolKit(name="drive", description="企业云盘工具类: 公共资源文件管理")

    # 工具类持有 role 引用 (由 AgentRole.add_toolkit 注入, 提供个人电脑)
    tk._drive_holder = {"role": None}  # type: ignore[attr-defined]

    def _get_role() -> Any:
        return getattr(tk, "_drive_holder", {}).get("role")

    def _computer() -> Any:
        role = _get_role()
        if role is None:
            raise RuntimeError("云盘工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return role.computer

    def _check_path(path: str) -> str:
        """校验云盘相对路径 → 完整路径 (防穿越)."""
        path = (path or "").strip().lstrip("/")
        if not path or not _PATH_OK.match(path):
            raise ValueError(
                f"非法路径: '{path}' (必须以 Public 或角色目录名开头, 不允许 '..')")
        return path

    def _full(path: str) -> str:
        return f"{_computer().drive_root}/{_check_path(path)}"

    def _err(exc: Exception) -> str:
        return f"错误: {exc}"

    def _drive_list(args: dict[str, Any]) -> str:
        """列出目录内容 (空路径 = 云盘根: Public + 各员工目录)."""
        try:
            path = (args.get("path") or "").strip()
            if not path:
                # 根目录: 列出 Public + 各角色目录
                out = _computer().run_command(
                    f"ls -1 {shlex.quote(_computer().drive_root)}")
                if out.startswith("[exit") or out.startswith("错误"):
                    return out
                lines = [ln for ln in out.splitlines() if ln]
                return "\n".join(f"- 📁 {ln}" for ln in lines) if lines else "(空)"
            full = _full(path)
            out = _computer().run_command(
                f"ls -la {shlex.quote(full)}")
            if out.startswith("[exit") or out.startswith("错误"):
                return out
            # 解析 ls -la 输出为友好条目
            entries = []
            for ln in out.splitlines():
                m = re.match(r"^([d\-])[r\-][w\-][x\-]\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(.+)$", ln)
                if m:
                    typ = "📁" if m.group(1) == "d" else "📄"
                    entries.append(f"- {typ} {m.group(2)}")
            return "\n".join(entries) if entries else out
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_upload(args: dict[str, Any]) -> str:
        """上传文件到云盘 (需对目标目录有写权限)."""
        path = (args.get("path") or "").strip()
        content = args.get("content", "")
        if not path:
            return "错误: 'path' (目标路径) 为必填参数."
        try:
            full = _full(path)
            return _computer().write_file(full, content)
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_read(args: dict[str, Any]) -> str:
        """读取云盘文件 (所有员工目录默认可读)."""
        path = (args.get("path") or "").strip()
        if not path:
            return "错误: 'path' (文件路径) 为必填参数."
        try:
            return _computer().read_file(_full(path))
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_delete(args: dict[str, Any]) -> str:
        """删除云盘文件/目录 (需写权限)."""
        path = (args.get("path") or "").strip()
        if not path:
            return "错误: 'path' (目标路径) 为必填参数."
        try:
            full = _full(path)
            return _computer().run_command(f"rm -rf {shlex.quote(full)} && echo '已删除: {path}'")
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_rename(args: dict[str, Any]) -> str:
        """重命名云盘文件/目录 (需写权限)."""
        path = (args.get("path") or "").strip()
        new_name = (args.get("new_name") or "").strip()
        if not path or not new_name:
            return "错误: 'path' 和 'new_name' 为必填参数."
        if "/" in new_name:
            return "错误: 'new_name' 只能是文件名 (不含路径)."
        try:
            full = _full(path)
            new_full = str(full.rsplit("/", 1)[0]) + "/" + new_name
            return _computer().run_command(
                f"mv {shlex.quote(full)} {shlex.quote(new_full)} "
                f"&& echo '已重命名: {path} → {new_name}'")
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_copy(args: dict[str, Any]) -> str:
        """复制云盘文件/目录 (读源 + 写目标)."""
        src = (args.get("src") or "").strip()
        dst = (args.get("dst") or "").strip()
        if not src or not dst:
            return "错误: 'src' 和 'dst' 为必填参数."
        try:
            src_full, dst_full = _full(src), _full(dst)
            dst_dir = shlex.quote(str(dst_full).rsplit("/", 1)[0])
            return _computer().run_command(
                f"mkdir -p {dst_dir} && "
                f"cp -r {shlex.quote(src_full)} {shlex.quote(dst_full)} "
                f"&& echo '已复制: {src} → {dst}'")
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_move(args: dict[str, Any]) -> str:
        """移动云盘文件/目录 (源删除 + 目标写入)."""
        src = (args.get("src") or "").strip()
        dst = (args.get("dst") or "").strip()
        if not src or not dst:
            return "错误: 'src' 和 'dst' 为必填参数."
        try:
            src_full, dst_full = _full(src), _full(dst)
            dst_dir = shlex.quote(str(dst_full).rsplit("/", 1)[0])
            return _computer().run_command(
                f"mkdir -p {dst_dir} && "
                f"mv {shlex.quote(src_full)} {shlex.quote(dst_full)} "
                f"&& echo '已移动: {src} → {dst}'")
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    def _drive_search(args: dict[str, Any]) -> str:
        """全盘查找云盘文件名."""
        keyword = (args.get("keyword") or "").strip()
        if not keyword:
            return "错误: 'keyword' (关键词) 为必填参数."
        try:
            root = shlex.quote(_computer().drive_root)
            kw = shlex.quote("*" + keyword + "*")
            out = _computer().run_command(
                f"find {root} -name {kw} -type f 2>/dev/null | sort")
            if out.startswith("[exit") or out.startswith("错误"):
                return out
            lines = [ln for ln in out.splitlines() if ln]
            if not lines:
                return f"(未找到包含 '{keyword}' 的文件)"
            # 去掉 drive_root 前缀, 显示相对路径
            prefix = _computer().drive_root.rstrip("/") + "/"
            return "\n".join(f"- {ln[len(prefix):] if ln.startswith(prefix) else ln}"
                             for ln in lines)
        except RuntimeError as exc:
            return _err(exc)

    def _drive_set_permission(args: dict[str, Any]) -> str:
        """设置本人云盘目录权限: true=777 全部可写 / false=755 他人只读.

        Linux 文件权限模型: 个人目录默认 755 (owner 可写, 他人只读);
        需要协作时可将自己目录改为 777 (Public 之外的共享空间).
        """
        target = (args.get("target_name") or "").strip()
        writable = args.get("writable")
        if not target:
            return "错误: 'target_name' (角色名) 为必填参数."
        if writable is None:
            return "错误: 'writable' (是否可写) 为必填参数 (true/false)."
        if isinstance(writable, str):
            writable = writable.lower() in ("1", "true", "yes", "on")
        try:
            role = _get_role()
            if role is None:
                return "错误: 云盘工具类尚未绑定角色."
            # 只能设置自己名字的目录 (Linux 权限: owner 才能 chmod)
            if target != role.name:
                return (f"错误: 只能设置自己目录的权限 (你是 {role.name}), "
                        f"不能改 '{target}' 的目录.")
            full = _full(target)
            mode = "777" if writable else "755"
            return _computer().run_command(
                f"chmod {mode} {shlex.quote(full)} && "
                f"echo '已设置 {target}/ 权限为 {mode} ('"
                f"{'全部可写' if writable else '本人可写, 他人只读'}" f"')'")
        except (ValueError, RuntimeError) as exc:
            return _err(exc)

    tk.add_python_tool(name="drive_list", description=(
        "列出企业云盘目录内容. path 为空 = 云盘根 (Public 公用 + 各员工目录); "
        "path 填 'Public/子目录' 或 '角色名/子目录' 查看具体目录. "
        "所有员工目录默认可读可看."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径 (可选, 空 = 云盘根)"},
        }}, handler=_drive_list)
    tk.add_python_tool(name="drive_upload", description=(
        "上传文件到企业云盘. path 必须是本人目录 (自己名字) 或 Public 公用目录下; "
        "其他员工目录默认只读, 写入会被拒绝. 例: drive_upload('Public/周报.md', '内容')."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "目标路径 (角色名或Public/子路径/文件名)"},
            "content": {"type": "string", "description": "文件内容"},
        }, "required": ["path", "content"]}, handler=_drive_upload)
    tk.add_python_tool(name="drive_read", description=(
        "读取企业云盘文件内容 (所有员工目录默认可读)."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
        }, "required": ["path"]}, handler=_drive_read)
    tk.add_python_tool(name="drive_delete", description=(
        "删除企业云盘文件或目录. 需要对该目录有写权限 (本人目录 / Public)."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "目标路径"},
        }, "required": ["path"]}, handler=_drive_delete)
    tk.add_python_tool(name="drive_rename", description=(
        "重命名企业云盘文件/目录 (需写权限)."),
        input_schema={"type": "object", "properties": {
            "path": {"type": "string", "description": "源路径"},
            "new_name": {"type": "string", "description": "新文件名 (不含路径)"},
        }, "required": ["path", "new_name"]}, handler=_drive_rename)
    tk.add_python_tool(name="drive_copy", description=(
        "复制企业云盘文件/目录: 源可读即可, 目标处需要写权限. "
        "例: drive_copy('Public/方案.md', '郭晓东/方案副本.md')."),
        input_schema={"type": "object", "properties": {
            "src": {"type": "string", "description": "源路径"},
            "dst": {"type": "string", "description": "目标路径"},
        }, "required": ["src", "dst"]}, handler=_drive_copy)
    tk.add_python_tool(name="drive_move", description=(
        "移动企业云盘文件/目录 (源删除+目标写入, 两处都要写权限)."),
        input_schema={"type": "object", "properties": {
            "src": {"type": "string", "description": "源路径"},
            "dst": {"type": "string", "description": "目标路径"},
        }, "required": ["src", "dst"]}, handler=_drive_move)
    tk.add_python_tool(name="drive_search", description=(
        "在全盘搜索文件名包含关键词的文件."),
        input_schema={"type": "object", "properties": {
            "keyword": {"type": "string", "description": "文件名关键词"},
        }, "required": ["keyword"]}, handler=_drive_search)
    tk.add_python_tool(name="drive_set_permission", description=(
        "设置本人云盘目录权限: writable=true → 777 (全部员工可写, 协作空间), "
        "false → 755 (本人可写, 其他员工只读, 默认). 只能设置自己名字的目录."),
        input_schema={"type": "object", "properties": {
            "target_name": {"type": "string", "description": "你的角色名 (只能设置自己的目录)"},
            "writable": {"type": "boolean", "description": "true=777全部可写, false=755只读"},
        }, "required": ["target_name", "writable"]}, handler=_drive_set_permission)

    return tk


def bind_drive_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将角色绑定到云盘工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: drive 工具类实例.
        role:    绑定的 AgentRole (提供个人电脑, 文件权限按容器内用户判定).
    """
    toolkit._drive_holder["role"] = role  # type: ignore[attr-defined]
