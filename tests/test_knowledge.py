"""업무 지식 그래프(온톨로지): 실행 기록 → 업무 유형·검증된 도구 순서·DFG·자주 난 오류 → 검색·자동 참고·내보내기."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.knowledge import KnowledgeGraph, concepts, to_graphml, to_turtle
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime

DAY = 86400


def session(path: Path, sid: str, turns: list[dict[str, Any]]) -> None:
    """웹 서버가 남기는 것과 같은 형식의 대화 기록 (turns + messages)."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    out_turns = []
    for i, t in enumerate(turns):
        messages.append({"role": "user", "content": t["prompt"]})
        tools = []
        for j, (srv, tool, args, err) in enumerate(t["calls"]):
            cid = f"{sid}_{i}_{j}"
            fn = tool if srv == "builtin" else f"{srv}__{tool}"
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": fn, "arguments": json.dumps(args, ensure_ascii=False)}}]})
            messages.append({"role": "tool", "tool_call_id": cid, "content": f"오류: {err}" if err else "ok"})
            tools.append(f"{srv}.{tool}" + ("(오류)" if err else ""))
        messages.append({"role": "assistant", "content": "끝"})
        out_turns.append({"time": t.get("time", time.time()), "prompt": t["prompt"], "kind": t.get("kind", "chat"), "skill": t.get("skill"),
                          "status": t.get("status", "completed"), "tools": tools, "files": t.get("files", [])})
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{sid}.json").write_text(json.dumps({"id": sid, "turns": out_turns, "messages": messages, "skill": None}, ensure_ascii=False),
                                      encoding="utf-8")


SALES = [("demo", "download_sales_excel", {"month": "2026-03", "wait": True}, None),
         ("builtin", "get_current_time", {}, None),
         ("builtin", "query_table", {"path": "downloads/매출현황_2026-03.xlsx", "group_by": ["부서"]}, None),
         ("builtin", "write_excel", {"path": "outputs/부서별.xlsx", "rows": [[1]]}, None)]


def corpus(root: Path) -> list[tuple[str, Path]]:
    now = time.time()
    kim, lee = root / "kim" / "sessions", root / "lee" / "sessions"
    session(kim, "a", [
        {"prompt": "3월 매출 엑셀을 받아서 부서별로 정리해줘", "calls": SALES, "files": ["/w/outputs/부서별.xlsx"], "time": now - 30 * DAY},
        {"prompt": "4월 매출 엑셀 부서별 정리", "calls": SALES, "files": ["/w/outputs/부서별_4.xlsx"], "time": now - 20 * DAY},
    ])
    session(kim, "b", [
        {"prompt": "5월 매출 엑셀 부서별로 집계해줘", "status": "tool_errors", "time": now - 10 * DAY,
         "calls": [SALES[0], ("builtin", "query_table", {"path": "downloads/x.xlsx", "group_by": ["부서명"]}, "열 '부서명' 을 찾을 수 없습니다 (있는 열: 부서, 금액)"),
                   ("builtin", "query_table", {"path": "downloads/y.xlsx", "group_by": ["부서명"]}, "열 '부서명' 을 찾을 수 없습니다 (있는 열: 부서, 금액, 월)")]},
    ])
    session(lee, "c", [
        {"prompt": "4월 매출 엑셀 받아서 부서별 합계 정리해줘", "calls": SALES, "files": ["/w/outputs/합계.xlsx"], "time": now - 2 * DAY},
        {"prompt": "어제 공지 목록 알려줘", "calls": [("demo", "list_notices", {"date_from": "2026-11-08"}, None),
                                                ("demo", "get_notice", {"notice_id": "N1"}, None)], "time": now - 3 * DAY},
        {"prompt": "어제 공지사항 목록 정리", "calls": [("demo", "list_notices", {"date_from": "2026-11-07"}, None),
                                                 ("demo", "get_notice", {"notice_id": "N2"}, None)], "time": now - 1 * DAY},
        {"prompt": "/demo__list_notices {\"date_from\": \"2026-11-01\"}", "kind": "tool",  # 도구 직접 실행: 유형 묶기에서 빠짐
         "calls": [("demo", "list_notices", {"date_from": "2026-11-01"}, None), ("builtin", "run_tool_batch", {"steps": []}, None)]},
        {"prompt": "/weekly_report {}", "kind": "skill", "skill": "weekly_report", "calls": [("builtin", "read_file_text", {"path": "a.md"}, None)],
         "files": ["/w/outputs/주간.pptx"], "time": now - DAY},
    ])
    return [("kim", kim), ("lee", lee)]


