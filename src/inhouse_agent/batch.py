"""도구 묶음 실행 (run_tool_batch): 여러 도구 호출을 LLM 왕복 한 번으로.

pi 의 codemode(모델이 스크립트로 여러 도구를 부르고 결과를 걸러서 요약만 받음)를 안전한 선언형으로 옮긴 것.
코드를 실행하지 않고, 스킬 SKILL.md 의 steps 와 같은 형식(workflow.py 템플릿)을 씁니다.

쓸모
- 앞 결과를 받아 다음 도구를 부르는 연쇄: 검색 → 상세 조회 → 다운로드 (원래는 LLM 왕복 3번 → 1번)
- 목록의 항목마다 같은 도구: for_each 로 동시에 실행 (원래는 항목 수만큼 왕복하거나 한 응답에 직접 나열)
- 큰 결과 줄이기: select / where / fields / limit 로 필요한 부분만 남기고(다음 단계도 줄인 값을 받음, 원본은 raw),
  hide 로 중간 결과는 모델에게 보여주지 않음

steps 예
  - {id: list, tool: groupware__search_notices, args: {keyword: 인사, date_from: "{last_month_start}"},
     select: items, fields: [id, title, date], hide: true}
  - {id: detail, tool: groupware__get_notice, for_each: "{steps.list.data.items}", args: {id: "{item.id}"},
     fields: [title, body], limit: 10}

규칙: 묶음 안에서 run_tool_batch 를 다시 부를 수 없음, 단계 최대 15개, for_each 항목 최대 20개(동시 4개).
"""

from __future__ import annotations

import asyncio
import contextvars
import difflib
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from .interaction import current_run
from .mcp_hub import BuiltinTool, ToolOutcome
from .workflow import WorkflowError, check_when, render, walk

if TYPE_CHECKING:
    from .runtime import AgentRuntime

MAX_STEPS = 15
MAX_ITEMS = 20
CONCURRENCY = 4
STEP_CHARS = 4000  # 단계 하나가 모델에게 보여줄 최대 글자 (max_chars 로 바꿀 수 있음)

_IN_BATCH: contextvars.ContextVar[bool] = contextvars.ContextVar("in_tool_batch", default=False)


def validate(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):  # 모델이 steps 를 JSON 문자열로 보내는 경우
        try:
            raw = json.loads(raw)
        except ValueError as e:
            raise WorkflowError(f"steps 가 올바른 JSON 이 아닙니다: {e}") from e
    if not isinstance(raw, list) or not raw:
        raise WorkflowError("steps 에 실행할 단계를 1개 이상 넣으세요: [{tool, args}, ...]")
    if len(raw) > MAX_STEPS:
        raise WorkflowError(f"단계는 최대 {MAX_STEPS}개입니다 (받은 수 {len(raw)}). 나눠서 부르세요.")
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict) or not s.get("tool"):
            raise WorkflowError(f"steps[{i}] 에 tool(도구 이름)이 필요합니다.")
        sid = str(s.get("id") or f"s{i}")
        if sid in ids:
            raise WorkflowError(f"steps id 가 중복됩니다: {sid}")
        ids.add(sid)
        args = s.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError as e:
                raise WorkflowError(f"steps[{i}].args 가 올바른 JSON 이 아닙니다: {e}") from e
        if not isinstance(args, dict):
            raise WorkflowError(f"steps[{i}].args 는 객체({{이름: 값}})여야 합니다.")
        on_error = str(s.get("on_error") or "stop")
        if on_error not in ("stop", "continue"):
            raise WorkflowError(f"steps[{i}].on_error 는 stop 또는 continue 입니다.")
        fields = s.get("fields")
        if fields is not None and not (isinstance(fields, list) and all(isinstance(f, str) for f in fields)):
            raise WorkflowError(f"steps[{i}].fields 는 키 이름 목록이어야 합니다. 예: [id, title]")
        where = s.get("where")
        if where is not None and not isinstance(where, dict):
            raise WorkflowError(f"steps[{i}].where 는 {{키: 포함할 글자}} 형태입니다. 예: {{dept: 영업}}")
        out.append({
            "id": sid, "tool": str(s["tool"]).strip(), "args": args, "on_error": on_error,
            "when": str(s["when"]) if s.get("when") is not None else None,
            "for_each": s.get("for_each"), "select": str(s["select"]) if s.get("select") else None,
            "where": where, "fields": fields, "limit": _int(s.get("limit")), "hide": bool(s.get("hide")),
            "max_chars": _int(s.get("max_chars")) or STEP_CHARS,
        })
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def shape(data: Any, st: dict[str, Any]) -> tuple[Any, list[str]]:
    """select → where → fields → limit 순서로 결과를 줄임. (줄인 값, 안내문)"""
    notes: list[str] = []
    if st["select"]:
        picked = walk(data, st["select"].split("."))
        if picked is None:
            keys = list(data)[:15] if isinstance(data, dict) else None
            notes.append(f"select '{st['select']}' 경로가 결과에 없음" + (f" (최상위 키: {', '.join(map(str, keys))})" if keys else ""))
        else:
            data = picked
    if st["where"] and isinstance(data, list):
        cond = {str(k): str(v).lower() for k, v in st["where"].items()}
        before = len(data)
        data = [x for x in data if isinstance(x, dict) and all(v in str(walk(x, k.split(".")) or "").lower() for k, v in cond.items())]
        notes.append(f"where 로 {before}개 중 {len(data)}개")
    if st["fields"]:
        def keep(x: Any) -> Any:
            return {f: walk(x, f.split(".")) for f in st["fields"]} if isinstance(x, dict) else x

        data = [keep(x) for x in data] if isinstance(data, list) else keep(data)
    if st["limit"] and isinstance(data, list) and len(data) > st["limit"]:
        notes.append(f"전체 {len(data)}개 중 앞 {st['limit']}개만")
        data = data[: st["limit"]]
    return data, notes


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.85)
    return f"{text[:head]}\n…(중략: 전체 {len(text):,}자)…\n{text[-(limit - head):]}"


