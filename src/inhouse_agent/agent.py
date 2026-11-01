"""LLM ↔ MCP tool 호출 루프 (ReAct 스타일 function calling 에이전트)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import AgentConfig
from .llm import ChatModel, LLMResponse, ToolCall
from .mcp_hub import MCPHub

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """당신은 사내 업무 자료 수집 에이전트입니다.
사내 사이트들의 자료는 MCP tool(웹 컨트롤)로만 가져올 수 있습니다.

규칙:
1. 사용자의 요청을 분석해서 어떤 사이트(MCP 서버)의 어떤 tool 이 필요한지 판단하고 직접 호출하세요.
2. 날짜가 필요한 요청('오늘', '어제', '지난주')은 현재 시각을 기준으로 정확한 날짜를 계산해서 tool 인자로 넣으세요.
3. tool 결과에 없는 내용을 지어내지 마세요. 자료를 가져오지 못했다면 실패 원인(오류 메시지)을 그대로 알려주세요.
4. tool 이 오류를 반환하면 인자를 고쳐 재시도하거나 다른 tool 을 시도하되, 같은 호출을 무의미하게 반복하지 마세요.
5. 여러 사이트의 자료가 필요하면 각각 가져온 뒤 종합해서 정리하세요.
6. 최종 답변은 한국어로, 핵심 요약 → 상세 자료(표/목록) → 출처(사용한 사이트/tool) 순서로 작성하세요.
7. 사용자가 파일 저장을 요청하면 save_file tool 로 저장하고 저장 경로를 알려주세요.
"""


@dataclass
class ToolEvent:
    function: str
    server: str
    tool: str
    arguments: dict[str, Any]
    result: str = ""
    is_error: bool = False
    elapsed: float = 0.0


@dataclass
class AgentResult:
    output: str
    messages: list[dict[str, Any]]
    tool_events: list[ToolEvent] = field(default_factory=list)
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    stopped_reason: str = "completed"  # completed | max_iterations | error
    elapsed: float = 0.0

    @property
    def success(self) -> bool:
        return self.stopped_reason == "completed"


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class Agent:
    def __init__(self, llm: ChatModel, hub: MCPHub, cfg: AgentConfig):
        self.llm = llm
        self.hub = hub
        self.cfg = cfg

    # ------------------------------------------------------------------
    def build_system_prompt(self, servers: list[str] | None = None, extra: str | None = None) -> str:
        now = datetime.now(ZoneInfo(self.cfg.timezone))
        parts = [
            (self.cfg.system_prompt or DEFAULT_SYSTEM_PROMPT).strip(),
            f"현재 시각: {now.strftime('%Y-%m-%d %H:%M (%A)')} [{self.cfg.timezone}]",
            "사용 가능한 사내 사이트(MCP 서버):\n" + self.hub.describe_servers(servers),
        ]
        if extra:
            parts.append(extra.strip())
        return "\n\n".join(parts)

    def new_conversation(self, servers: list[str] | None = None, extra_system: str | None = None) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.build_system_prompt(servers, extra_system)}]

    # ------------------------------------------------------------------
    async def run(
        self,
        prompt: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        servers: list[str] | None = None,
        extra_system: str | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentResult:
        """프롬프트 1건을 처리. messages 를 주면 이어지는 대화로 처리하고 그 리스트를 갱신합니다."""
        started = time.monotonic()
        if messages is None:
            messages = self.new_conversation(servers, extra_system)
        elif messages and messages[0].get("role") == "system":
            # 대화가 길어져도 현재 시각/서버 상태는 최신으로 유지
            messages[0] = {"role": "system", "content": self.build_system_prompt(servers, extra_system)}
        messages.append({"role": "user", "content": prompt})
        self._trim_history(messages)

        tools = self.hub.openai_tools(servers)
        events: list[ToolEvent] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        async def emit(kind: str, data: dict[str, Any]) -> None:
            if on_event is None:
                return
            try:
                r = on_event(kind, data)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001
                log.exception("on_event 콜백 오류")

        iteration = 0
        resp: LLMResponse | None = None
        try:
            while iteration < self.cfg.max_iterations:
                iteration += 1
                await emit("llm_start", {"iteration": iteration})
                resp = await self.llm.chat(
                    messages,
                    tools or None,
                    parallel_tool_calls=self.cfg.parallel_tool_calls if tools else None,
                )
                _add_usage(usage, resp.usage)
                messages.append(resp.to_message())

                if not resp.tool_calls:
                    output = resp.content or ""
                    await emit("final", {"content": output})
                    return AgentResult(output, messages, events, iteration, usage, "completed", time.monotonic() - started)

                if resp.content:
                    await emit("thought", {"content": resp.content})
                results = await self._run_tool_calls(resp.tool_calls, emit)
                for tc, ev in zip(resp.tool_calls, results):
                    events.append(ev)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": ev.result})

            # 반복 한도 초과 → tool 없이 지금까지 결과로 마무리 답변 요청
            messages.append(
                {
                    "role": "user",
                    "content": "tool 호출 한도에 도달했습니다. 더 이상 tool 을 호출하지 말고 지금까지 수집한 내용만으로 최종 답변을 작성하세요.",
                }
            )
            resp = await self.llm.chat(messages, None)
            _add_usage(usage, resp.usage)
            messages.append(resp.to_message())
            output = resp.content or ""
            await emit("final", {"content": output})
            return AgentResult(output, messages, events, iteration, usage, "max_iterations", time.monotonic() - started)
        except Exception as e:
            log.exception("에이전트 실행 오류")
            # 대화 이력이 tool_call 짝이 깨진 상태로 남지 않도록 정리
            _repair_dangling_tool_calls(messages)
            msg = f"에이전트 실행 중 오류가 발생했습니다: {type(e).__name__}: {e}"
            await emit("error", {"error": msg})
            return AgentResult(msg, messages, events, iteration, usage, "error", time.monotonic() - started)

    # ------------------------------------------------------------------
    async def _run_tool_calls(self, calls: list[ToolCall], emit: Callable[[str, dict[str, Any]], Awaitable[None]]) -> list[ToolEvent]:
        async def one(tc: ToolCall) -> ToolEvent:
            route = self.hub.route_of(tc.name) or ("?", tc.name)
            try:
                args = json.loads(tc.arguments) if tc.arguments and tc.arguments.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("arguments 는 JSON 객체여야 합니다.")
            except (json.JSONDecodeError, ValueError) as e:
                ev = ToolEvent(tc.name, route[0], route[1], {}, f"오류: tool 인자 JSON 파싱 실패({e}). 올바른 JSON 객체로 다시 호출하세요.", True)
                await emit("tool_end", _ev_dict(ev))
                return ev
            ev = ToolEvent(tc.name, route[0], route[1], args)
            await emit("tool_start", _ev_dict(ev))
            t0 = time.monotonic()
            text, is_err = await self.hub.call(tc.name, args)
            ev.elapsed = time.monotonic() - t0
            ev.result = _truncate(text, self.cfg.max_tool_result_chars)
            ev.is_error = is_err
            if is_err and not ev.result.startswith("오류"):
                ev.result = "오류: " + ev.result
            await emit("tool_end", _ev_dict(ev))
            return ev

        # 서버별 직렬화는 hub 가 처리하므로 여기선 동시에 실행해도 안전
        return list(await asyncio.gather(*(one(tc) for tc in calls)))

    def _trim_history(self, messages: list[dict[str, Any]]) -> None:
        """오래된 대화 삭제 (system 유지, user 메시지 경계에서 잘라 tool_call 짝이 깨지지 않게)."""
        limit = self.cfg.max_history_messages
        if len(messages) <= limit:
            return
        system = messages[0] if messages and messages[0].get("role") == "system" else None
        body = messages[1:] if system else messages[:]
        while len(body) + (1 if system else 0) > limit:
            idx = next((i for i in range(1, len(body)) if body[i].get("role") == "user"), None)
            if idx is None:
                break
            body = body[idx:]
        messages[:] = ([system] if system else []) + body


def _add_usage(total: dict[str, int], u: dict[str, int]) -> None:
    for k, v in (u or {}).items():
        total[k] = total.get(k, 0) + int(v or 0)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.8)
    tail = limit - head
    return f"{text[:head]}\n\n...(중략: 전체 {len(text)}자 중 {limit}자만 전달)...\n\n{text[-tail:]}"


def _ev_dict(ev: ToolEvent) -> dict[str, Any]:
    return {
        "function": ev.function,
        "server": ev.server,
        "tool": ev.tool,
        "arguments": ev.arguments,
        "result": ev.result,
        "is_error": ev.is_error,
        "elapsed": round(ev.elapsed, 2),
    }


def _repair_dangling_tool_calls(messages: list[dict[str, Any]]) -> None:
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            missing = [tc for tc in m["tool_calls"] if tc["id"] not in answered]
            if missing:
                insert_at = i + 1
                while insert_at < len(messages) and messages[insert_at].get("role") == "tool":
                    insert_at += 1
                for tc in missing:
                    messages.insert(insert_at, {"role": "tool", "tool_call_id": tc["id"], "content": "오류: 실행되지 않음"})
            break
