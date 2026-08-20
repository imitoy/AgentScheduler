"""人力资源工具类 (HR ToolKit).

包含:
  - post_job_posting: 发布招聘启事. 输入用人需求, 后台立即完成候选人(新角色)
    的创建并让其加入团队 (启动 worker), 返回新人的完整档案.
  - list_candidates: 列出模板池中的所有角色 (已入职).

用法:
    from src.python_tools.hr_toolkit import create_hr_toolkit
    tk = create_hr_toolkit()
    hr_role.add_toolkit(tk)   # add_toolkit 自动绑定当前角色 (用于获取 RolePool)

接口文档 (模块结构与方法):

模块级函数:
    - create_hr_toolkit(): 创建人力资源工具类.
    - bind_role_to_toolkit(): 将当前角色绑定到 hr 工具类 (由 AgentRole.add_toolkit 内部调用).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_hr_toolkit(api_key: str | None = None) -> ToolKit:
    """创建人力资源工具类.

    参数:
        api_key: DeepSeek API 密钥 (可选, 默认读环境变量).

    返回:
        包含招聘相关工具的 ToolKit 实例.
    """
    tk = ToolKit(name="hr", description="人力资源工具类: 招聘, 入职")
    # 持有当前角色引用 (由 AgentRole.add_toolkit 绑定, 用于访问 RolePool)
    tk._hr_holder = {"role": None}  # type: ignore[attr-defined]

    def _role() -> Any:
        r = tk._hr_holder["role"]  # type: ignore[attr-defined]
        if r is None:
            raise RuntimeError("hr 工具类尚未绑定角色, 请通过 role.add_toolkit() 注册")
        return r

    def _pool() -> Any:
        """获取角色所属 RolePool (可能未启动)."""
        return getattr(_role(), "_pool", None)

    def _post_job_posting(args: dict[str, Any]) -> str:
        """发布招聘启事: 招聘新角色并立即入职.

        参数:
            args: {"requirement": 用人需求描述, "source": 申请人来源(可选)}

        流程:
            1. HR 输入自然语言的用人需求 (招聘启事)
            2. 后台创建新员工档案 (role_id, 姓名, 职位, 技能等)
            3. 新员工立即加入团队 (注册进 RolePool 并启动 worker, 可收发消息)
            4. 同时注册到公司人才库 (模板池), 供后续复用
            5. 返回新员工档案给 HR 确认

        返回:
            新员工的 JSON 档案 (role_id, 姓名, 职位, 技能, 状态等).
        """
        requirement = args.get("requirement", "").strip()
        if not requirement:
            return "错误: 'requirement' (用人需求) 为必填参数."

        from src.core.role_factory import RoleFactory

        # 后台招聘流程: 根据招聘启事生成新员工档案 (HR 无需了解内部实现)
        factory = RoleFactory(api_key=api_key)
        try:
            new_role = factory.create_role(requirement)
        except Exception as exc:
            logger.error("招聘流程处理失败: %s", exc)
            return f"错误: 招聘启事处理失败 - {exc}"

        # 入职: 加入运行中的团队 (RolePool), 启动 worker, 可收发消息
        pool = _pool()
        onboarding = "已加入团队"
        if pool is not None:
            try:
                pool.add_role_and_start(new_role)
                onboarding = "已加入团队并上岗 (可收发消息)"
            except ValueError as exc:  # 角色已存在等
                logger.warning("入职失败: %s", exc)
                onboarding = f"团队注册失败: {exc}"
        else:
            logger.warning("RolePool 未绑定, 新员工仅注册到模板池")

        # 返回新角色的完整信息
        info = {
            "role_id": new_role.role_id,
            "name": new_role.name,
            "title": new_role.title,
            "responsibilities": new_role.responsibilities,
            "personality": new_role.personality,
            "skills": new_role.skills,
            "interest_keywords": sorted(new_role.interest_keywords),
            "status": onboarding,
        }
        return json.dumps(info, ensure_ascii=False, indent=2)

    def _list_candidates(args: dict[str, Any]) -> str:
        """列出模板池中的所有候选人(角色).

        参数:
            args: 无需参数.

        返回:
            所有已注册角色的列表.
        """
        from src.core.role_templates import TEMPLATES

        roles = []
        for tname, factory_fn in TEMPLATES.items():
            r = factory_fn()
            roles.append({
                "role_id": r.role_id,
                "name": r.name,
                "title": r.title,
                "skills_count": len(r.skills),
            })
        return json.dumps(roles, ensure_ascii=False, indent=2)

    tk.add_python_tool(
        name="post_job_posting",
        description=(
            "发布招聘启事. 输入用人需求, 发布后新员工会立即加入团队并上岗 "
            "(可收发消息、参与工作). 后台自动创建新员工的完整档案 "
            "(包括 role_id, 姓名, 职位, 性格, 技能, 关键词). "
            "示例需求: '需要一位精通 Rust 的后端工程师, 熟悉 gRPC 和 PostgreSQL'"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "用人需求描述 (自然语言, 尽量包含技能要求和性格偏好)",
                },
            },
            "required": ["requirement"],
        },
        handler=_post_job_posting,
    )

    tk.add_python_tool(
        name="list_candidates",
        description=(
            "列出当前角色模板池中的所有角色 (已入职的成员), 包含 role_id, 姓名, 职位."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_list_candidates,
    )

    return tk


def bind_role_to_toolkit(toolkit: ToolKit, role: Any) -> None:
    """将当前角色绑定到 hr 工具类 (由 AgentRole.add_toolkit 内部调用).

    参数:
        toolkit: hr 工具类实例.
        role:    绑定的 AgentRole (用于通过 _pool 找到 RolePool 完成入职).
    """
    toolkit._hr_holder["role"] = role  # type: ignore[attr-defined]
