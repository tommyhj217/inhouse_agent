"""LLM ↔ MCP tool 호출 루프 (function calling 에이전트)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import AgentConfig
from .interaction import AskFn, RunContext, current_run
from .llm import ChatModel, LLMResponse, ToolCall
from .mcp_hub import MCPHub

if TYPE_CHECKING:
    from .skills import Skill, SkillStore

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """당신은 사내 업무 자료 수집·분석 에이전트입니다.
사내 사이트들의 자료는 MCP tool(웹 컨트롤)로만 가져올 수 있습니다.

규칙:
1. 요청을 분석해서 어떤 사이트(MCP 서버)의 어떤 tool 이 필요한지 판단하고 직접 호출하세요.
2. 날짜가 필요한 요청('오늘', '어제', '지난주')은 현재 시각 기준으로 정확한 날짜를 계산해서 넣으세요.
3. tool 결과에 없는 내용을 지어내지 마세요. 실패하면 원인(오류 메시지)을 그대로 알려주세요.
4. tool 이 오류를 반환하면 인자를 고쳐 재시도하거나 다른 tool 을 시도하되, 같은 호출을 무의미하게 반복하지 마세요.

파일 다운로드·분석 규칙:
5. MCP tool 로 파일을 내려받은 뒤에는 결과에 붙는 [파일 감지] 점검 결과를 반드시 확인하세요.
   - ⏳ 다운로드 중 → wait_for_download 로 완료를 기다린 뒤 진행
   - ⚠️ 의심(예: 엑셀인데 실제로는 HTML 로그인/오류 페이지) / ❌ 손상 / 빈 파일 → 원인을 판단해 다시 내려받거나 사용자에게 알림
   - 파일 감지가 없는데 다운로드를 했다면 wait_for_download 또는 list_downloads 로 직접 찾으세요.
6. 정상 파일은 analyze_file 로 구조를 파악하고, 계산·필터가 필요하면 query_table 로 정확히 계산하세요.
   숫자를 추측하거나 샘플 행만 보고 합계를 내지 마세요.
7. 결과 보고에는 사용한 파일의 경로와 상태(정상/문제)를 함께 적으세요.

다운로드 링크 규칙:
8. MCP 결과에 파일 다운로드 URL 이 있으면([다운로드 링크 감지]) download_url 로 받으세요.
   받은 파일이 로그인/오류 HTML 이면 MCP 쪽에 쿠키/세션 정보를 주는 tool 이 있는지 찾아 cookies 인자로 다시 받거나,
   MCP 의 브라우저 다운로드 기능을 쓰세요.
   원격 MCP 서버(다른 PC)가 알려준 경로는 에이전트가 자동으로 가져옵니다([원격 MCP 파일 처리]).
   가져오지 못했거나 나중에 생긴 파일은 fetch_remote_file 로 가져오고, 원격에서 아직 받는 중이면 wait_sec 를 주세요.

도구가 없을 때 (동적 도구):
9. 필요한 기능을 하는 tool 이 없으면 포기하지 말고 직접 만드세요.
   - 일회성 계산/변환/파일 가공 → run_python
   - 반복해서 쓸 기능(특정 형식 파싱, 여러 MCP 호출 반복·조합, 사내 REST API 호출 등) → create_tool 로 등록 후 custom__<이름> 으로 호출
   - 코드는 표준 라이브러리 위주로 작성하고, 파일은 ctx.workspace / ctx.download_dir 아래에서 다루세요.
   - 기존 tool 은 ctx.call_tool_data("tool이름", {...}) 로 호출하면 dict/list 로 받습니다 (텍스트는 ctx.call_tool).
     파일을 받는 tool 은 ctx.call_tool_files("download_url", {"url": ...}) 로 받은 파일 경로/상태를 얻으세요.
   - create_tool 에는 반드시 test_args 를 넣어 실제로 돌려 보고, 실패하면 오류(traceback)를 보고 고쳐서 다시 만드세요.
   - 같은 기능의 custom__ 도구가 이미 있으면 새로 만들지 말고 그것을 쓰세요.

결과 파일 / 범용 도구:
- 만든 파일(save_file, write_excel, make_chart, create_zip, sql_query save_as 등)과 받은 파일은 최종 답변에 전체 경로를 꼭 적으세요.
  웹 화면에서 사용자가 경로를 클릭해 바로 받을 수 있습니다. 메일/메신저로 공유할 때는 get_download_link 의 링크를 쓰세요.
