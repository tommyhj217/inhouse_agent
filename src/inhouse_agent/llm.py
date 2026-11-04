"""Azure OpenAI (Chat Completions + function calling) 클라이언트 래퍼."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import LLMConfig

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 문자열 (LLM 이 생성한 그대로)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """대화 이력에 넣을 assistant 메시지(dict)."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return msg


class ChatModel(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse: ...


def _api_messages(messages: list[dict[str, Any]], vision: bool) -> list[dict[str, Any]]:
    """API 로 보낼 형태: 내부용 키(_로 시작) 제거, 비전 미지원 배포면 이미지를 텍스트 안내로."""
    out = []
    for m in messages:
        m = {k: v for k, v in m.items() if not k.startswith("_")}
        if isinstance(m.get("content"), list) and not vision:
            m["content"] = "\n".join(
                p.get("text", "") if p.get("type") == "text" else "(이미지가 첨부되었지만 현재 모델 배포는 이미지를 볼 수 없습니다: llm.vision=false)"
                for p in m["content"]
            )
        out.append(m)
    return out


class AzureChatModel:
    """AsyncAzureOpenAI 기반 ChatModel 구현."""

    def __init__(self, cfg: LLMConfig):
        cfg.validate()
        self.cfg = cfg
        from openai import AsyncAzureOpenAI

        kwargs: dict[str, Any] = {
            "api_version": cfg.api_version,
            "timeout": cfg.timeout,
            "max_retries": cfg.max_retries,
        }
        if cfg.extra_headers:
            kwargs["default_headers"] = cfg.extra_headers
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        else:
            kwargs["azure_endpoint"] = cfg.endpoint

        if cfg.auth == "entra_id":
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("auth: entra_id 를 쓰려면 `pip install azure-identity` 가 필요합니다.") from e
            # azure.identity 의 동기 토큰 공급자를 사용합니다 (openai SDK 가 동기 provider 도 지원).
            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(DefaultAzureCredential(), cfg.entra_scope)
        else:
            kwargs["api_key"] = cfg.api_key

        self._client = AsyncAzureOpenAI(**kwargs)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> LLMResponse:
        cfg = self.cfg
        params: dict[str, Any] = {"model": cfg.deployment, "messages": _api_messages(messages, cfg.vision)}
        if tools:
            params["tools"] = tools
            if tool_choice:
                params["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                params["parallel_tool_calls"] = parallel_tool_calls
        if cfg.max_output_tokens:
            params[cfg.max_tokens_param] = cfg.max_output_tokens
        if cfg.temperature is not None:
            params["temperature"] = cfg.temperature
        if cfg.reasoning_effort:
            params["reasoning_effort"] = cfg.reasoning_effort
        if cfg.extra_body:
            params["extra_body"] = cfg.extra_body

        resp = await self._client.chat.completions.create(**params)
        if not resp.choices:
            # Azure 콘텐츠 필터 등으로 choices 가 비는 경우
            return LLMResponse(content="(모델 응답이 비어 있습니다. 콘텐츠 필터 여부를 확인하세요.)", finish_reason="empty")
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
            if getattr(tc, "function", None) is not None
        ]
        usage: dict[str, int] = {}
        if resp.usage is not None:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens or 0,
                "completion_tokens": resp.usage.completion_tokens or 0,
                "total_tokens": resp.usage.total_tokens or 0,
            }
        return LLMResponse(content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
