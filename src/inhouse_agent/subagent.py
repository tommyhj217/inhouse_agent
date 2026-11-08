"""하위 에이전트 (run_subagent): 큰 작업을 나눠 각각 별도 대화에서 처리하고 결과 요약만 돌려받음.

쓸모 있는 경우
- 첨부 파일·과거 보고서가 여러 개일 때 파일마다 깊게 분석 (각자 별도 대화 → 본 대화가 커지지 않음)
- 여러 사이트(MCP 서버)에서 각각 자료를 모을 때 (한 번에 여러 개 호출하면 동시에 실행)
- 규칙집을 만들 때 과거 보고서를 하나씩 읽고 장별 특징을 정리
쓰지 않는 경우: 단순 조회 하나, LLM 이 없는 상태(토큰 없이)

규칙: 깊이 1 (하위 작업자는 다시 하위 작업자를 부르지 않음), 사용자에게 질문하지 않음,
      같은 사용자 작업폴더 사용, 쓴 토큰은 상위 실행 사용량에 합산.
"""

from __future__ import annotations

import contextvars
import dataclasses
import time
import uuid
from typing import TYPE_CHECKING, Any

from .interaction import current_run
from .mcp_hub import BuiltinTool, ToolOutcome

if TYPE_CHECKING:
    from .runtime import AgentRuntime

_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("subagent_depth", default=0)

SUB_SYSTEM = """# 하위 작업자 역할
당신은 상위 에이전트가 맡긴 하위 작업 하나만 처리하는 작업자입니다.
- 사용자에게 질문하지 마세요(ask_user 금지). 애매하면 합리적인 기본값으로 진행하고 가정을 결과에 적으세요.
- 맡은 범위만 처리하고, run_subagent 는 쓰지 마세요.
- 끝나면 상위 에이전트가 바로 쓸 수 있게 짧게 정리하세요:
  1) 핵심 결과 (숫자는 정확히) 2) 근거가 된 파일 경로·출처 3) 확인하지 못한 점"""


def make_subagent_tool(rt: "AgentRuntime") -> BuiltinTool:
    acfg = rt.cfg.agent

    async def run_subagent(args: dict[str, Any]) -> ToolOutcome:
        if _DEPTH.get() >= 1:
            return ToolOutcome("오류: 하위 작업 안에서는 하위 에이전트를 다시 부를 수 없습니다. 직접 처리하세요.", True)
        ready, why = rt.llm_status()
        if not ready:
            return ToolOutcome(f"오류: 하위 에이전트는 LLM 이 있어야 합니다 ({why}).", True)
        task = str(args.get("task") or "").strip()
        if not task:
            return ToolOutcome("오류: task(맡길 작업 설명)가 필요합니다.", True)
        files = [str(f) for f in args.get("files") or []]
        servers = [str(s) for s in args.get("servers") or []] or None
        max_it = max(2, min(int(args.get("max_iterations") or acfg.subagent_max_iterations), acfg.max_iterations))

        from .agent import Agent

        sub = Agent(rt.llm, rt.hub, dataclasses.replace(acfg, max_iterations=max_it), rt.skills)
        sub.rules_path = rt.rules_path
        sub.recent_sessions_in_prompt = 0
        parent = current_run.get()
        emit = parent.meta.get("emit") if parent is not None else None
        sid = uuid.uuid4().hex[:8]
        label = task.splitlines()[0][:60]

        async def on_event(kind: str, data: dict[str, Any]) -> None:
            if emit is not None and kind in ("tool_start", "tool_end", "final", "error", "compact"):
                await emit("subagent", {"sid": sid, "task": label, "event": kind, "tool": data.get("function"),
                                        "is_error": data.get("is_error"), "elapsed": data.get("elapsed")})

        if emit is not None:
            await emit("subagent", {"sid": sid, "task": label, "event": "start"})
        token = _DEPTH.set(_DEPTH.get() + 1)
        t0 = time.monotonic()
        try:
            res = await sub.run(task, servers=servers, extra_system=SUB_SYSTEM, on_event=on_event, ask=None,
                                attachments=files or None)
        finally:
            _DEPTH.reset(token)
        if parent is not None:  # 하위 작업 토큰을 상위 실행 사용량에 합산
            parent.meta.setdefault("sub_usage", []).append(dict(res.usage))
            parent.meta.setdefault("skills_used", []).extend(res.skills_used)
        tools = [e.function for e in res.tool_events]
        if emit is not None:
            await emit("subagent", {"sid": sid, "task": label, "event": "done", "status": res.stopped_reason,
                                    "elapsed": round(time.monotonic() - t0, 1), "tokens": res.usage.get("total_tokens", 0)})
        text = (f"[하위 작업 결과] {label}\n{res.output}\n\n"
                f"(상태 {res.stopped_reason} · 도구 {len(tools)}회{': ' + ', '.join(dict.fromkeys(tools)) if tools else ''} · "
                f"{res.elapsed:.1f}초 · 토큰 {res.usage.get('total_tokens', 0):,})")
        return ToolOutcome(text, not res.success, list(res.files))

    return BuiltinTool(
        "run_subagent",
        "하위 에이전트에게 작업 하나를 맡겨 별도 대화에서 처리하고 결과 요약만 받습니다. 한 응답에서 여러 개를 부르면 동시에 실행됩니다. "
        "파일·보고서가 여러 개라 각각 깊게 분석해야 할 때, 여러 사이트에서 각각 자료를 모을 때 쓰세요. "
        "단순한 조회 하나에는 쓰지 말고 직접 처리하세요. 하위 에이전트는 사용자에게 질문하지 않으니 task 에 조건(날짜·부서·형식)을 모두 적으세요.",
        {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "맡길 작업 (무엇을, 어떤 조건으로, 어떤 형식으로 돌려줄지)"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "분석할 파일 경로 (선택)"},
                "servers": {"type": "array", "items": {"type": "string"}, "description": "쓸 MCP 서버 이름 (선택, 생략 시 전체)"},
                "max_iterations": {"type": "integer", "description": f"최대 도구 왕복 횟수 (기본 {acfg.subagent_max_iterations})"},
            },
            "required": ["task"],
        },
        run_subagent,
    )
