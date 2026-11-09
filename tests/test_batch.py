"""도구 묶음 실행 (run_tool_batch): 실제 샘플 MCP 서버로 연쇄 호출·항목마다 실행·결과 줄이기·오류 안내·에이전트 왕복 수."""

from __future__ import annotations

import json
from pathlib import Path

from test_agent import ScriptedLLM, write_config

from inhouse_agent.batch import shape, validate
from inhouse_agent.config import load_config
from inhouse_agent.interaction import RunContext, current_run
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime


async def test_chain_search_then_detail_for_each(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        rows = (await rt.hub.call("demo__list_notices", {"date_from": "2026-11-02", "date_to": "2026-11-04"})).data
        out = await rt.hub.call("run_tool_batch", {"steps": [
            {"id": "list", "tool": "demo__list_notices", "args": {"date_from": "2026-11-02", "date_to": "2026-11-04"}, "hide": True},
            {"id": "detail", "tool": "demo__get_notice", "for_each": "{steps.list.data}", "args": {"notice_id": "{item.id}"},
             "fields": ["id", "body"]},
        ]})
    assert not out.is_error, out.text
    assert f"도구 호출 {1 + len(rows)}회" in out.text and f"× {len(rows)}개 항목" in out.text
    assert "(숨김: 다음 단계 입력으로만 사용" in out.text and rows[0]["title"] not in out.text  # 목록은 숨김
    shown = out.data["steps"]["detail"]
    assert [d["id"] for d in shown] == [r["id"] for r in rows] and all("공지 본문" in d["body"] for d in shown)


async def test_shaping_files_and_dates(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        out = await rt.hub.call("run_tool_batch", {"steps": [
            {"id": "n", "tool": "demo__list_notices", "args": {"date_from": "2026-11-01", "date_to": "2026-11-10"},
             "where": {"dept": "인사"}, "fields": ["id", "dept"], "limit": 2},
            {"id": "xl", "tool": "demo__download_sales_excel", "for_each": ["2026-09", "2026-10"], "args": {"month": "{item}", "wait": True}},
            {"id": "t", "tool": "get_current_time", "args": {}, "when": "{steps.xl.error} == "},
        ]})
    assert not out.is_error, out.text
    n = out.data["steps"]["n"]
    assert len(n) <= 2 and all(set(x) == {"id", "dept"} and "인사" in x["dept"] for x in n) and n
    assert "where 로" in out.text
    names = sorted(Path(f["path"]).name for f in out.files)
    assert any("2026-09" in x for x in names) and any("2026-10" in x for x in names)  # 다운로드 파일이 상위 실행으로 전달됨
    assert "### 3. [t] get_current_time ✓" in out.text  # when 조건 (앞 단계 오류 없음)


async def test_errors_are_explained(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        call = rt.hub.call
        bad = await call("run_tool_batch", {"steps": [{"tool": "demo__get_notise", "args": {}}]})
        assert bad.is_error and "혹시: demo__get_notice" in bad.text
        # for_each 가 목록이 아님 + 변수 경로가 틀림 → 실제 키를 알려줌
        r = await call("run_tool_batch", {"steps": [
            {"id": "d", "tool": "demo__get_notice", "args": {"notice_id": "N1"}},
            {"id": "e", "tool": "demo__get_notice", "for_each": "{steps.d.data}", "args": {"notice_id": "{item}"}},
        ]})
        assert "for_each 값이 목록이 아닙니다: dict (최상위 키: id, body)" in r.text and "'e' 단계 실패로 멈춤" in r.text
        assert r.is_error and "### 1. [d] demo__get_notice ✓" in r.text  # 앞 단계 결과는 남기고, 멈췄으니 오류
        r = await call("run_tool_batch", {"steps": [
            {"id": "d", "tool": "demo__get_notice", "args": {"notice_id": "N1"}},
            {"id": "x", "tool": "demo__get_notice", "args": {"notice_id": "{steps.d.data.noticeId}"}, "on_error": "continue"},
            {"id": "y", "tool": "get_current_time", "args": {}},
        ]})
        assert "⚠️ 값을 찾지 못한 변수: {steps.d.data.noticeId} (data 최상위 키: id, body)" in r.text and "[y] get_current_time ✓" in r.text
        nested = await call("run_tool_batch", {"steps": [{"tool": "run_tool_batch", "args": {"steps": []}}]})
        assert nested.is_error and "run_tool_batch 를 쓸 수 없습니다" in nested.text
        assert (await call("run_tool_batch", {"steps": []})).is_error
        ok = await call("run_tool_batch", {"steps": json.dumps([{"tool": "get_current_time"}])})  # JSON 문자열로 와도 처리
        assert not ok.is_error


def test_validate_and_shape() -> None:
    st = validate([{"tool": "a", "select": "items", "where": {"dept": "영업"}, "fields": ["id", "meta.owner"], "limit": 1}])[0]
    data = {"items": [{"id": 1, "dept": "영업1팀", "meta": {"owner": "kim"}}, {"id": 2, "dept": "인사"}, {"id": 3, "dept": "영업2팀"}]}
    view, notes = shape(data, st)
    assert view == [{"id": 1, "meta.owner": "kim"}] and "전체 2개 중 앞 1개만" in notes and "where 로 3개 중 2개" in notes
    view, notes = shape({"rows": []}, {**st, "where": None, "fields": None, "limit": None})
    assert view == {"rows": []} and "select 'items' 경로가 결과에 없음 (최상위 키: rows)" in notes
    for raw, msg in ([{"args": {}}], "tool"), ([{"tool": "a", "on_error": "x"}], "on_error"), ([{"tool": "a"}] * 16, "최대 15"):
        try:
            validate(raw)
        except ValueError as e:
            assert msg in str(e)
        else:  # pragma: no cover
            raise AssertionError(raw)


async def test_agent_uses_batch_in_one_round_trip(tmp_path: Path) -> None:
    """검색 → 상세 3건을 원래는 LLM 왕복 3번 이상 → 묶음 1번 + 답 1번."""
    cfg = load_config(write_config(tmp_path))
    steps = [{"id": "list", "tool": "demo__list_notices", "args": {"date_from": "{today}"}, "fields": ["id"], "hide": True},
             {"id": "detail", "tool": "demo__get_notice", "for_each": "{steps.list.data}", "args": {"notice_id": "{item.id}"}}]
    llm = ScriptedLLM([LLMResponse(None, [ToolCall("b1", "run_tool_batch", json.dumps({"steps": steps}))]), LLMResponse("오늘 공지 정리")])
    events: list[tuple[str, dict]] = []
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("오늘 공지 본문까지 정리해줘", on_event=lambda k, d: events.append((k, d)))
    assert r.success and r.iterations == 2 and len(llm.calls) == 2
    tool_msg = next(m for m in llm.calls[1] if m["role"] == "tool")
    assert "[도구 묶음 실행]" in tool_msg["content"] and "공지 본문" in tool_msg["content"]
    inner = [d for k, d in events if k == "tool_end" and "-" in str(d.get("id", ""))]  # 화면에 안쪽 호출도 보임
    assert {d["tool"] for d in inner} == {"list_notices", "get_notice"}
    assert any(d["function"] == "run_tool_batch" for k, d in events if k == "tool_end")


async def test_batch_without_run_context(tmp_path: Path) -> None:
    """스케줄러·직접 실행처럼 화면 이벤트가 없는 곳에서도 동작."""
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        tok = current_run.set(RunContext(ask=None, interactive=False))
        try:
            out = await rt.hub.call("run_tool_batch", {"steps": [{"tool": "get_current_time"}]})
        finally:
            current_run.reset(tok)
    assert not out.is_error and "도구 호출 1회" in out.text


async def test_filtered_result_flows_to_next_step_and_raw_keeps_original(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        rows = (await rt.hub.call("demo__list_notices", {"date_from": "2026-11-01", "date_to": "2026-11-10"})).data
        want = [r for r in rows if "인사" in r["dept"]]
        out = await rt.hub.call("run_tool_batch", {"steps": [
            {"id": "n", "tool": "demo__list_notices", "args": {"date_from": "2026-11-01", "date_to": "2026-11-10"},
             "where": {"dept": "인사"}, "fields": ["id"], "hide": True},
            {"id": "d", "tool": "demo__get_notice", "for_each": "{steps.n.data}", "args": {"notice_id": "{item.id}"}},
            {"id": "all", "tool": "demo__get_notice", "args": {"notice_id": "{steps.n.raw.0.id}"}},  # 원본 첫 항목
        ]})
    assert f"× {len(want)}개 항목" in out.text  # 거른 항목만 상세 조회
    assert [d["id"] for d in out.data["steps"]["d"]] == [r["id"] for r in want]
    assert out.data["steps"]["all"]["id"] == rows[0]["id"]


async def test_called_from_skill_steps_keeps_inner_templates(tmp_path: Path) -> None:
    """스킬 steps·채팅창 직접 실행(워크플로)에서 부를 때: 바깥은 입력값·날짜만 채우고 {steps..}{item} 은 묶음이 채움."""
    from inhouse_agent.workflow import run_workflow, validate_steps

    cfg = load_config(write_config(tmp_path))
    inner = [{"id": "list", "tool": "demo__list_notices", "args": {"date_from": "{d}"}, "fields": ["id"], "hide": True},
             {"id": "detail", "tool": "demo__get_notice", "for_each": "{steps.list.data}", "args": {"notice_id": "{item.id}"}}]
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        want = (await rt.hub.call("demo__list_notices", {"date_from": "2026-11-02"})).data
        for steps_arg in (inner, json.dumps(inner, ensure_ascii=False)):  # 객체 / 채팅창에서 온 JSON 문자열
            r = await run_workflow(rt.hub, validate_steps([{"tool": "run_tool_batch", "args": {"steps": steps_arg}}]),
                                   {"d": "2026-11-02"}, timezone="Asia/Seoul")
            assert r.success, r.output
            assert f"× {len(want)}개 항목" in r.output and all(x["id"] in r.output for x in want)
