"""技能工具类 (Skill ToolKit) — 第三种工具格式: 基于 SKILL.md 的技能包.

与 Python 原生工具、MCP 工具并列的第三种工具来源:
  - 每个技能 = 一个目录, 内含 SKILL.md (frontmatter: name/description + 使用指引)
    以及可选的 scripts/ (脚本), references/ (参考文档), assets/ (模板资源)
  - SkillManager 扫描技能库目录 (默认 data/skills/), 解析所有 SKILL.md 的
    frontmatter (name/description), 把每个技能注册为一个"工具":
    调用该工具 → 返回 SKILL.md 全文 + 相关文件清单, LLM 获得技能指引后
    按步骤行动 (必要时配合 computer.run_command 等执行技能内脚本)
  - 管理工具 (skill_search / skill_list / skill_add / skill_remove /
    skill_my_skills) 与 mcp_manager 对称: LLM 自主搜索/添加/移除技能

技能库来源: https://github.com/anbeime/skill (CC-BY-4.0 技能商店),
clone 后把 skills/ 目录复制到 data/skills/ (gitignored, 不入库).

用法:
    from src.python_tools.skill_toolkit import SkillManager, create_skill_manager_toolkit
    mgr = SkillManager()                      # 默认扫描 data/skills/
    tk = create_skill_manager_toolkit(mgr)    # 打包成 LLM 工具类
    role.add_toolkit(tk)                      # add_toolkit 自动绑定当前角色
    # 或编程式:
    mgr.search_skills("ppt")                  # 搜索
    mgr.add_skill(role, "pptx-generator")     # 给角色添加技能工具

接口文档 (模块结构与方法):

模块级函数:
    - create_skill_manager_toolkit(): 把技能管理操作打包成 LLM 可调用的工具类.
    - bind_role_to_toolkit(): 将当前角色绑定到 skill_manager 工具类 (由 AgentRole.add_toolkit 内部调用).

类与方法:
    SkillInfo:
        - tool_name(): 转成合法工具名 (小写下划线, 空格/连字符 → 下划线).
        - read_skill_md(): 读取 SKILL.md 全文 (不存在返回空串).
        - list_related_files(): 列出技能目录下的相关文件 (scripts/references/assets), 相对路径排序.
    SkillManager:
        - ensure_loaded(): 扫描技能库全部 SKILL.md 并解析 frontmatter. 幂等, 可重复调用.
        - list_available(): 列出技能库中全部技能 (名称 + 简述 + 目录).
        - search_skills(): 按关键词搜索技能 (匹配名称或描述).
        - add_skill(): 为角色安装一个技能工具.
        - remove_skill(): 从角色移除一个技能工具.
        - list_role_skills(): 列出角色已添加的技能工具.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from src.core.tools import ToolDef, ToolKit

logger = logging.getLogger(__name__)

# 默认技能库目录 (相对项目根, gitignored)
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "skills"


class SkillInfo:
    """一个技能包的元信息.

    参数:
        name:        技能名 (frontmatter name, 无则用目录名; 工具名用它)
        description: 技能描述 (frontmatter description, 用于工具描述)
        path:        SKILL.md 所在目录 (绝对路径)
    """

    def __init__(self, name: str, description: str, path: Path):
        self.name = name
        self.description = description
        self.path = path

    def tool_name(self) -> str:
        """转成合法工具名 (小写下划线, 空格/连字符 → 下划线)."""
        n = re.sub(r"[^a-z0-9_]+", "_", self.name.lower()).strip("_")
        return n or "skill"

    def read_skill_md(self) -> str:
        """读取 SKILL.md 全文 (不存在返回空串)."""
        p = self.path / "SKILL.md"
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")

    def list_related_files(self) -> list[str]:
        """列出技能目录下的相关文件 (scripts/references/assets), 相对路径排序."""
        files = []
        for sub in ("scripts", "references", "assets"):
            d = self.path / sub
            if d.exists():
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        files.append(str(p.relative_to(self.path)))
        return files


def _parse_frontmatter(text: str) -> tuple[Optional[str], str]:
    """从 SKILL.md 文本解析 frontmatter 的 name/description.

    支持 4 种变体:
      1. 标准 YAML: --- name: xxx / description: xxx ---
      2. 多行字段 (name 后跟 dependency 等嵌套块) — 只取 name/description 首行
      3. name 带空格/引号 (如 "PDF Processing Pro")
      4. 无 frontmatter (直接 # 标题) — name 返回 None (用目录名兜底)

    参数:
        text: SKILL.md 全文.

    返回:
        (name, description). name 可为 None (调用方用目录名兜底).
    """
    if not text.startswith("---"):
        return None, ""
    end = text.find("\n---", 3)
    if end == -1:
        end = text.find("...", 3)
    fm = text[3:end] if end != -1 else text[3:]
    name = None
    desc = ""
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip("\"'")
        elif line.startswith("description:") and not desc:
            desc = line[len("description:"):].strip().strip("\"'")
            # 多行 description: 若下一行缩进且仍是描述的一部分则续接
            # (简单起见只取首行 — 首行已足够作为工具描述)
    return name, desc


class SkillManager:
    """技能库管理器: 扫描 SKILL.md, 按需把技能注册为角色工具.

    与 MCPManager 对称: 技能池是只读目录, 每个技能 → 一个工具
    (handler 返回 SKILL.md 全文 + 相关文件清单), 角色按需添加/移除.

    参数:
        skills_dir: 技能库根目录 (默认 data/skills/).
    """

    def __init__(self, skills_dir: str | Path | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
        self._skills: dict[str, SkillInfo] = {}   # 技能名 → SkillInfo
        self._role_skills: dict[str, set[str]] = {}  # role_id → {技能名}
        self._loaded = False

    # ── 加载 ──────────────────────────────────────────────

    def ensure_loaded(self) -> dict[str, SkillInfo]:
        """扫描技能库全部 SKILL.md 并解析 frontmatter. 幂等, 可重复调用.

        返回:
            {技能名: SkillInfo}.
        """
        if self._loaded:
            return self._skills
        if not self.skills_dir.exists():
            logger.warning("技能库目录不存在: %s (clone anbeime/skill 后复制 skills/ 过来)",
                           self.skills_dir)
            self._loaded = True
            return self._skills
        for md in sorted(self.skills_dir.rglob("SKILL.md")):
            text = md.read_text(encoding="utf-8")
            name, desc = _parse_frontmatter(text)
            if not name:
                name = md.parent.name  # 目录名兜底
            # 同名冲突: 保留第一个, 后续加序号 (如 article-illustrator → article-illustrator-2)
            base = name
            idx = 2
            while name in self._skills:
                name = f"{base}-{idx}"
                idx += 1
            self._skills[name] = SkillInfo(name=name, description=desc, path=md.parent)
        self._loaded = True
        logger.info("SkillManager: 已加载 %d 个技能 (来自 %s)",
                    len(self._skills), self.skills_dir)
        return self._skills

    # ── 查询 ──────────────────────────────────────────────

    def list_available(self) -> list[dict[str, str]]:
        """列出技能库中全部技能 (名称 + 简述 + 目录)."""
        self.ensure_loaded()
        return [
            {"name": n, "description": (s.description or "")[:120], "path": str(s.path)}
            for n, s in sorted(self._skills.items())
        ]

    def search_skills(self, keyword: str) -> list[dict[str, str]]:
        """按关键词搜索技能 (匹配名称或描述).

        参数:
            keyword: 搜索词, 如 "ppt" / "video" / "pdf".

        返回:
            匹配的技能列表 [{name, description, path}].
        """
        self.ensure_loaded()
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        hits = []
        for name, s in sorted(self._skills.items()):
            haystack = f"{name} {s.description or ''}".lower()
            if kw in haystack:
                hits.append({"name": name, "description": (s.description or "")[:120],
                             "path": str(s.path)})
        return hits

    # ── 角色技能管理 ──────────────────────────────────────

    def add_skill(self, role: Any, skill_name: str) -> str:
        """为角色安装一个技能工具.

        安装语义: 在角色 ToolRegistry 注册一个工具, 工具名 = 技能名,
        描述 = 技能描述, handler 返回 SKILL.md 全文 + 相关文件清单 —
        LLM 调用后获得完整技能指引, 按步骤执行.

        参数:
            role:       AgentRole 实例.
            skill_name: 技能名 (须在技能库中, 用 skill_search 查找).

        返回:
            操作结果说明 (成功/已存在/不存在).
        """
        self.ensure_loaded()
        info = self._skills.get(skill_name)
        if info is None:
            return (f"错误: 技能库中没有名为 '{skill_name}' 的技能. "
                    f"可用 skill_search / skill_list 查看全部技能.")
        role_id = role.role_id
        mine = self._role_skills.setdefault(role_id, set())
        if skill_name in mine:
            return f"技能 '{skill_name}' 已添加给 {role_id}, 无需重复添加."

        # 注册代理 handler → 返回 SKILL.md 全文 + 相关文件清单
        from src.core.tools import ToolRegistry
        if role._tools is None:
            role._tools = ToolRegistry()

        def _make_handler(info_: SkillInfo):
            def handler(args: dict[str, Any]) -> str:
                return _read_skill_content(info_)
            return handler

        role._tools.add_tool(
            name=info.tool_name(),
            description=(info.description or f"技能: {info.name}")[:300],
            input_schema={"type": "object", "properties": {}},
            handler=_make_handler(info),
            source=f"skill:{info.name}",
        )
        mine.add(skill_name)
        logger.info("[%s] 技能工具已添加: %s (来自 %s)", role_id, skill_name, info.path)
        return f"成功: 技能 '{skill_name}' 已安装到 {role_id} ({info.description[:60]}...)"

    def remove_skill(self, role: Any, skill_name: str) -> str:
        """从角色移除一个技能工具.

        参数:
            role:       AgentRole 实例.
            skill_name: 技能名.

        返回:
            操作结果说明 (成功/未添加/不存在).
        """
        role_id = role.role_id
        mine = self._role_skills.get(role_id, set())
        if skill_name not in mine:
            return f"技能 '{skill_name}' 尚未添加给 {role_id}, 无需移除."

        info = self._skills.get(skill_name)
        from src.core.tools import ToolRegistry
        if role._tools is None:
            role._tools = ToolRegistry()
        if info is not None:
            role._tools.remove_tool(info.tool_name())
        mine.discard(skill_name)
        logger.info("[%s] 技能工具已移除: %s", role_id, skill_name)
        return f"成功: 技能 '{skill_name}' 已从 {role_id} 移除."

    def list_role_skills(self, role: Any) -> list[dict[str, str]]:
        """列出角色已添加的技能工具."""
        mine = self._role_skills.get(role.role_id, set())
        result = []
        for n in sorted(mine):
            info = self._skills.get(n)
            result.append({"name": n, "description": (info.description if info else "")[:120]})
        return result


# ── 技能内容读取 (工具 handler 用) ────────────────────────

def _read_skill_content(info: SkillInfo) -> str:
    """拼接技能完整内容: frontmatter 摘要 + SKILL.md 全文 + 相关文件清单."""
    body = info.read_skill_md()
    related = info.list_related_files()
    parts = [
        f"技能: {info.name}",
        f"目录: {info.path}",
        f"描述: {info.description}",
        "",
        "════ SKILL.md 全文 ════",
        body or "(SKILL.md 为空)",
    ]
    if related:
        parts += [
            "",
            "════ 相关文件 (可用 run_command / 文件工具访问) ════",
            *[f"- {p}" for p in related],
        ]
    parts.append("(技能指引结束, 请按上述步骤执行; 需要执行脚本时用个人电脑的 run_command 工具)")
    return "\n".join(parts)


# ── LLM 管理工具 (打包成 tool_call 工具类) ────────────────

def create_skill_manager_toolkit(manager: SkillManager) -> ToolKit:
    """把技能管理操作打包成 LLM 可调用的工具类.

    参数:
        manager: SkillManager 实例 (全局共享).

    返回:
        包含 skill_search / skill_list / skill_add / skill_remove /
        skill_my_skills 的工具类.
    """
    tk = ToolKit(name="skill_manager", description="技能管理: 搜索/添加/移除 SKILL.md 技能工具")
    tk._skill_holder = {"manager": manager, "role": None}  # type: ignore[attr-defined]

    def _role() -> Any:
        r = tk._skill_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("skill_manager 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r

    def _skill_search(args: dict[str, Any]) -> str:
        """搜索可用技能."""
        kw = args.get("keyword", "").strip()
        if not kw:
            return "请提供 keyword 搜索词."
        hits = manager.search_skills(kw)
        if not hits:
            return f"没有匹配 '{kw}' 的技能. 可用 skill_list 查看全部."
        lines = [f"搜索 '{kw}' 找到 {len(hits)} 个技能:"]
        for h in hits:
            lines.append(f"- {h['name']}: {h['description']}")
        return "\n".join(lines)

    def _skill_list(args: dict[str, Any]) -> str:
        """列出全部可用技能."""
        avail = manager.list_available()
        if not avail:
            return "暂无可用技能 (技能库为空, 请确认 data/skills/ 存在)."
        lines = [f"技能库共有 {len(avail)} 个技能:"]
        for a in avail:
            lines.append(f"- {a['name']}: {a['description']}")
        return "\n".join(lines)

    def _skill_add(args: dict[str, Any]) -> str:
        """为当前角色添加一个技能."""
        name = args.get("skill_name", "").strip()
        if not name:
            return "请提供 skill_name."
        return manager.add_skill(_role(), name)

    def _skill_remove(args: dict[str, Any]) -> str:
        """从当前角色移除一个技能."""
        name = args.get("skill_name", "").strip()
        if not name:
            return "请提供 skill_name."
        return manager.remove_skill(_role(), name)

    def _skill_my_skills(args: dict[str, Any]) -> str:
        """查看当前角色已添加的技能."""
        mine = manager.list_role_skills(_role())
        if not mine:
            return "你还没有添加任何技能. 可用 skill_search / skill_list 寻找, 用 skill_add 添加."
        lines = [f"你已添加 {len(mine)} 个技能:"]
        for m in mine:
            lines.append(f"- {m['name']}: {m['description']}")
        return "\n".join(lines)

    tk.add_python_tool(
        "skill_search",
        "搜索技能库中的技能 (按名称或描述关键词). 先搜索找到合适技能, 再用 skill_add 添加给自己.",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词, 如 ppt/video/pdf/写作"},
        }, "required": ["keyword"]},
        _skill_search,
    )
    tk.add_python_tool(
        "skill_list",
        "列出技能库中全部可用技能 (名称+简述). 查看有哪些技能可用.",
        {"type": "object", "properties": {}},
        _skill_list,
    )
    tk.add_python_tool(
        "skill_add",
        "为当前角色添加一个技能. 添加后即可在任务中调用该技能 (获得其完整使用指引).",
        {"type": "object", "properties": {
            "skill_name": {"type": "string", "description": "要添加的技能名, 如 pptx-generator"},
        }, "required": ["skill_name"]},
        _skill_add,
    )
    tk.add_python_tool(
        "skill_remove",
        "从当前角色移除一个已添加的技能.",
        {"type": "object", "properties": {
            "skill_name": {"type": "string", "description": "要移除的技能名"},
        }, "required": ["skill_name"]},
        _skill_remove,
    )
    tk.add_python_tool(
        "skill_my_skills",
        "查看当前角色已添加的技能列表.",
        {"type": "object", "properties": {}},
        _skill_my_skills,
    )
    return tk


def bind_role_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 skill_manager 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: skill_manager 工具类实例.
        role:    绑定的 AgentRole.
    """
    toolkit._skill_holder["role"] = role  # type: ignore[attr-defined]