def _as_data(out: ToolOutcome) -> Any:
    if out.data is not None:
        return out.data
    try:
        return json.loads(out.text)
    except (ValueError, TypeError):
        return None


def _missing_hint(paths: list[str], ctx: dict[str, Any]) -> str:
    hints = []
    for p in dict.fromkeys(paths):
        parts = p.split(".")
        extra = ""
        if parts[0] == "steps" and len(parts) > 1:
            st = ctx["steps"].get(parts[1])
            if st is None:
                extra = " (그 단계가 아직 실행되지 않았거나 없음)"
            else:
                d = st.get(parts[2] if len(parts) > 2 and parts[2] == "raw" else "data")
                if isinstance(d, dict):
                    extra = f" (data 최상위 키: {', '.join(map(str, list(d)[:15]))})"
                elif isinstance(d, list):
                    keys = list(dict.fromkeys(k for x in d[:20] if isinstance(x, dict) for k in x))[:15]
                    extra = f" (data 는 {len(d)}개 목록" + (f", 항목 키: {', '.join(map(str, keys))}" if keys else "") + ")"
                elif d is None:
                    extra = " (그 단계 결과가 JSON 이 아니라 text 만 있음)"
        hints.append("{" + p + "}" + extra)
    return "⚠️ 값을 찾지 못한 변수: " + ", ".join(hints)