def test_concepts_korean() -> None:
    c = concepts("3월 매출 엑셀을 받아서 부서별로 정리해줘")
    assert {"기간:월", "매출", "xlsx", "부서", "정리"} <= set(c), c
    assert "해줘" not in c and "을" not in "".join(c)
    assert "기간:일" in concepts("어제 공지 목록 알려줘") and "공지" in concepts("어제 공지 목록 알려줘")


def test_build_task_types_workflows_dfg_pitfalls(tmp_path: Path) -> None:
    kg = KnowledgeGraph(tmp_path / "k" / "graph.json")
    d = kg.build(corpus(tmp_path / "users"))
    nodes = d["nodes"]
    tasks = {n["label"]: n for n in nodes.values() if n["type"] == "TaskType"}
    sales = next(n for label, n in tasks.items() if "매출" in label)
    assert sales["count"] == 4 and sales["success"] == 3 and sales["users"] == 2  # 두 사람의 같은 업무가 하나의 유형으로
    assert any("공지" in label for label in tasks) and "weekly_report" in tasks  # 스킬 실행은 스킬별 유형
    wf = next(n for n in nodes.values() if n["type"] == "Workflow" and "download_sales_excel" in n["label"])
    assert wf["label"] == "download_sales_excel → query_table → write_excel" and wf["count"] == 3  # 보조 도구(get_current_time) 뺌
    assert wf["steps"][0] == "demo__download_sales_excel(month=<월>, wait=true)"  # 인자 형태 (값이 아니라 종류)
    assert wf["steps"][1].startswith("query_table(path=<파일>")
    dfg = {(e["s"], e["o"]): e["count"] for e in d["edges"] if e["p"] == "followedBy"}
    assert dfg[("tool:demo__download_sales_excel", "tool:query_table")] == 4
    pit = [n for n in nodes.values() if n["type"] == "Pitfall"]
    assert pit and pit[0]["tool"] == "query_table" and pit[0]["count"] == 2 and "'…'" in pit[0]["label"]  # 값이 달라도 같은 오류로 묶음
    assert any(e["p"] == "produces" and e["o"] == "artifact:xlsx" for e in d["edges"])
    assert any(e["p"] == "partOf" and e["o"] == "site:demo" for e in d["edges"])
    assert d["source"] == {"runs": 8, "success": 7, "users": 2, "task_types": 3}  # 직접 실행은 유형을 만들지 않음
    assert not any(e["o"] == "tool:run_tool_batch" or e["s"] == "tool:run_tool_batch" for e in d["edges"])  # 묶음 상자는 흐름에서 뺌


def test_search_hint_and_recency(tmp_path: Path) -> None:
    kg = KnowledgeGraph(tmp_path / "graph.json")
    kg.sources = lambda: corpus(tmp_path / "users")
    res = kg.search("6월 매출 엑셀 부서별로 정리해줘")
    assert res and "매출" in res[0]["task_type"] and res[0]["success"] == 3
    assert res[0]["pitfalls"][0]["tool"] == "query_table" and res[0]["artifacts"] == {"xlsx": 3}
    assert len(res) == 1  # '정리' 하나로만 걸리는 공지 업무는 약한 결과라 빠짐
    hint = kg.hint("6월 매출 엑셀 부서별로 정리해줘")
    assert hint.startswith("[참고: 지식 그래프") and "검증된 순서: demo__download_sales_excel(month=<월>" in hint and "주의: query_table" in hint
    assert kg.hint("오늘 날씨 어때") == ""  # 모르는 업무엔 붙이지 않음
    label = kg.search("공지 목록")[0]["task_type"]
    assert "공지" in label and "공지사항" not in label and "기간:" not in label  # 이름: 기간 빼고 겹치는 말은 짧은 쪽
    assert "업무 유형" in kg.overview() and "download_sales_excel → query_table" in kg.overview()


