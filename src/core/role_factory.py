"""RoleFactory — LLM-driven role creation from hiring requirements.

Usage:
    factory = RoleFactory()
    new_role = factory.create_role("需要一位精通 Rust 的后端工程师，熟悉 gRPC 和 PostgreSQL")
    pool.add_role(new_role)
    pool.start()

接口文档 (模块结构与方法):

类与方法:
    RoleFactory:
        - create_role(): Create a new role from a hiring requirement description.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from src.core.llm import DeepSeekLLM, OllamaLLM
from src.core.role_templates import TEMPLATES, _next_name, add_template
from src.core.roles import AgentRole

logger = logging.getLogger(__name__)

# ── Prompt template ───────────────────────────────────────

_CREATE_ROLE_PROMPT = """你是一个 HR 专员，需要根据用人需求创建一个新的团队成员角色。

现有角色模板（参考格式）：
{existing_templates}

用人需求：
{requirement}

请根据需求创建一个新角色，输出 JSON 格式：
```json
{{
    "role_id": "英文小写下划线，如 rust_engineer",
    "title": "职位名称",
    "responsibilities": "职责描述（中文，一句话概括主要工作内容）",
    "personality": "性格特点（中文，2-3句）",
    "skills": ["技能1", "技能2", ...],
    "interest_keywords": ["关键词1", "关键词2", ...],
    "system_prompt_extra": "额外的系统提示（可选，如输出格式要求）"
}}
```

注意：
1. role_id 不要与现有模板重复
2. interest_keywords 要包含中英文关键词
3. skills 至少 5 个
4. 关键词至少 6 个
5. 仅输出 JSON，不要其他内容"""


class RoleFactory:
    """Creates new AgentRole instances via LLM based on hiring requirements.

    Usage:
        factory = RoleFactory()
        role = factory.create_role("需要一位精通 Rust 的后端工程师")
        pool.add_role(role)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        # LLM 后端: deepseek (云端) / ollama (本地), 默认读环境变量 LLM_PROVIDER
        provider = provider or os.environ.get("LLM_PROVIDER", "deepseek")
        if provider == "ollama":
            self._llm = OllamaLLM(model=model, label="role_factory")
        else:
            self._llm = DeepSeekLLM(api_key=api_key, model=model, label="role_factory")

    def create_role(self, requirement: str) -> AgentRole:
        """Create a new role from a hiring requirement description.

        Calls the LLM to generate a role config, then builds an AgentRole.
        Also registers it in the template pool.

        Args:
            requirement: Natural language hiring requirement.
                         e.g. "需要一位精通 Rust 的后端工程师，熟悉 gRPC 和 PostgreSQL"

        Returns:
            A new AgentRole with a generated person name and role_id.
        """
        # Build the list of existing templates for the LLM
        existing = []
        for tname, factory_fn in TEMPLATES.items():
            r = factory_fn()
            existing.append({
                "role_id": r.role_id,
                "title": r.title,
                "skills": r.skills[:5],
                "keywords": sorted(r.interest_keywords)[:5],
            })

        prompt = _CREATE_ROLE_PROMPT.format(
            existing_templates=json.dumps(existing, indent=2, ensure_ascii=False),
            requirement=requirement,
        )

        logger.info("RoleFactory: generating role for requirement: %s", requirement[:80])

        response_text, tokens = self._llm.chat(
            system="你是一个专业的 HR 专员，擅长根据需求创建精准的角色定义。仅输出 JSON。",
            user=prompt,
            temperature=0.3,
            max_tokens=512,
        )

        # Parse JSON from LLM response
        role_config = self._parse_json(response_text)
        if not role_config:
            raise ValueError(f"Failed to parse role config from LLM response: {response_text[:200]}")

        # Validate required fields
        required = ["role_id", "title", "responsibilities", "personality", "skills", "interest_keywords"]
        for field in required:
            if field not in role_config:
                raise ValueError(f"Missing required field '{field}' in role config")

        # Generate unique person name and role_id
        person_name = _next_name()
        generated_role_id = role_config["role_id"]

        # Ensure role_id is unique
        if generated_role_id in TEMPLATES:
            generated_role_id = f"{generated_role_id}_{_next_name()[-3:]}"

        role = AgentRole(
            name=person_name,
            role_id=generated_role_id,
            title=role_config["title"],
            responsibilities=role_config.get("responsibilities", ""),
            personality=role_config["personality"],
            skills=role_config["skills"],
            interest_keywords=set(role_config["interest_keywords"]),
            system_prompt_extra=role_config.get("system_prompt_extra", ""),
        )

        # Register in template pool
        add_template(role)

        logger.info(
            "RoleFactory: created role '%s' (%s) — %s, %d skills, %d keywords, %d tokens",
            generated_role_id, person_name, role.title,
            len(role.skills), len(role.interest_keywords), tokens,
        )

        return role

    @staticmethod
    def _parse_json(text: str) -> Optional[dict[str, Any]]:
        """Extract JSON from LLM response (handles ```json blocks)."""
        # Try direct parse first
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try ```json ... ``` block
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Try { ... } extraction
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse JSON from: %s", text[:200])
        return None
