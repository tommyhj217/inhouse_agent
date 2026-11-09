"""LLM 없이 실행하는 워크플로 (토큰 없이 스킬/기능 테스트).

스킬 SKILL.md 머리말에 steps 를 적으면, LLM 이 없거나 mode=workflow 일 때 순서대로 tool 을 직접 호출합니다.

steps:
  - id: dl                         # (선택) 결과를 뒤에서 쓸 이름
    tool: demo__download_sales_excel
    args: {month: "{month}", wait: true}
  - id: dept
    tool: query_table
    args: {path: "{steps.dl.file}", group_by: [부서], aggregates: [{column: 금액, func: sum}]}
    on_error: stop                 # stop(기본) | continue
    when: "{dept} != 전체"          # (선택) 조건이 맞을 때만 실행 (==, !=, 값만 쓰면 비어있지 않을 때)
  - id: menu
    script: scripts/make_menu.py     # 스킬 폴더의 스크립트 실행 (= tool: run_skill_script)
    args: {date: "{today}"}          # → --date 2026-11-05
output: |                          # (선택) 최종 결과 템플릿. 생략하면 단계별 결과를 이어 붙임
  ## {month} 매출
  {steps.dept.text}

템플릿 변수
  {입력이름}, {today} {yesterday} {last_month} … (날짜), {prev} (바로 앞 단계 결과 텍스트)
  {attachment} {attachments} (채팅에 첨부한 파일 경로 - 첫 파일 / 전체 줄바꿈 목록)
  {steps.<id>.text}  {steps.<id>.file} (첫 파일 경로)  {steps.<id>.files}  {steps.<id>.error}
  {steps.<id>.data.<키>.<0>…} (구조화 결과: dict 키 / 리스트 번호)
  필터: {paths|lines} (여러 줄/쉼표 → 목록), {x|json}, {list|first}
  값 전체가 변수 하나면 원래 타입(숫자/리스트/dict)으로 들어갑니다.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .agent import AgentResult, ToolEvent, _ev_dict, _truncate
from .interaction import AskFn, RunContext, current_run
from .runtime_utils import render_dates

_VAR = re.compile(r"\{([A-Za-z_가-힣][\w가-힣]*(?:\.[\w가-힣-]+)*(?:\|[a-z]+)?)\}")
_FILTERS = {
    "lines": lambda v: [x.strip() for x in re.split(r"[\n,]", str(v or "")) if x.strip()],  # 여러 줄/쉼표 → 목록
    "json": lambda v: json.dumps(v, ensure_ascii=False),
    "first": lambda v: (v[0] if isinstance(v, list) and v else v),
}


class WorkflowError(ValueError):
    pass


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    if steps in (None, []):
        return []
    if not isinstance(steps, list):
        raise WorkflowError("steps 는 목록이어야 합니다.")
    out = []
    ids = set()
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or not s.get("tool"):
            raise WorkflowError(f"steps[{i}] 에 tool 이 필요합니다.")
        sid = str(s.get("id") or f"s{i}")
        if sid in ids:
            raise WorkflowError(f"steps id 가 중복됩니다: {sid}")
        ids.add(sid)
        if s.get("args") is not None and not isinstance(s["args"], dict):
            raise WorkflowError(f"steps[{i}].args 는 key: value 형태여야 합니다.")
        on_error = str(s.get("on_error") or "stop")
        if on_error not in ("stop", "continue"):
            raise WorkflowError(f"steps[{i}].on_error 는 stop 또는 continue")
        out.append({"id": sid, "tool": str(s["tool"]), "args": s.get("args") or {}, "on_error": on_error, "title": s.get("title") or "",
                    "when": str(s["when"]) if s.get("when") is not None else None})
    return out


def _lookup(path: str, ctx: dict[str, Any]) -> tuple[bool, Any]:
    if "|" in path:
        base, flt = path.split("|", 1)
        ok, v = _lookup(base, ctx)
        if not ok or flt not in _FILTERS:
            return False, None
        return True, _FILTERS[flt](v)
    parts = path.split(".")
    if parts[0] == "steps" and len(parts) >= 3:
        st = ctx["steps"].get(parts[1])
        if st is None:
            return True, ""  # 아직 실행 안 된(또는 실패로 건너뛴) 단계
        field = parts[2]
        if field == "data":
            cur: Any = st.get("data")
            for p in parts[3:]:
                if isinstance(cur, list) and p.lstrip("-").isdigit():
                    idx = int(p)
                    cur = cur[idx] if -len(cur) <= idx < len(cur) else None
                elif isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    cur = None
            return True, cur
        if field == "file":
            return True, (st.get("files") or [""])[0]
        if field in ("text", "files", "error"):
            return True, st.get(field)
        return False, None
    if path == "prev":
        return True, ctx.get("prev", "")
    if path in ctx["inputs"]:
        return True, ctx["inputs"][path]
    return False, None


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """문자열/리스트/dict 안의 {변수} 치환. 값 전체가 변수 하나면 원래 타입 유지."""
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    whole = _VAR.fullmatch(value.strip())
    if whole:
        ok, v = _lookup(whole.group(1), ctx)
        if ok:
            return v
    text = render_dates(value, ctx["timezone"])

    def repl(m: re.Match[str]) -> str:
        ok, v = _lookup(m.group(1), ctx)
        if not ok:
            return m.group(0)  # 모르는 {..} 는 그대로 (JSON 예시 등)
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    return _VAR.sub(repl, text)


def check_when(expr: str | None, ctx: dict[str, Any]) -> bool:
    if expr is None:
        return True
    text = str(render(expr, ctx)).strip()
    for op in ("!=", "=="):
        if op in text:
            a, b = (x.strip().strip("'\"") for x in text.split(op, 1))
            return (a == b) if op == "==" else (a != b)
    return text.lower() not in ("", "false", "0", "no", "none", "아니오")


async def run_workflow(
    hub: Any,
    steps: list[dict[str, Any]],
    inputs: dict[str, Any],
    *,
    timezone: str,
    output_template: str | None = None,
    title: str = "",
    on_event: Any = None,
    ask: AskFn | None = None,
    max_chars: int = 30000,
) -> AgentResult:
    """steps 를 순서대로 직접 실행 (LLM 호출 없음). 화면에는 에이전트 실행과 같은 이벤트로 보임."""
    started = time.monotonic()
    ctx: dict[str, Any] = {"inputs": dict(inputs), "steps": {}, "prev": "", "timezone": timezone}
    events: list[ToolEvent] = []
    run_ctx = RunContext(ask=ask, interactive=ask is not None)
    token = current_run.set(run_ctx)

    async def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event is not None:
            r = on_event(kind, data)
            if hasattr(r, "__await__"):
                await r

    stopped = "completed"
    error: dict[str, Any] | None = None
    lines: list[str] = []
    try:
        run_ctx.meta["emit"] = emit
        status = ["pending"] * len(steps)

        async def plan() -> None:
            await emit("plan", {"steps": [{"step": s["title"] or s["tool"], "status": st} for s, st in zip(steps, status)], "note": "LLM 없이 워크플로로 실행"})

        await plan()
        for i, step in enumerate(steps):
            if not check_when(step.get("when"), ctx):
                lines.append(f"### {i + 1}. {step['title'] or step['tool']} (조건 불일치로 건너뜀: {step['when']})")
                status[i] = "skipped"
                await plan()
                continue
            status[i] = "in_progress"
            await plan()
            tool = str(render(step["tool"], ctx))
            args = render(step["args"], ctx)
            route = hub.route_of(tool) or ("?", tool)
            ev = ToolEvent(tool, route[0], route[1], args if isinstance(args, dict) else {})
            call_id = f"wf{i + 1}"
            await emit("tool_start", _ev_dict(ev, call_id))
            t0 = time.monotonic()
            if hub.route_of(tool) is None:
                out_text, is_err, files, data = f"오류: 알 수 없는 tool '{tool}' (도구 목록은 / 메뉴 참고)", True, [], None
            else:
                out = await hub.call(tool, args)
                out_text, is_err, files, data = out.text, out.is_error, out.files, out.data
            ev.elapsed = time.monotonic() - t0
            ev.result, ev.is_error, ev.files = _truncate(out_text, max_chars), is_err, files
            from .agent import view_of

            ev.view = view_of(data)
            events.append(ev)
            await emit("tool_end", _ev_dict(ev, call_id))
            paths = [f["path"] for f in files if f.get("path")]
            if data is None:
                try:
                    data = json.loads(out_text)
                except (ValueError, TypeError):
                    data = None
            ctx["steps"][step["id"]] = {"text": out_text, "files": paths, "data": data, "error": out_text if is_err else ""}
            ctx["prev"] = out_text
            lines.append(f"### {i + 1}. {step['title'] or tool}" + (" ❌" if is_err else "") + f"\n{_truncate(out_text, 4000)}")
            status[i] = "done" if not is_err else "skipped"
            await plan()
            if is_err and step["on_error"] == "stop":
                stopped = "error"
                name = step["title"] or tool
                single = len(steps) == 1
                error = {"code": "step_failed", "retryable": True, "detail": out_text.removeprefix("오류: ")[:400],
                         "title": f"'{name}' 실행 중 오류가 났습니다" if single else f"{i + 1}단계 '{name}' 에서 오류가 나서 멈췄습니다",
                         "hint": "입력값(경로·날짜 등)과 사이트(MCP 서버) 상태를 확인하고 다시 실행하세요." if single else
                                 "단계의 입력값과 사이트(MCP 서버) 상태를 확인하세요. 이 단계가 실패해도 계속하려면 SKILL.md 의 그 단계에 on_error: continue 를 넣으세요."}
                head = f"❌ **{error['title']}**\n- 원인: {error['detail']}\n- 조치: {error['hint']}\n"
                lines = [head] if single else [head, *lines]  # 단계가 하나면 같은 오류를 두 번 보여주지 않음
                break
        if output_template and stopped == "completed":
            output = str(render(output_template, ctx))
        else:
            output = (f"# {title}\n" if title and not error else "") + "\n\n".join(lines)
        await emit("final", {"content": output})
    finally:
        current_run.reset(token)
    files: dict[str, dict[str, Any]] = {}
    for e in events:
        for f in e.files:
            files[f.get("path", "")] = f
    messages = [
        {"role": "user", "content": f"[워크플로 실행] {title} 입력값 {json.dumps(inputs, ensure_ascii=False)}"},
        {"role": "assistant", "content": output},
    ]
    return AgentResult(output, messages, events, len(events), {}, stopped, time.monotonic() - started, list(files.values()), [], error)
