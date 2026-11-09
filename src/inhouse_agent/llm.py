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
    model: str | None = None  # 답한 모델 이름 (여러 모델을 쓸 때)

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
        model: str | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse: ...


CACHE_KEY_MAX = 64  # prompt_cache_key 최대 길이 (OpenAI)


def _api_messages(messages: list[dict[str, Any]], vision: bool) -> list[dict[str, Any]]:
    """API 로 보낼 형태: 내부용 키(_로 시작) 제거, 비전 미지원 배포면 이미지를 텍스트 안내로.
    사용자 메시지에 _sent(보낸 시각)가 있으면 끝에 붙임 - 시스템 프롬프트에 분 단위 시각을 넣지 않아
    대화 앞부분이 그대로 유지되므로 프롬프트 캐시가 깨지지 않음."""
    out = []
    for m in messages:
        sent = m.get("_sent") if m.get("role") == "user" else None
        m = {k: v for k, v in m.items() if not k.startswith("_")}
        if sent:
            note = f"\n\n(보낸 시각: {sent})"
            if isinstance(m.get("content"), list):
                m["content"] = [*m["content"], {"type": "text", "text": note.strip()}]
            else:
                m["content"] = f"{m.get('content') or ''}{note}"
        if isinstance(m.get("content"), list) and not vision:
            m["content"] = "\n".join(
                p.get("text", "") if p.get("type") == "text" else "(이미지가 첨부되었지만 현재 모델 배포는 이미지를 볼 수 없습니다: llm.vision=false)"
                for p in m["content"]
            )
        out.append(m)
    return out


def cached_tokens(usage: Any) -> int:
    """usage.prompt_tokens_details.cached_tokens (SDK 객체 또는 dict). 없으면 0."""
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    v = details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", None)
    return int(v or 0)


def _rejects_cache_key(e: BaseException) -> bool:
    """400 이면서 오류 내용에 prompt_cache_key 가 나오면 '모르는 인자' 로 거절한 것."""
    status = getattr(e, "status_code", None)
    return status == 400 and "prompt_cache_key" in str(e)


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
        self._cache_key_ok = True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        model: str | None = None,  # 하나의 배포만 쓰므로 무시 (여러 모델은 routing.ModelPool)
        cache_key: str | None = None,
    ) -> LLMResponse:
        """cache_key: 대화마다 같은 값 → Azure/OpenAI 가 같은 캐시 서버로 보내 앞부분(시스템 프롬프트·도구·이전 대화)을
        캐시 입력(일반 입력의 약 10% 가격)으로 처리할 가능성이 커짐. 배포·api_version 이 모르는 인자라고 거절하면 끄고 다시 보냄."""
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
        body = dict(cfg.extra_body or {})
        if cache_key and cfg.prompt_cache_key and self._cache_key_ok:
            # extra_body 로 보냄: 오래된 openai SDK(1.54 등)는 prompt_cache_key 인자를 모르기 때문
            body.setdefault("prompt_cache_key", cache_key[:CACHE_KEY_MAX])
        if body:
            params["extra_body"] = body

        try:
            resp = await self._client.chat.completions.create(**params)
        except Exception as e:
            if "prompt_cache_key" in body and _rejects_cache_key(e):
                self._cache_key_ok = False  # 이 배포는 prompt_cache_key 를 모름 → 이후로는 보내지 않음 (자동 캐시는 그대로 동작)
                log.warning("배포 %s 가 prompt_cache_key 를 지원하지 않아 빼고 보냅니다: %s", cfg.deployment, str(e)[:200])
                body.pop("prompt_cache_key")
                if not body:
                    params.pop("extra_body")
                resp = await self._client.chat.completions.create(**params)
            else:
                raise
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
                # 입력 중 캐시에서 읽은 토큰 (prompt_tokens 에 포함됨, 더 싼 단가) - 1,024 토큰 이상 같은 앞부분일 때 생김
                "cached_tokens": cached_tokens(resp.usage),
            }
        return LLMResponse(content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
