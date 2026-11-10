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