def test_exports_open_in_rdflib_and_networkx(tmp_path: Path) -> None:
    kg = KnowledgeGraph(tmp_path / "graph.json")
    d = kg.build(corpus(tmp_path / "users"))
    rdflib = pytest.importorskip("rdflib")
    g = rdflib.Graph().parse(data=to_turtle(d), format="turtle")
    iak = rdflib.Namespace("urn:inhouse-agent:knowledge#")
    owl = rdflib.Namespace("http://www.w3.org/2002/07/owl#")
    assert (iak.TaskType, rdflib.RDF.type, owl.Class) in g and (iak.solvedBy, rdflib.RDFS.domain, iak.TaskType) in g
    tasks = list(g.subjects(rdflib.RDF.type, iak.TaskType))
    assert len(tasks) == 3 and any("매출" in str(g.value(t, rdflib.RDFS.label)) for t in tasks)
    nx = pytest.importorskip("networkx")
    p = kg.export("graphml", tmp_path)
    G = nx.read_graphml(p)
    assert G.number_of_nodes() == len(d["nodes"]) and G.number_of_edges() == len(d["edges"])
    assert G.edges["tool:demo__download_sales_excel", "tool:query_table"]["weight"] == 4
    assert to_graphml(d).startswith("<?xml")


async def test_agent_gets_hint_from_people_history(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM([LLMResponse("정리했습니다")])
    events: list[tuple[str, dict]] = []
    async with AgentRuntime(cfg, llm=llm) as rt:
        corpus(rt.users.root)  # 사용자 kim, lee 의 실제 대화 기록 자리
        r = await rt.agent.run("6월 매출 엑셀 부서별로 정리해줘", on_event=lambda k, d: events.append((k, d)))
        sent_user = llm.calls[0][-1]["content"]
        assert r.success and "[참고: 지식 그래프" in sent_user and "검증된 순서" in sent_user
        assert any(k == "knowledge" for k, _ in events)
        assert rt.knowledge.path.is_file()  # 처음 찾을 때 자동으로 만듦
        out = await rt.hub.call("knowledge_search", {"query": "공지 목록"})
        assert "공지" in out.text and "list_notices" in out.text
        exp = await rt.hub.call("knowledge_export", {"format": "ttl"})
        assert not exp.is_error and exp.files[0]["path"].endswith("knowledge.ttl")
        llm.responses.append(LLMResponse("ok"))
        await rt.agent.run("오늘 날씨 어때")  # 모르는 업무엔 붙이지 않음
        assert "[참고: 지식 그래프" not in llm.calls[-1][-1]["content"]


def test_built_from_real_web_runs(tmp_path: Path) -> None:
    """웹 서버로 실제 실행 → 저장된 기록에서 지식을 만들고 찾음 (기록 형식이 맞는지)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(write_config(tmp_path))
    script = []
    for day in ("2026-11-05", "2026-11-06", "2026-11-07"):
        script += [LLMResponse(None, [ToolCall("c" + day, "demo__list_notices", json.dumps({"date_from": day}))]), LLMResponse("공지 정리")]
    llm = ScriptedLLM(script)
    app = create_app(cfg, runtime=AgentRuntime(cfg, llm=llm), enable_scheduler=False)
    H = {"X-Agent-User": "park"}
    with TestClient(app) as c:
        for day in ("5일", "6일", "7일"):
            run = c.post("/api/runs", json={"prompt": f"11월 {day} 공지 목록 정리해줘", "new_session": True}, headers=H).json()
            with c.stream("GET", f"/api/runs/{run['id']}/events", headers=H) as s:
                "".join(s.iter_text())
        built = c.post("/api/knowledge/build", headers=H).json()
        assert built["source"]["runs"] == 3 and built["source"]["task_types"] == 1
        found = c.get("/api/knowledge/search", params={"q": "공지 목록 정리"}, headers=H).json()
        assert found["results"][0]["workflows"][0]["steps"] == ["demo__list_notices(date_from=<날짜>)"]
        ov = c.get("/api/knowledge", headers=H).json()
        assert "실행 3건" in ov["text"] and "| 공지 · 목록 · 정리 | 3 | 3 | 1 |" in ov["text"]
        assert "kg-search" in c.get("/").text


async def test_suggests_runnable_skill_draft_from_proven_order(tmp_path: Path) -> None:
    """같은 순서로 3번 성공한 업무 → SKILL.md 초안 → 실제 샘플 MCP 서버로 그대로 실행."""
    from inhouse_agent.skills import parse_skill
    from inhouse_agent.workflow import run_workflow, validate_steps

    agg = [{"column": "금액", "func": "sum"}]
    now = time.time()
    turns = [{"prompt": f"{m}월 매출 엑셀 받아서 부서별 금액 합계 내줘", "time": now - (10 - m) * DAY,
              "calls": [("demo", "download_sales_excel", {"month": f"2026-0{m}", "wait": True}, None),
                        ("builtin", "query_table", {"path": f"downloads/매출현황_2026-0{m}.xlsx", "group_by": ["부서"], "aggregates": agg}, None),
                        ("builtin", "write_excel", {"path": f"reports/부서별_{m}.xlsx", "sheets": [{"name": "요약", "columns": ["부서"], "rows": [[m]]}]}, None)]}
             for m in (6, 7, 8)]
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        session(rt.users.root / "kim" / "sessions", "s", turns)
        rt.knowledge.build()
        out = await rt.hub.call("knowledge_suggest_skills", {})
        assert not out.is_error and "초안을 만들었습니다" in out.text
        draft = out.data["drafts"][0]
        text = Path(draft["path"]).read_text(encoding="utf-8")
        skill = parse_skill(text, Path(draft["path"]).parent)
        steps = validate_steps(skill.steps)
        assert [s["tool"] for s in steps] == ["demo__download_sales_excel", "query_table", "write_excel"]
        assert steps[0]["args"] == {"month": "{month}", "wait": True}
        assert steps[1]["args"] == {"path": "{steps.s1.file}", "group_by": ["부서"], "aggregates": agg}  # 매번 같은 값은 그대로
        assert steps[2]["args"]["path"] == "{path}"  # 저장할 파일 이름은 입력값 (앞 단계 파일이 아님)
        names = {i.name: i for i in skill.inputs}
        assert names["month"].default == "{last_month}" and names["path"].default.startswith("reports/auto_")
        assert "## 절차" in skill.body and "2단계 path: 앞 단계 파일을 쓰도록 했습니다" in skill.body  # 확인할 점을 초안에 남김
        assert draft["name"].isascii() and draft["name"].startswith("auto_download_sales_excel_write_excel_")
        # 초안 그대로 실행 (sheets 는 매번 달라 입력값 → 실행할 때 줌)
        inputs = skill.resolve_inputs({"month": "2026-10", "sheets": json.dumps([{"name": "요약", "columns": ["부서"], "rows": [["영업"]]}])},
                                      rt.cfg.agent.timezone)
        r1 = await run_workflow(rt.hub, steps[:2], inputs, timezone=rt.cfg.agent.timezone)
        assert r1.success, r1.output
        assert "부서" in r1.tool_events[1].result and r1.tool_events[0].files  # 1단계에서 받은 엑셀을 2단계가 집계
        # 이미 같은 이름의 스킬이 있으면 다시 제안하지 않음
        again = rt.knowledge.suggest_skills(existing={draft["name"]})
        assert again == []


def test_usage_compares_runs_with_and_without_hint(tmp_path: Path) -> None:
    """참고를 붙인 실행이 기록되고, 사용 이력에서 같은 업무 유형의 참고 없는 실행과 비교됨 (웹 경로 그대로)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM([LLMResponse("6월 정리 끝"), LLMResponse("7월 정리 끝")])
    rt = AgentRuntime(cfg, llm=llm)
    corpus(rt.users.root)  # kim·lee 의 예전 기록 (참고 없이 한 매출 업무 포함)
    app = create_app(cfg, runtime=rt, enable_scheduler=False)
    H = {"X-Agent-User": "kim"}
    with TestClient(app) as c:
        for m in ("6월", "7월"):
            run = c.post("/api/runs", json={"prompt": f"{m} 매출 엑셀 부서별로 정리해줘", "new_session": True}, headers=H).json()
            with c.stream("GET", f"/api/runs/{run['id']}/events", headers=H) as s:
                "".join(s.iter_text())
            turn = c.get(f"/api/sessions/{run['session_id']}", headers=H).json()["turns"][-1]
            assert "매출" in (turn["knowledge_hint"] or "")
        u = c.get("/api/usage?days=60", headers=H).json()
        ke = u["knowledge_effect"]
        assert ke["hinted"]["runs"] == 2 and ke["hinted"]["success_rate"] == 1.0
        assert ke["baseline"]["runs"] == 3 and ke["baseline"]["success_rate"] == pytest.approx(2 / 3, abs=0.01)  # kim 의 예전 매출 업무 3건
        assert "업무 지식 참고 효과" in u["text"] and "| 참고 붙음 | 2 | 100% |" in u["text"]
        drafts = c.post("/api/knowledge/suggest-skills", headers=H).json()["drafts"]
        assert drafts and drafts[0]["name"].startswith("auto_download_sales_excel")
