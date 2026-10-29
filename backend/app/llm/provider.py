"""DeepSeek adapter —— **唯一**发起外部模型调用的地方（文档 §7 设计约定）。

## 为什么不装 openai SDK

DeepSeek 提供 OpenAI 兼容接口，装 SDK 是最省事的做法。但这里坚持用 `httpx` 直连 REST：

1. **少一个依赖就少一个失败面。** 本项目已经是 FastAPI（自带 httpx），
   为一个 POST 请求引入一整套 SDK，在单用户本地应用里不划算。
2. **错误分类必须自己来。** SDK 会把超时、限流、空响应统一包成泛化的
   `APIError`，而本项目的降级策略（§6.1 的 L0–L4）**依赖区分错误种类**：
   `finish_reason == "length"` 要加 token 重试，空 content 要直接 L0，
   429 才是限流。自己发请求才能拿到这些细节。

## DeepSeek 特有的三个坑（官方明确提示）

- 只支持 `response_format={"type": "json_object"}`，**不保证字段结构与类型**；
- JSON 模式可能偶尔返回**空 content**，这不是异常，是必须被处理的正常情况；
- JSON 可能被 `max_tokens` **中途截断**，需看 `finish_reason == "length"`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 模型偶尔会给 JSON 套一层 ```json 围栏，剥掉即可，不必因此判失败。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LLMErrorKind(str, Enum):
    """错误分类。**分类本身就是降级策略的输入**，不能笼统地抛一个 Exception。"""

    TIMEOUT = "timeout"        # 超时：可重试，也可直接 L0
    NETWORK = "network"        # 网络不可达：答辩断网的典型形态，直接 L0
    RATE_LIMIT = "rate_limit"  # 429：限流，直接 L0
    EMPTY = "empty"            # 空 content：DeepSeek 官方提示可能偶发，直接 L0
    TRUNCATED = "truncated"    # finish_reason=length：加 token 重试一次
    API = "api"                # 其他非 2xx：直接 L0
    SCHEMA = "schema"          # 返回的不是合法 JSON：L1，回传原文重试


class LLMError(Exception):
    def __init__(self, kind: LLMErrorKind, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail}")


@dataclass
class LLMResponse:
    """一次成功的原始响应。注意：**这里还没做业务校验**。"""

    content: str
    data: dict
    finish_reason: str | None = None
    attempts: int = 1
    model: str = ""
    usage: dict = field(default_factory=dict)


class DeepSeekProvider:
    """DeepSeek chat/completions 客户端。

    `transport` 参数是为测试留的口子：注入 `httpx.MockTransport` 即可
    在不联网的情况下覆盖超时、限流、空响应等每一种故障模式。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key if api_key is not None else settings.DEEPSEEK_API_KEY
        self.base_url = (base_url or settings.DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or settings.DEEPSEEK_MODEL
        self.timeout = timeout if timeout is not None else settings.LLM_TIMEOUT_SEC
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self.temperature = (
            temperature if temperature is not None else settings.LLM_TEMPERATURE
        )
        self._transport = transport

    @property
    def available(self) -> bool:
        """没有 API Key 时不应该去发请求 —— 那只会浪费一次 15 秒超时。"""
        return bool(self.api_key)

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        """发一次 chat/completions，要求 JSON 输出，返回解析后的 dict。

        抛出的 `LLMError.kind` 决定了编排器下一步做什么，调用方**必须**区分处理。
        """
        if not self.api_key:
            raise LLMError(LLMErrorKind.API, "未配置 DEEPSEEK_API_KEY")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            # DeepSeek 只支持这一种 response_format
            "response_format": {"type": "json_object"},
            # 官方的流式与非流式都用这个字段，非流式恒为 False
            "stream": False,
        }

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with self._client() as client:
                resp = await client.post(
                    url, json=body, headers=headers, timeout=timeout or self.timeout
                )
        except httpx.TimeoutException as exc:
            raise LLMError(LLMErrorKind.TIMEOUT, str(exc) or "请求超时") from exc
        except httpx.HTTPError as exc:
            # 断网、DNS 失败、连接被拒都在这一类 —— 答辩现场拔网线的形态
            raise LLMError(LLMErrorKind.NETWORK, str(exc) or "网络不可达") from exc

        if resp.status_code == 429:
            raise LLMError(LLMErrorKind.RATE_LIMIT, "429 Too Many Requests")
        if resp.status_code >= 400:
            raise LLMError(
                LLMErrorKind.API, f"HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMError(LLMErrorKind.API, f"响应不是合法 JSON: {resp.text[:200]}") from exc

        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish_reason = choice.get("finish_reason")

        if not content.strip():
            # 官方明确提示的偶发情况。当作正常分支处理，不是 except。
            raise LLMError(LLMErrorKind.EMPTY, "模型返回空 content")

        if finish_reason == "length":
            raise LLMError(LLMErrorKind.TRUNCATED, "输出被 max_tokens 截断")

        data = _parse_json(content)
        if data is None:
            raise LLMError(LLMErrorKind.SCHEMA, f"content 不是合法 JSON: {content[:200]}")

        return LLMResponse(
            content=content,
            data=data,
            finish_reason=finish_reason,
            model=payload.get("model", self.model),
            usage=payload.get("usage") or {},
        )


def _parse_json(content: str) -> dict | None:
    """宽松解析：先原样解析，失败则剥掉代码围栏再试一次。"""
    text = _FENCE_RE.sub("", content.strip())
    for candidate in (content.strip(), text):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def get_provider(**kwargs) -> DeepSeekProvider:
    """构造 provider。编排器与探测脚本都走这里。"""
    return DeepSeekProvider(**kwargs)
