"""Azure OpenAI 클라이언트 래퍼.

- AzureChatModel      : Chat Completions (기본, llm.api: chat)
- AzureResponsesModel : Responses API (llm.api: responses) - 추론 내용을 다음 요청에 이어 줌
  store=false + include=reasoning.encrypted_content 로 받은 추론 항목(암호화)을 대화에 보관했다가,
  같은 배포에 다시 보낼 때 그대로 넣음. 그러면 모델이 도구 결과를 받을 때마다 처음부터 다시 생각하지 않아
  도구 왕복과 출력(추론) 토큰이 줄어듦 (OpenAI 권장 방식, pi 의 Azure 구현과 같음).
두 클래스 모두 같은 chat() 인터페이스와 내부 메시지 형식(Chat Completions 형식)을 씁니다.
"""

from __future__ import annotations

import json
import logging
import re
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
    # Responses API: 응답 출력 항목(추론 항목 + 메시지 + 도구 호출) 원본과 그것을 만든 배포.
    # 다음 요청에 그대로 다시 보내 추론을 이어 받음 (같은 배포일 때만 - pi 방식)
    raw_items: list[dict[str, Any]] | None = None
    raw_source: str | None = None
    budget_notes: list[str] = field(default_factory=list)  # 출력 한도 자동 조정 기록 (화면·로그용)

    def to_message(self) -> dict[str, Any]:
        """대화 이력에 넣을 assistant 메시지(dict)."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        if self.raw_items:
            ids = {tc.id for tc in self.tool_calls}
            # 실행하지 않을 도구 호출(예: 마지막 정리에서 막은 호출)은 빼야 '결과 없는 도구 호출' 오류가 안 남
            items = [i for i in self.raw_items if i.get("type") != "function_call" or i.get("call_id") in ids]
            msg["_resp_items"], msg["_resp_source"] = items, self.raw_source
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


def _auth_kwargs(cfg: LLMConfig) -> dict[str, Any]:
    if cfg.auth == "entra_id":
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("auth: entra_id 를 쓰려면 `pip install azure-identity` 가 필요합니다.") from e
        # azure.identity 의 동기 토큰 공급자를 사용합니다 (openai SDK 가 동기 provider 도 지원).
        return {"azure_ad_token_provider": get_bearer_token_provider(DefaultAzureCredential(), cfg.entra_scope)}
    return {"api_key": cfg.api_key}


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


EFFORT_DOWN = {"xhigh": "high", "high": "medium", "medium": "low", "low": "minimal"}
_TOO_LARGE = re.compile(r"(max_output_tokens|max_completion_tokens|max_tokens).{0,80}(too large|exceed|greater than|maximum|at most|less than)|"
                        r"(too large|exceed|greater than|maximum|at most|less than).{0,80}(max_output_tokens|max_completion_tokens|max_tokens)", re.I | re.S)


def _args_ok(arguments: str) -> bool:
    try:
        return isinstance(json.loads(arguments or "{}"), dict)
    except ValueError:
        return False


def truncated_without_answer(r: LLMResponse) -> bool:
    """출력 한도에서 잘렸는데 쓸 만한 결과가 없음: 답 글이 비었거나(추론만 하다 끝남) 도구 인자 JSON 이 중간에 잘림."""
    if r.finish_reason != "length":
        return False
    if r.tool_calls:
        return not all(_args_ok(tc.arguments) for tc in r.tool_calls)
    return not (r.content or "").strip()


def _merge_usage(total: dict[str, int], u: dict[str, int]) -> None:
    for k, v in (u or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + int(v)


class OutputBudget:
    """출력 한도(max_output_tokens) 자동 조정. 추론 모델은 '보이지 않는 추론 토큰'도 이 한도를 씀 →
    입력이 길거나 어려운 요청이면 추론만 하다 한도에 닿아 답이 비는(incomplete) 일이 생김. 그때:
      1) 한도를 바로 상한(max_output_tokens_cap)까지 올려 다시 요청 - 출력은 쓴 만큼만 과금되므로 조금씩 올리며
         실패한 호출(추론만 하고 버려지는 토큰)을 늘리는 것보다 낭비가 적음. 올린 값은 기억해 다음 요청부터 바로 씀
      2) 상한에서도 잘리면 추론 강도(reasoning_effort)를 한 단계 낮춰 한 번 더 (그래도 안 되면 잘린 그대로 → 에이전트가 원인·조치 안내)
      3) 배포가 '한도가 너무 크다'고 거절하면 반으로 줄여 다시 (그 값을 상한으로 기억)
    다시 시도한 호출의 토큰도 모두 사용량에 합산하고, usage.output_retries 로 몇 번 했는지 남김."""

    cfg: LLMConfig

    def _init_budget(self) -> None:
        self._budget: int | None = self.cfg.max_output_tokens or None
        self._budget_max: int | None = None  # 배포가 거절한 크기보다 작은 값

    async def _request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kw: Any) -> LLMResponse:
        raise NotImplementedError

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        model: str | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse:
        cfg = self.cfg
        cap = max(cfg.max_output_tokens_cap or 0, cfg.max_output_tokens or 0)
        budget, effort = self._budget, cfg.reasoning_effort
        total: dict[str, int] = {}
        retries = shrinks = 0
        notes: list[str] = []
        while True:
            try:
                r = await self._request(messages, tools, tool_choice=tool_choice, parallel_tool_calls=parallel_tool_calls,
                                        model=model, cache_key=cache_key, budget=budget, effort=effort)
            except Exception as e:
                if budget and getattr(e, "status_code", None) == 400 and _TOO_LARGE.search(str(e)) and budget > 1024 and shrinks < 5:
                    shrinks += 1
                    budget = max(1024, budget // 2)
                    self._budget = self._budget_max = budget
                    notes.append(f"배포가 출력 한도를 거절해 {budget:,} 로 줄임")
                    log.warning("배포 %s 가 출력 한도를 거절해 %s 로 줄여 다시 보냅니다: %s", cfg.deployment, budget, str(e)[:200])
                    continue
                raise
            _merge_usage(total, r.usage)
            if not truncated_without_answer(r) or retries >= 2:
                break
            retries += 1
            if budget and budget < (self._budget_max or cap) and cap:
                new = min(self._budget_max or cap, cap)
                log.warning("배포 %s: 출력 한도 %s 에서 답 없이 잘림(추론 토큰 %s) → %s 로 올려 다시 요청",
                            cfg.deployment, budget, r.usage.get("reasoning_tokens"), new)
                notes.append(f"출력 한도 {budget:,} → {new:,}")
                budget = self._budget = new
                continue
            if effort in EFFORT_DOWN:
                log.warning("배포 %s: 출력 한도 상한(%s)에서도 잘림 → 추론 강도 %s → %s 로 다시 요청", cfg.deployment, budget, effort, EFFORT_DOWN[effort])
                notes.append(f"추론 강도 {effort} → {EFFORT_DOWN[effort]}")
                effort = EFFORT_DOWN[effort]
                continue
            break
        r.usage = total
        if retries:
            r.usage["output_retries"] = retries
            r.budget_notes = notes
        if truncated_without_answer(r):
            r.tool_calls = [tc for tc in r.tool_calls if _args_ok(tc.arguments)]  # 잘린 도구 호출은 실행하지 않음
        return r


class AzureChatModel(OutputBudget):
    """AsyncAzureOpenAI 기반 ChatModel 구현 (Chat Completions)."""

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

        kwargs.update(_auth_kwargs(cfg))
        self._client = AsyncAzureOpenAI(**kwargs)
        self._cache_key_ok = True
        self._init_budget()

    async def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        model: str | None = None,  # 하나의 배포만 쓰므로 무시 (여러 모델은 routing.ModelPool)
        cache_key: str | None = None,
        budget: int | None = None,
        effort: str | None = None,
    ) -> LLMResponse:
        """한 번 호출 (출력 한도 자동 조정은 OutputBudget.chat).
        cache_key: 대화마다 같은 값 → Azure/OpenAI 가 같은 캐시 서버로 보내 앞부분(시스템 프롬프트·도구·이전 대화)을
        캐시 입력(일반 입력의 약 10% 가격)으로 처리할 가능성이 커짐. 배포·api_version 이 모르는 인자라고 거절하면 끄고 다시 보냄."""
        cfg = self.cfg
        params: dict[str, Any] = {"model": cfg.deployment, "messages": _api_messages(messages, cfg.vision)}
        if tools:
            params["tools"] = tools
            if tool_choice:
                params["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                params["parallel_tool_calls"] = parallel_tool_calls
        if budget:
            params[cfg.max_tokens_param] = budget
        if cfg.temperature is not None:
            params["temperature"] = cfg.temperature
        if effort:
            params["reasoning_effort"] = effort
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
                # 출력 중 보이지 않는 추론 토큰 (completion_tokens 에 포함, 출력 한도도 함께 씀)
                "reasoning_tokens": int(getattr(getattr(resp.usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0),
            }
        return LLMResponse(content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Responses API
# ---------------------------------------------------------------------------

RESPONSES_MIN_OUTPUT_TOKENS = 16
_V1_VERSIONS = ("v1", "preview", "latest")


def responses_base_url(cfg: LLMConfig) -> str | None:
    """Responses API 주소. v1(기본): <리소스>/openai/v1 (pi 와 같음). 날짜 버전이면 None (azure_endpoint 방식).
    APIM 등 게이트웨이(base_url)를 쓰면 '/openai' 앞부분을 살려 …/openai/v1 로, 다르면 responses_base_url 로 직접 지정."""
    if cfg.responses_base_url:
        return cfg.responses_base_url.rstrip("/")
    if cfg.responses_api_version not in _V1_VERSIONS:
        return None
    root = cfg.endpoint or ""
    if not root and cfg.base_url:
        i = cfg.base_url.find("/openai")
        root = cfg.base_url[:i] if i >= 0 else cfg.base_url
    root = root.rstrip("/")
    for tail in ("/openai/v1", "/openai"):
        if root.endswith(tail):
            root = root[: -len(tail)]
    return f"{root}/openai/v1"


def responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions 형식 도구 → Responses 형식 (function 이 한 단계 펼쳐짐)."""
    out = []
    for t in tools:
        f = t.get("function") or t
        out.append({"type": "function", "name": f["name"], "description": f.get("description") or "",
                    "parameters": f.get("parameters") or {"type": "object", "properties": {}}, "strict": False})
    return out