def make_batch_tool(rt: "AgentRuntime") -> BuiltinTool:
    tz = rt.cfg.agent.timezone

    async def run_tool_batch(args: dict[str, Any]) -> ToolOutcome:
        if _IN_BATCH.get():
            return ToolOutcome("오류: run_tool_batch 안에서 run_tool_batch 를 다시 부를 수 없습니다.", True)
        try:
            steps = validate(args.get("steps"))
        except WorkflowError as e:
            return ToolOutcome(f"오류: {e}", True)
        hub = rt.hub
        run = current_run.get()
        emit = run.meta.get("emit") if run is not None else None
        bid = "b" + uuid.uuid4().hex[:6]
        ctx: dict[str, Any] = {"inputs": {}, "steps": {}, "prev": "", "timezone": tz}
        sem = asyncio.Semaphore(CONCURRENCY)
        lines: list[str] = []
        files: dict[str, dict[str, Any]] = {}
        shown: dict[str, Any] = {}
        ok_steps = 0
        failed: str | None = None
        calls = 0
        t_all = time.monotonic()

        async def call(tool: str, targs: Any, cid: str) -> ToolOutcome:
            nonlocal calls
            from .agent import ToolEvent, _ev_dict, _truncate, view_of

            calls += 1
            route = hub.route_of(tool) or ("?", tool)
            if not isinstance(targs, dict):
                return ToolOutcome(f"오류: args 를 채운 결과가 객체가 아닙니다: {str(targs)[:200]}", True)
            ev = ToolEvent(tool, route[0], route[1], targs)
            if emit is not None:
                await emit("tool_start", _ev_dict(ev, cid))
            t0 = time.monotonic()
            async with sem:
                out = await hub.call(tool, targs)
            ev.elapsed = time.monotonic() - t0
            ev.result, ev.is_error, ev.files, ev.view = _truncate(out.text, rt.cfg.agent.max_tool_result_chars), out.is_error, out.files, view_of(out.data)
            if emit is not None:
                await emit("tool_end", _ev_dict(ev, cid))
            for f in out.files:
                files[f.get("path", "")] = f
            return out

        token = _IN_BATCH.set(True)
        try:
            for i, st in enumerate(steps, 1):
                head = f"### {i}. [{st['id']}] {st['tool']}"
                ctx["missing"] = []
                if not check_when(st["when"], ctx):
                    lines.append(f"{head} - 건너뜀 (when 조건: {st['when']})")
                    continue
                tool = st["tool"]
                if tool == "run_tool_batch" or hub.route_of(tool) is None:
                    near = difflib.get_close_matches(tool, [n for n in hub._routes if n != "run_tool_batch"], n=3, cutoff=0.5)
                    msg = ("묶음 안에서는 run_tool_batch 를 쓸 수 없습니다" if tool == "run_tool_batch"
                           else f"알 수 없는 도구 '{tool}'" + (f" - 혹시: {', '.join(near)}" if near else " (도구 목록의 이름을 그대로 쓰세요)"))
                    res: dict[str, Any] = {"text": f"오류: {msg}", "files": [], "data": None, "error": msg}
                    ctx["steps"][st["id"]] = res
                    lines.append(f"{head} ❌\n오류: {msg}")
                    if st["on_error"] == "stop":
                        failed = st["id"]
                        break
                    continue
                t0 = time.monotonic()
                notes: list[str] = []
                if st["for_each"] is not None:
                    items = render(st["for_each"], ctx)
                    if isinstance(items, str):
                        try:
                            items = json.loads(items)
                        except ValueError:
                            items = [x.strip() for x in items.splitlines() if x.strip()] if "\n" in items else items
                    if not isinstance(items, list):
                        kind = type(items).__name__ if items not in ("", None) else "빈 값"
                        hint = f" (최상위 키: {', '.join(map(str, list(items)[:15]))})" if isinstance(items, dict) else ""
                        msg = f"for_each 값이 목록이 아닙니다: {kind}{hint}. 목록을 가리키는 경로를 쓰세요 (예: {{steps.<id>.data.items}})"
                        if ctx["missing"]:
                            msg += "\n" + _missing_hint(ctx["missing"], ctx)
                        ctx["steps"][st["id"]] = {"text": f"오류: {msg}", "files": [], "data": None, "error": msg}
                        lines.append(f"{head} ❌\n오류: {msg}")
                        if st["on_error"] == "stop":
                            failed = st["id"]
                            break
                        continue
                    if len(items) > MAX_ITEMS:
                        notes.append(f"항목 {len(items)}개 중 앞 {MAX_ITEMS}개만 실행 (나머지는 다음 묶음으로)")
                        items = items[:MAX_ITEMS]

                    async def one(n: int, item: Any) -> tuple[Any, ToolOutcome]:
                        lctx = {**ctx, "locals": {"item": item, "index": n}, "missing": ctx["missing"]}
                        return item, await call(tool, render(st["args"], lctx), f"{bid}-{i}-{n}")

                    pairs = await asyncio.gather(*(one(n, it) for n, it in enumerate(items)))
                    errs = [(it, o) for it, o in pairs if o.is_error]
                    data = [{"error": o.text[:300]} if o.is_error else (_as_data(o) if _as_data(o) is not None else o.text) for _, o in pairs]
                    text = "\n\n".join(f"[{n}] {o.text}" for n, (_, o) in enumerate(pairs))
                    step_files = [f["path"] for _, o in pairs for f in o.files if f.get("path")]
                    is_err = bool(pairs) and len(errs) == len(pairs)
                    if errs:
                        notes.append(f"{len(pairs)}개 중 {len(errs)}개 실패")
                    label = f"{head} × {len(pairs)}개 항목"
                else:
                    out = await call(tool, render(st["args"], ctx), f"{bid}-{i}")
                    data, text, is_err = _as_data(out), out.text, out.is_error
                    step_files = [f["path"] for f in out.files if f.get("path")]
                    label = head
                # select/where/fields/limit 로 줄인 결과가 data (다음 단계도 이 값을 받음), 원본은 raw
                view = data
                if data is not None and not is_err:
                    view, more = shape(data, st)
                    notes += more
                elif not is_err and (st["select"] or st["fields"] or st["where"]):
                    notes.append("결과가 JSON 이 아니어서 select/where/fields 를 적용하지 못함 (텍스트 그대로)")
                missing_now = list(ctx["missing"])  # 이 단계 args·for_each 에서 못 찾은 변수
                ctx["steps"][st["id"]] = {"text": text, "files": step_files, "data": view, "raw": data, "error": text if is_err else ""}
                ctx["prev"] = text
                if missing_now:
                    notes.append(_missing_hint(missing_now, ctx))
                elapsed = time.monotonic() - t0
                if is_err:
                    body = _clip(text, st["max_chars"])
                else:
                    ok_steps += 1
                    if view is not None:
                        shown[st["id"]] = view
                        body = _clip(json.dumps(view, ensure_ascii=False, default=str), st["max_chars"])
                    else:
                        body = _clip(text, st["max_chars"])
                    if st["hide"]:
                        body = f"(숨김: 다음 단계 입력으로만 사용 · 결과 {len(text):,}자)"
                lines.append(f"{label} {'❌' if is_err else '✓'} {elapsed:.1f}초"
                             + (f" · 파일 {len(step_files)}개: {', '.join(step_files[:5])}" if step_files else "")
                             + "".join(f"\n- {n}" for n in notes) + f"\n{body}")
                if is_err and st["on_error"] == "stop":
                    failed = st["id"]
                    break
        finally:
            _IN_BATCH.reset(token)
        done = len([s for s in steps if s["id"] in ctx["steps"]])
        top = (f"[도구 묶음 실행] 단계 {done}/{len(steps)} · 도구 호출 {calls}회 · {time.monotonic() - t_all:.1f}초"
               + (f" · ❌ '{failed}' 단계 실패로 멈춤 (on_error: continue 면 이어서 실행)" if failed else ""))
        # 실패로 멈췄으면 오류 (화면에 '완료' 로 보이지 않게, 모델도 고쳐서 다시 부르게). on_error: continue 로 건너뛴 실패는 성공으로
        return ToolOutcome(top + "\n\n" + "\n\n".join(lines), failed is not None or ok_steps == 0, list(files.values()), {"steps": shown})

    return BuiltinTool(
        "run_tool_batch",
        "여러 도구 호출을 한 번에 실행합니다 (LLM 왕복을 줄여 반복 한도·비용 절약). "
        "앞 단계 결과를 다음 단계 인자로 넘길 때({steps.<id>.data.<키>}), 목록의 항목마다 같은 도구를 부를 때(for_each + {item.<키>}), "
        "큰 결과에서 필요한 부분만 볼 때(select/where/fields/limit, 중간 결과는 hide) 쓰세요. "
        "처음 쓰는 도구는 먼저 한 번 직접 불러 결과 구조(키 이름)를 확인한 뒤 묶으세요. 서로 관련 없는 호출 몇 개는 그냥 한 응답에서 동시에 부르면 됩니다. "
        "select/where/fields/limit 로 줄인 결과가 그 단계의 data 이고 다음 단계도 이 값을 받습니다(원본은 raw). "
        "변수: {steps.<id>.text|file|files|error}, {steps.<id>.data.items.0.id}, {steps.<id>.raw...}, {item}, {item.<키>}, {index}, {today} {yesterday} {last_month} 등 날짜.",
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": f"순서대로 실행할 단계 (최대 {MAX_STEPS}개)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "뒤 단계에서 {steps.<id>...} 로 쓸 이름 (생략 시 s1, s2…)"},
                            "tool": {"type": "string", "description": "도구 이름 (도구 목록의 이름 그대로)"},
                            "args": {"type": "object", "description": "도구 인자. 값에 {변수} 사용 가능 (값 전체가 변수 하나면 원래 타입 그대로)"},
                            "for_each": {"type": "string", "description": f"목록을 가리키는 변수. 항목마다 실행(동시 {CONCURRENCY}개, 최대 {MAX_ITEMS}개), args 에서 {{item}} {{index}}"},
                            "when": {"type": "string", "description": "조건이 맞을 때만 실행. 예: \"{steps.list.data.total} != 0\""},
                            "select": {"type": "string", "description": "결과에서 볼 경로. 예: items 또는 data.rows"},
                            "where": {"type": "object", "description": "목록 항목 중 {키: 포함할 글자} 가 모두 맞는 것만"},
                            "fields": {"type": "array", "items": {"type": "string"}, "description": "목록 항목에서 남길 키"},
                            "limit": {"type": "integer", "description": "목록 앞에서 몇 개만 볼지"},
                            "hide": {"type": "boolean", "description": "true 면 결과를 보여주지 않음 (다음 단계 입력으로만)"},
                            "on_error": {"type": "string", "enum": ["stop", "continue"], "description": "실패하면 멈춤(기본) / 다음 단계 계속"},
                            "max_chars": {"type": "integer", "description": f"이 단계 결과를 보여줄 최대 글자 (기본 {STEP_CHARS})"},
                        },
                        "required": ["tool"],
                    },
                },
            },
            "required": ["steps"],
        },
        run_tool_batch,
    )