- 3단계 이상 걸리는 작업은 update_plan 으로 계획을 세우고 진행 상황을 갱신하세요.
- 여러 파일 조인·복잡한 집계는 sql_query, 이전 자료와의 비교는 compare_tables, 보고서는 write_excel/make_chart 를 쓰세요.
- 웹 페이지는 web_fetch 로 빠르게 읽고, 사내 API 는 http_request 로 호출하세요.
- 다음에도 쓸 요령(사이트별 조회 방법, 사용자 선호 형식 등)은 remember 로 저장하고, 작업 전에 recall 로 확인하세요.

스킬/대화 규칙:
10. 요청이 아래 '등록된 스킬' 설명과 맞으면 먼저 use_skill 로 지침을 불러와 그 절차를 따르세요.
11. 조건이 애매하거나 여러 선택지 중 골라야 하면 ask_user 로 확인하세요. 단, 꼭 필요할 때만 쓰세요.
12. 최종 답변은 한국어로, 핵심 요약 → 상세 자료(표/목록) → 출처(사이트/tool/파일) 순서로 작성하세요.
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
    files: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentResult:
    output: str
    messages: list[dict[str, Any]]
    tool_events: list[ToolEvent] = field(default_factory=list)
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    stopped_reason: str = "completed"  # completed | max_iterations | error | cancelled
    elapsed: float = 0.0
    files: list[dict[str, Any]] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.stopped_reason == "completed"


EventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class Agent:
    def __init__(self, llm: ChatModel, hub: MCPHub, cfg: AgentConfig, skills: "SkillStore | None" = None):
        self.llm = llm
        self.hub = hub
        self.cfg = cfg
        self.skills = skills

    # ------------------------------------------------------------------
    def build_system_prompt(self, servers: list[str] | None = None, extra: str | None = None) -> str:
        now = datetime.now(ZoneInfo(self.cfg.timezone))
        parts = [
            (self.cfg.system_prompt or DEFAULT_SYSTEM_PROMPT).strip(),
            f"현재 시각: {now.strftime('%Y-%m-%d %H:%M (%A)')} [{self.cfg.timezone}]",
            "사용 가능한 사내 사이트(MCP 서버):\n" + self.hub.describe_servers(servers),
        ]
        ws = getattr(self.hub, "workspace", None)
        if ws is not None:
            parts.append(
                "파일 작업 폴더: " + str(ws.root) + "\n다운로드 감시 폴더: " + ", ".join(str(d) for d in ws.watch_dirs)
            )
        mem = ws.root / "memory.json" if ws is not None else None
        if mem is not None and mem.is_file():
            try:
                keys = list(json.loads(mem.read_text(encoding="utf-8")))
            except ValueError:
                keys = []
            if keys:
                parts.append(f"저장된 기억 {len(keys)}개 (recall 로 조회): " + ", ".join(keys[:30]))
        if self.skills is not None:
            catalog = self.skills.catalog_text()
            if catalog:
                parts.append("등록된 스킬 (use_skill 로 지침 불러오기):\n" + catalog)
        if extra:
            parts.append(extra.strip())
        return "\n\n".join(parts)

    def new_conversation(self, servers: list[str] | None = None, extra_system: str | None = None) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.build_system_prompt(servers, extra_system)}]

    @staticmethod
    def skill_system(skill: "Skill", inputs: dict[str, Any], timezone: str) -> str:
        return (
            f"# 지금 실행할 스킬: {skill.name}\n설명: {skill.description}\n"
            f"입력값: {json.dumps(inputs, ensure_ascii=False)}\n"
            + (f"스킬 참고 파일: {', '.join(skill.files)} (read_skill_file 로 읽기)\n" if skill.files else "")
            + "\n아래 지침을 순서대로 따르세요. use_skill 을 다시 호출할 필요는 없습니다.\n\n"
            + skill.render(inputs, timezone)
        )

    # ------------------------------------------------------------------
    async def run(
        self,
        prompt: str,
        *,
        messages: list[dict[str, Any]] | None = None,
        servers: list[str] | None = None,
        extra_system: str | None = None,
        on_event: EventCallback | None = None,
        ask: AskFn | None = None,
        skill: "Skill | None" = None,
        skill_inputs: dict[str, Any] | None = None,
    ) -> AgentResult:
        """프롬프트 1건 처리. messages 를 주면 이어지는 대화로 처리하고 그 리스트를 갱신합니다.

        ask: 사용자에게 질문하는 함수(웹/CLI). None 이면 ask_user 는 기본값으로 진행하도록 안내됩니다.
        skill: 주면 해당 스킬 지침을 시스템 프롬프트에 넣고 실행합니다(스킬 테스트/예약작업).
        """
        started = time.monotonic()
        if skill is not None:
            inputs = skill.resolve_inputs(skill_inputs, self.cfg.timezone)
            sk_sys = self.skill_system(skill, inputs, self.cfg.timezone)
            extra_system = f"{extra_system}\n\n{sk_sys}" if extra_system else sk_sys
            if servers is None and skill.servers:
                servers = skill.servers
            if not prompt.strip():
                prompt = f"'{skill.name}' 스킬을 입력값 {json.dumps(inputs, ensure_ascii=False)} 로 실행하세요."
        if messages is None:
            messages = self.new_conversation(servers, extra_system)
        elif messages and messages[0].get("role") == "system":
            # 대화가 길어져도 현재 시각/서버/스킬 상태는 최신으로 유지
            messages[0] = {"role": "system", "content": self.build_system_prompt(servers, extra_system)}
        messages.append({"role": "user", "content": prompt})
        self._trim_history(messages)

        ctx = RunContext(ask=ask, interactive=ask is not None)
        token = current_run.set(ctx)
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

        ctx.meta["emit"] = emit  # update_plan 등 내장 tool 이 화면에 진행 상황을 보낼 수 있게

        def result(output: str, reason: str, it: int) -> AgentResult:
            files: dict[str, dict[str, Any]] = {}
            for e in events:
                for f in e.files:
                    files[f.get("path", "")] = f
            return AgentResult(
                output, messages, events, it, usage, reason, time.monotonic() - started,
                list(files.values()), list(dict.fromkeys(ctx.meta.get("skills_used", []))),
            )

        iteration = 0
        resp: LLMResponse | None = None
        try:
            while iteration < self.cfg.max_iterations:
                iteration += 1
                await emit("llm_start", {"iteration": iteration})
                tools = self.hub.openai_tools(servers)  # 실행 중 만든 동적 도구도 바로 반영
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
                    return result(output, "completed", iteration)

                if resp.content:
                    await emit("thought", {"content": resp.content})
                tool_results = await self._run_tool_calls(resp.tool_calls, emit)
                for tc, ev in zip(resp.tool_calls, tool_results):
                    events.append(ev)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": ev.result})

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
            return result(output, "max_iterations", iteration)
        except asyncio.CancelledError:
            _repair_dangling_tool_calls(messages, "사용자가 실행을 중단했습니다")
            messages.append({"role": "assistant", "content": "(사용자 요청으로 실행이 중단되었습니다)"})
            raise
        except Exception as e:
            log.exception("에이전트 실행 오류")
            _repair_dangling_tool_calls(messages)
            msg = f"에이전트 실행 중 오류가 발생했습니다: {type(e).__name__}: {e}"
            await emit("error", {"error": msg})
            return result(msg, "error", iteration)
        finally:
            current_run.reset(token)

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
                await emit("tool_end", _ev_dict(ev, tc.id))
                return ev
            ev = ToolEvent(tc.name, route[0], route[1], args)
            await emit("tool_start", _ev_dict(ev, tc.id))
            t0 = time.monotonic()
            out = await self.hub.call(tc.name, args)
            ev.elapsed = time.monotonic() - t0
            ev.result = _truncate(out.text, self.cfg.max_tool_result_chars)
            ev.is_error = out.is_error
            ev.files = out.files
            if ev.is_error and not ev.result.startswith("오류"):
                ev.result = "오류: " + ev.result
            await emit("tool_end", _ev_dict(ev, tc.id))
            return ev

        # 같은 서버 호출은 hub 가 직렬화하므로 여기선 동시에 실행해도 안전
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


def _ev_dict(ev: ToolEvent, call_id: str = "") -> dict[str, Any]:
    return {
        "id": call_id,
        "function": ev.function,
        "server": ev.server,
        "tool": ev.tool,
        "arguments": ev.arguments,
        "result": ev.result,
        "is_error": ev.is_error,
        "elapsed": round(ev.elapsed, 2),
        "files": ev.files,
    }


def _repair_dangling_tool_calls(messages: list[dict[str, Any]], reason: str = "실행되지 않음") -> None:
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
                    messages.insert(insert_at, {"role": "tool", "tool_call_id": tc["id"], "content": f"오류: {reason}"})
            break