def _user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        parts = []
        for p in content:
            if p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url") if isinstance(p.get("image_url"), dict) else p.get("image_url")
                parts.append({"type": "input_image", "image_url": url, "detail": "auto"})
            else:
                parts.append({"type": "input_text", "text": str(p.get("text") or "")})
        return parts
    return [{"type": "input_text", "text": str(content or "")}]


def responses_input(messages: list[dict[str, Any]], vision: bool, source: str) -> tuple[list[dict[str, Any]], int]:
    """내부 메시지(Chat Completions 형식) → Responses input 항목. (항목, 다시 보낸 추론 항목 수)
    - assistant 메시지에 같은 배포(source)가 만든 _resp_items 가 있으면 그대로 (추론 항목 포함, pi 방식)
    - 아니면(다른 모델·Chat Completions 로 만든 대화) 텍스트·도구 호출만, 항목 id 없이 (추론 짝 검사 피함)"""
    items: list[dict[str, Any]] = []
    replayed = 0
    for raw, m in zip(messages, _api_messages(messages, vision)):
        role = m.get("role")
        if role in ("system", "developer"):
            items.append({"role": "developer" if role == "developer" else "system", "content": str(m.get("content") or "")})
        elif role == "user":
            items.append({"role": "user", "content": _user_parts(m.get("content"))})
        elif role == "assistant":
            saved = raw.get("_resp_items")
            ids = [tc["id"] for tc in m.get("tool_calls") or []]
            if saved and raw.get("_resp_source") == source and \
                    [i.get("call_id") for i in saved if i.get("type") == "function_call"] == ids:
                items += [dict(i) for i in saved]
                replayed += sum(1 for i in saved if i.get("type") == "reasoning")
                continue
            if m.get("content"):
                items.append({"role": "assistant", "content": str(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({"type": "function_call", "call_id": tc["id"], "name": fn.get("name"), "arguments": fn.get("arguments") or "{}"})
        elif role == "tool":
            c = m.get("content")
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id"),
                          "output": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)})
    return items, replayed


