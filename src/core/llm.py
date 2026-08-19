"""LLM 客户端 — DeepSeek 云端 / 本地 Ollama 真实 AI 后端接入.

两个后端都走 OpenAI 兼容的 chat/completions 接口 (OpenAI 格式):
  - DeepSeekLLM: DeepSeek V4 Flash, 可选 thinking (推理) 模式, 需 API Key.
  - OllamaLLM:   本地 Ollama 服务, 默认 gemma4:31b, 免 API Key.

公共接口 (与 MockLLM 同形):
  - chat(system, user) → (response_text, tokens_consumed)
  - summarize(log_text) → (summary_text, tokens_consumed)
  - chat_with_tools(messages, tools) → (content, raw_tool_calls, usage) 原生 function calling

切换后端: 环境变量 LLM_PROVIDER=deepseek|ollama (见 roles.RolePool / role_factory.RoleFactory).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────

# DeepSeek 云端
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "true").lower() in ("1", "true", "yes", "on")

# 本地 Ollama (OpenAI 兼容端点, 免 API Key)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b")

DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.7

# ── 请求失败重试 (用户指定) ────────────────────────────────
# 本 Agent 并发请求量大, 开局阶段易触发限速 (429/5xx/超时).
# 规则: 可恢复错误等 API_RETRY_DELAY 秒 (10s) 重试, 最多 API_RETRY_MAX 次
# (200 次 — 请求量大需要足够耐心); 重试耗尽放弃本次请求 (任务标记失败),
# 不再无限等待. 不可恢复的客户端错误 (400/401/403/404 等) 不重试.
API_RETRY_DELAY = 10.0   # 失败后的重试间隔秒数
API_RETRY_MAX = 200      # 最大重试次数
API_TIMEOUT = 120        # 单次请求超时秒数


# ── 共享基类: OpenAI 兼容客户端 ─────────────────────────────

class OpenAICompatLLM:
    """OpenAI 兼容 chat/completions 客户端基类 (DeepSeek/Ollama 共用).

    两个后端协议完全一致 (OpenAI 格式), 差异只有:
      环境变量名 / 默认模型 / 是否需要 API Key / 是否注入 thinking 参数.

    子类覆盖类属性即可, 无需重写请求逻辑:
      API_NAME        日志前缀 (如 "DeepSeek" / "Ollama")
      API_KEY_ENV     读取 API Key 的环境变量名 (空串 = 不需要 Key)
      BASE_URL_ENV    读取服务地址的环境变量名
      MODEL_ENV       读取模型名的环境变量名
      DEFAULT_BASE_URL 默认服务地址
      DEFAULT_MODEL    默认模型名
      REQUIRES_API_KEY 是否强制要求 API Key (缺失时抛 ValueError)

    参数:
        api_key: API 密钥 (可选; Ollama 免 Key, 传空则不携带 Authorization 头).
        base_url: 服务地址 (默认读环境变量, 再回落到类默认值).
        model: 模型名称 (同上).
        label: 角色标识, DEBUG 日志前缀, 便于多角色并发时区分是谁在调 API.
    """

    API_NAME = "LLM"
    API_KEY_ENV = ""
    BASE_URL_ENV = ""
    MODEL_ENV = ""
    DEFAULT_BASE_URL = ""
    DEFAULT_MODEL = ""
    REQUIRES_API_KEY = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        label: Optional[str] = None,
    ):
        self.api_key = api_key if api_key is not None else (
            os.environ.get(self.API_KEY_ENV, "") if self.API_KEY_ENV else ""
        )
        self.base_url = (
            base_url or os.environ.get(self.BASE_URL_ENV, self.DEFAULT_BASE_URL)
        ).rstrip("/")
        self.model = model or os.environ.get(self.MODEL_ENV, self.DEFAULT_MODEL)
        # 角色标识: DEBUG 日志前缀, 便于多角色并发时区分是谁在调 API
        self.label = label or ""
        # 最近一次请求失败原因 (重试耗尽/不可恢复错误时诊断用)
        self._retry_error = ""

        if self.REQUIRES_API_KEY and not self.api_key:
            raise ValueError(
                f"{self.API_NAME} API key is required. Set {self.API_KEY_ENV} env var "
                f"or pass api_key= to {self.__class__.__name__}()."
            )

    # ── 调试日志 (带角色前缀) ─────────────────────────────

    def _debug(self, msg: str, *args) -> None:
        """打 DEBUG 日志, 消息前加角色标识 (如 [ceo]), 无角色则不加."""
        if self.label:
            # 前缀拼进格式串: 占位符总数 = 1(label) + msg 自身占位符
            logger.debug("[%s] " + msg, self.label, *args)
        else:
            logger.debug(msg, *args)

    # ── Public API (same interface as MockLLM) ─────────────

    def chat(
        self,
        system: str,
        user: str,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> tuple[str, int]:
        """发送聊天请求. 返回(回复文本, Token数).

        参数:
            system: 系统提示词.
            user: 用户输入.
            temperature: 采样温度.
            max_tokens: 最大输出 token 数.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
            self._debug("chat: 追加 system 消息 (%d 字符): %s",
                        len(system), system)
        messages.append({"role": "user", "content": user})
        self._debug("chat: 追加 user 消息 (%d 字符): %s",
                    len(user), user)

        response_text, usage = self._call_api(messages, temperature, max_tokens)
        tokens = usage.get("total_tokens", 0) if usage else 0
        return response_text, tokens

    def summarize(
        self,
        log_text: str,
        temperature: float = 0.3,
        max_tokens: int = 256,
    ) -> tuple[str, int]:
        """从日志/文本生成简洁总结. 返回(总结文本, Token数).

        参数:
            log_text: 待总结的日志/文本块.
            temperature: 采样温度 (总结任务偏低).
            max_tokens: 最大输出 token 数.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个专业的助理，负责写工作总结。请用简洁的中文总结以下内容，"
                    "提取关键决策、待办事项和值得关注的低优先级事件。"
                    "输出格式：先写一段总结，然后列出关键决策和待办事项。"
                ),
            },
            {"role": "user", "content": f"请总结今天的工作日志：\n{log_text}"},
        ]
        self._debug("summarize: 追加 user 消息 (%d 字符): %s",
                    len(log_text), log_text)

        response_text, usage = self._call_api(messages, temperature, max_tokens)
        tokens = usage.get("total_tokens", 0) if usage else 0
        return response_text, tokens

    # ── Internal ───────────────────────────────────────────

    def _extra_payload(self, payload: dict, max_tokens: Optional[int]) -> None:
        """子类钩子: 在发送前向 payload 注入私有参数 (基类默认不注入).

        参数:
            payload: 即将发送的请求体 (已含 model/messages/temperature 等).
            max_tokens: 调用方传入的 max_tokens 原始值 (None = 未显式限制).
        """
        return

    def _post_with_retry(self, url: str, payload: dict,
                         headers: dict) -> Optional[dict]:
        """发送 POST 请求, 失败自动重试 (限速/超时/5xx 等可恢复错误).

        规则 (用户指定, 本 Agent 并发请求量大):
          - 可恢复错误 (HTTP 429 限速 / 5xx / 超时 / 连接错误) → 等
            API_RETRY_DELAY 秒 (10s) 后重试, 最多 API_RETRY_MAX 次 (200)
          - 不可恢复错误 (400/401/403/404 等客户端错误) → 立即放弃, 不重试
          - 重试耗尽 → 放弃本次请求 (返回 None, 调用方转成 "[API error: ...]"
            错误文本 → 上层任务标记失败, 不再无限等待)

        参数:
            url:     chat/completions 端点.
            payload: 请求体.
            headers: 请求头.

        返回:
            响应 JSON dict; 放弃时返回 None (原因在 self._retry_error).
        """
        last_err = ""
        for attempt in range(1, API_RETRY_MAX + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers,
                                     timeout=API_TIMEOUT)
                if resp.status_code == 429 or resp.status_code >= 500:
                    # 限速/服务端错误: 可恢复, 等 10s 重试
                    last_err = f"HTTP {resp.status_code}"
                    logger.warning(
                        "%s API 请求失败 (%s, 第 %d/%d 次), %.0fs 后重试",
                        self.API_NAME, last_err, attempt, API_RETRY_MAX,
                        API_RETRY_DELAY)
                    time.sleep(API_RETRY_DELAY)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                last_err = "timeout"
                logger.warning(
                    "%s API 请求超时 (第 %d/%d 次), %.0fs 后重试",
                    self.API_NAME, attempt, API_RETRY_MAX, API_RETRY_DELAY)
                time.sleep(API_RETRY_DELAY)
            except requests.exceptions.HTTPError as e:
                # 4xx 客户端错误 (非 429): 重试无意义, 立即放弃
                self._retry_error = str(e)
                logger.error("%s API 请求失败, 不可恢复: %s", self.API_NAME, e)
                return None
            except requests.exceptions.RequestException as e:
                # 连接错误/其他网络问题: 可恢复, 重试
                last_err = str(e)
                logger.warning(
                    "%s API 请求错误 (%s, 第 %d/%d 次), %.0fs 后重试",
                    self.API_NAME, last_err[:120], attempt, API_RETRY_MAX,
                    API_RETRY_DELAY)
                time.sleep(API_RETRY_DELAY)
        # 重试耗尽: 放弃本次请求 (上层把错误文本标记为任务失败)
        self._retry_error = f"重试 {API_RETRY_MAX} 次仍失败: {last_err}"
        logger.error("%s API 请求失败 %d 次, 放弃: %s",
                     self.API_NAME, API_RETRY_MAX, last_err)
        return None

    def _call_api(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, Optional[dict]]:
        """核心 API 调用. 返回 (content_text, usage_dict).

        请求 OpenAI 兼容 /v1/chat/completions 端点. 推理内容兼容两个
        字段名: reasoning_content (DeepSeek) 与 reasoning (Ollama).
        API 超时/异常时返回 "[API timeout]" / "[API error: ...]" 错误文本
        (roles.py 的 LLM_ERROR_MARKERS 据此把任务标记为失败).
        """
        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        self._extra_payload(payload, max_tokens)

        self._debug(
            "%s API call: model=%s messages=%d",
            self.API_NAME, self.model, len(messages),
        )

        data = self._post_with_retry(url, payload, headers)
        if data is None:
            # 重试耗尽/不可恢复: 转成错误文本 (roles.py 的 LLM_ERROR_MARKERS
            # 据此把任务标记为失败, 不再无限等待)
            return f"[API error: {self._retry_error}]", None

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        # 提取正文; 若后端开了推理, reasoning_content (DeepSeek) 或
        # reasoning (Ollama) 里是思考过程, 只进 DEBUG 日志, 不返回给调用方
        content = message.get("content", "") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""

        if reasoning:
            self._debug("%s reasoning (%d chars): %s",
                        self.API_NAME, len(reasoning), reasoning)

        # 正文为空但推理非空 (边缘情况): 退回推理内容, 避免空答复
        if not content and reasoning:
            logger.warning("%s: empty content, falling back to reasoning_content",
                           self.API_NAME)
            content = reasoning

        usage = data.get("usage")

        if not content:
            logger.warning("%s returned empty content. Raw: %s",
                           self.API_NAME, str(data)[:200])

        # Token 明细日志 (调试用)
        if usage:
            self._debug(
                "%s tokens: prompt=%s completion=%s total=%s",
                self.API_NAME,
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                usage.get("total_tokens", "?"),
            )

        return content, usage

    # ── 原生 function calling ─────────────────────────────

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, list[dict], Optional[dict]]:
        """原生 function calling 请求 (OpenAI 兼容).

        请求携带 tools 声明, 响应里 message.tool_calls 结构化给出工具调用
        (name + arguments JSON), 而非文本协议 ```tool_call 块.

        参数:
            messages: 对话消息 (含 role/tool_call_id 等字段, 原样透传).
            tools:    OpenAI 格式工具声明列表.

        返回:
            (content, raw_tool_calls, usage).
            raw_tool_calls: 响应中 message["tool_calls"] 原样列表 (每个含
            id/type/function{name,arguments}); 无工具调用时为 [].
        """
        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # 无上限: max_tokens=None 时不传该字段 (由模型决定最大输出, 避免
        # 长内容 JSON 被 1024 token 截断 → 非法 JSON 死循环). 上限限制以后再加.
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": "auto",
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        self._extra_payload(payload, max_tokens)

        self._debug(
            "%s API call (tools): model=%s messages=%d tools=%d",
            self.API_NAME, self.model, len(messages), len(tools),
        )

        data = self._post_with_retry(url, payload, headers)
        if data is None:
            # 重试耗尽/不可恢复: 转成错误文本 (任务标记失败)
            return f"[API error: {self._retry_error}]", [], None

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})

        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        raw_calls = message.get("tool_calls") or []

        if reasoning:
            self._debug("%s reasoning (%d chars): %s",
                        self.API_NAME, len(reasoning), reasoning)

        # 原生工具调用: content 可能为空 (推理全在 reasoning 字段),
        # 有 tool_calls 时以 tool_calls 为准, 不回退到 reasoning 当正文
        if not content and reasoning and not raw_calls:
            logger.warning("%s: empty content, falling back to reasoning_content",
                           self.API_NAME)
            content = reasoning

        usage = data.get("usage")

        if usage:
            self._debug(
                "%s tokens: prompt=%s completion=%s total=%s",
                self.API_NAME,
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                usage.get("total_tokens", "?"),
            )

        return content, raw_calls, usage


# ── DeepSeek 云端 ──────────────────────────────────────────

class DeepSeekLLM(OpenAICompatLLM):
    """DeepSeek API 客户端, 支持思维链 (thinking) 模式.

    用法:
        llm = DeepSeekLLM(api_key="sk-...")
        # Thinking 模式: 环境变量 DEEPSEEK_THINKING=true 或 DeepSeekLLM(thinking=True)
        text, tokens = llm.chat(system="You are helpful.", user="Hello")
        text, tokens = llm.summarize(log_text="...")

    参数:
        api_key: DeepSeek API 密钥 (默认读 DEEPSEEK_API_KEY).
        base_url: API 地址 (默认 DEEPSEEK_BASE_URL).
        model: 模型名称 (默认 DEEPSEEK_MODEL).
        thinking: 是否启用思考模式 (None = 读环境变量 DEEPSEEK_THINKING).
        label: 角色标识 (DEBUG 日志前缀).
    """

    API_NAME = "DeepSeek"
    API_KEY_ENV = "DEEPSEEK_API_KEY"
    BASE_URL_ENV = "DEEPSEEK_BASE_URL"
    MODEL_ENV = "DEEPSEEK_MODEL"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"
    REQUIRES_API_KEY = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        thinking: Optional[bool] = None,
        label: Optional[str] = None,
    ):
        super().__init__(api_key=api_key, base_url=base_url, model=model, label=label)
        # 思考模式: 显式传参优先, 否则读环境变量 (默认开)
        self.thinking = thinking if thinking is not None else DEEPSEEK_THINKING

    def _extra_payload(self, payload: dict, max_tokens: Optional[int]) -> None:
        """DeepSeek: thinking 开启时注入 thinking 参数, 并保证 max_tokens >= 1024.

        推理预算需要额外 token 空间; 显式 max_tokens 过小会截断思考过程.
        """
        if not self.thinking:
            return
        payload["thinking"] = {"type": "enabled"}
        cur = payload.get("max_tokens")
        if cur is not None and cur < 1024:
            payload["max_tokens"] = 1024


# ── 本地 Ollama ────────────────────────────────────────────

class OllamaLLM(OpenAICompatLLM):
    """本地 Ollama 客户端 (OpenAI 兼容端点, 免 API Key).

    用法:
        llm = OllamaLLM()   # 默认连接本地 http://localhost:11434 的 gemma4:31b
        text, tokens = llm.chat(system="You are helpful.", user="Hello")
        content, raw_calls, usage = llm.chat_with_tools(messages, tools)

    说明:
        Ollama 的 /v1/chat/completions 与 OpenAI 格式一致, 无需鉴权
        (不携带 Authorization 头). 模型默认不强制开 thinking — 保持
        普通对话低延迟; 推理模型若要思考过程, 由 Ollama 侧自行处理.

    参数:
        api_key: 兼容参数, 一般留空 (传了也会作为 Bearer 头发送).
        base_url: Ollama 服务地址 (默认 OLLAMA_BASE_URL = http://localhost:11434).
        model: 模型标签 (默认 OLLAMA_MODEL = gemma4:31b).
        label: 角色标识 (DEBUG 日志前缀).
    """

    API_NAME = "Ollama"
    API_KEY_ENV = ""
    BASE_URL_ENV = "OLLAMA_BASE_URL"
    MODEL_ENV = "OLLAMA_MODEL"
    DEFAULT_BASE_URL = "http://localhost:11434"
    DEFAULT_MODEL = "gemma4:31b"
    REQUIRES_API_KEY = False