def _replayable(output: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """다시 보낼 출력 항목. 추론 항목에 암호화 내용이 없으면(store=false 에서는 다시 쓸 수 없음)
    추론 항목을 빼고, 짝 검사에 걸리지 않게 다른 항목의 id 도 뺌."""
    keep = [i for i in output if i.get("type") in ("reasoning", "message", "function_call")]
    if all(i.get("encrypted_content") for i in keep if i.get("type") == "reasoning"):
        return keep
    out = []
    for i in keep:
        if i.get("type") == "reasoning":
            continue
        i = {k: v for k, v in i.items() if k != "id"}
        out.append(i)
    return out


def parse_response(data: dict[str, Any], source: str) -> LLMResponse:
    """Responses API 응답(dict) → LLMResponse."""
    # 'incomplete'(출력 한도·콘텐츠 필터로 잘림)는 오류가 아님 - error 칸이 함께 와도 잘린 응답으로 처리 (OutputBudget 이 다시 시도)
    if data.get("error") and (data.get("status") == "failed" or not data.get("output")) and data.get("status") != "incomplete":
        err = data["error"]
        raise RuntimeError(f"{err.get('code') or 'error'}: {err.get('message') or err}")
    output = [i for i in data.get("output") or [] if isinstance(i, dict)]
    texts: list[str] = []
    calls: list[ToolCall] = []
    for i in output:
        if i.get("type") == "message":
            for c in i.get("content") or []:
                if c.get("type") == "output_text":
                    texts.append(c.get("text") or "")
                elif c.get("type") == "refusal":
                    texts.append(f"(모델이 답변을 거절했습니다: {c.get('refusal') or ''})")
        elif i.get("type") == "function_call":
            calls.append(ToolCall(id=i.get("call_id") or i.get("id") or "", name=i.get("name") or "", arguments=i.get("arguments") or "{}"))
    reason = (data.get("incomplete_details") or {}).get("reason") if data.get("status") == "incomplete" else None
    finish = {"max_output_tokens": "length", "content_filter": "content_filter"}.get(str(reason), None) if reason else None
    if finish is None:
        finish = "tool_calls" if calls else ("stop" if output else "empty")
    u = data.get("usage") or {}
    usage: dict[str, int] = {}
    if u:
        usage = {
            "prompt_tokens": int(u.get("input_tokens") or 0),
            "completion_tokens": int(u.get("output_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0),
            "cached_tokens": int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0),
            "reasoning_tokens": int((u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0),
        }
    content = "".join(texts) or None
    if not output:
        content = "(모델 응답이 비어 있습니다. 콘텐츠 필터 여부를 확인하세요.)"
    return LLMResponse(content=content, tool_calls=calls, finish_reason=finish, usage=usage,
                       raw_items=_replayable(output) or None, raw_source=source)


class AzureResponsesModel(OutputBudget):
    """Responses API 기반 ChatModel (llm.api: responses). 인터페이스·메시지 형식은 AzureChatModel 과 같음."""

    def __init__(self, cfg: LLMConfig, http_client: Any = None):
        cfg.validate()
        self.cfg = cfg
        from openai import AsyncAzureOpenAI

        kwargs: dict[str, Any] = {"api_version": cfg.responses_api_version, "timeout": cfg.timeout, "max_retries": cfg.max_retries}
        if http_client is not None:  # 테스트·사내 프록시용
            kwargs["http_client"] = http_client
        if cfg.extra_headers:
            kwargs["default_headers"] = cfg.extra_headers
        base = responses_base_url(cfg)
        if base:
            kwargs["base_url"] = base  # → POST <리소스>/openai/v1/responses?api-version=v1
        else:
            kwargs["azure_endpoint"] = cfg.endpoint  # → POST <리소스>/openai/responses?api-version=<날짜 버전>
        kwargs.update(_auth_kwargs(cfg))
        self._client = AsyncAzureOpenAI(**kwargs)
        if not hasattr(self._client, "responses"):  # pragma: no cover - 오래된 SDK
            raise RuntimeError("llm.api: responses 는 openai 패키지 1.66 이상이 필요합니다: pip install -U openai")
        self.source = f"{cfg.deployment}@{base or cfg.endpoint}"  # 추론 항목은 같은 배포에만 다시 보냄
        self._cache_key_ok = True
        self._replay_ok = cfg.reasoning_replay
        self.stats = {"replayed": 0}
        self._init_budget()

    async def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str | None = None,
        parallel_tool_calls: bool | None = None,
        model: str | None = None,
        cache_key: str | None = None,
        budget: int | None = None,
        effort: str | None = None,
    ) -> LLMResponse:
        cfg = self.cfg
        items, replayed = responses_input(messages, cfg.vision, self.source if self._replay_ok else "")
        params: dict[str, Any] = {"model": cfg.deployment, "input": items, "store": False}
        if tools:
            params["tools"] = responses_tools(tools)
            if tool_choice:
                params["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                params["parallel_tool_calls"] = parallel_tool_calls
        if budget:
            params["max_output_tokens"] = max(budget, RESPONSES_MIN_OUTPUT_TOKENS)
        if cfg.temperature is not None:
            params["temperature"] = cfg.temperature
        if effort:
            params["reasoning"] = {"effort": effort}
        if self._replay_ok:
            params["include"] = ["reasoning.encrypted_content"]
        body = dict(cfg.extra_body or {})
        if cache_key and cfg.prompt_cache_key and self._cache_key_ok:
            body.setdefault("prompt_cache_key", cache_key[:CACHE_KEY_MAX])
        if body:
            params["extra_body"] = body

        for _ in range(3):  # 배포가 모르는 인자(캐시 키 / 추론 내용 포함)는 빼고 다시
            try:
                resp = await self._client.responses.create(**params)
                break
            except Exception as e:
                if "prompt_cache_key" in body and _rejects_cache_key(e):
                    self._cache_key_ok = False
                    log.warning("배포 %s 가 prompt_cache_key 를 지원하지 않아 빼고 보냅니다: %s", cfg.deployment, str(e)[:200])
                    body.pop("prompt_cache_key")
                    if body:
                        params["extra_body"] = body
                    else:
                        params.pop("extra_body", None)
                    continue
                if "include" in params and getattr(e, "status_code", None) == 400 and \
                        any(k in str(e) for k in ("encrypted_content", "reasoning", "include")):
                    # 추론 모델이 아니거나 이 배포가 추론 내용 반환을 지원하지 않음 → 이어 받기 없이
                    self._replay_ok = False
                    log.warning("배포 %s 가 추론 내용 이어 받기를 지원하지 않아 끄고 보냅니다: %s", cfg.deployment, str(e)[:200])
                    params.pop("include")
                    params["input"], replayed = responses_input(messages, cfg.vision, "")
                    continue
                raise
        else:  # pragma: no cover
            raise RuntimeError("Responses API 호출 재시도 초과")
        data = resp.model_dump(exclude_none=True) if hasattr(resp, "model_dump") else dict(resp)
        out = parse_response(data, self.source if self._replay_ok else "")
        if not self._replay_ok:
            out.raw_items = None
        self.stats["replayed"] += replayed
        if replayed:
            out.usage["reasoning_replayed"] = replayed
        return out

    async def aclose(self) -> None:
        await self._client.close()


def make_chat_model(cfg: LLMConfig) -> "AzureChatModel | AzureResponsesModel":
    """llm.api 에 맞는 클라이언트 (chat: Chat Completions, responses: Responses API)."""
    return AzureResponsesModel(cfg) if cfg.api == "responses" else AzureChatModel(cfg)
