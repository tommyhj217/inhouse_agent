"""/ 명령(스킬·도구) 과 LLM 없이(토큰 없이) 실행하는 워크플로 테스트."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.llm import LLMResponse
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.skills import SkillError, SkillStore, parse_skill
from inhouse_agent.workflow import check_when, render, validate_steps

ROOT = Path(__file__).resolve().parents[1]


def _offline_config(tmp_path: Path) -> Path:
    """LLM 없이 쓰는 설정 (provider: none) + 저장소의 샘플 스킬."""
    p = write_config(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace("llm:\n", "llm:\n  provider: none\n", 1), encoding="utf-8")
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    return p


def test_render_and_conditions() -> None:
    ctx = {"inputs": {"month": "2026-10", "n": 3, "paths": "a\nb, c"}, "prev": "앞 결과", "timezone": "Asia/Seoul",
           "steps": {"s1": {"text": "T", "files": ["/w/x.xlsx", "/w/y.csv"], "data": {"items": [{"id": "N1"}, {"id": "N2"}]}, "error": ""}}}
    assert render("{n}", ctx) == 3  # 값 전체가 변수면 타입 유지
    assert render({"p": "{steps.s1.file}", "ids": ["{steps.s1.data.items.1.id}"], "q": "월 {month} / {prev}"}, ctx) == {"p": "/w/x.xlsx", "ids": ["N2"], "q": "월 2026-10 / 앞 결과"}
    assert render("{paths|lines}", ctx) == ["a", "b", "c"]
    assert render("{steps.s1.files|first}", ctx) == "/w/x.xlsx"
    assert render('{"json": 1} {모름}', ctx) == '{"json": 1} {모름}'  # 모르는 중괄호는 그대로
    assert render("{steps.none.text}", ctx) == ""
    assert "-" in render("{yesterday}", ctx)
    assert check_when("{month} == 2026-10", ctx) and not check_when("{month} != 2026-10", ctx)
    assert check_when("{prev}", ctx) and not check_when("{steps.none.text}", ctx)
    with pytest.raises(ValueError):
        validate_steps([{"args": {}}])
    with pytest.raises(ValueError):
        validate_steps([{"id": "a", "tool": "x"}, {"id": "a", "tool": "y"}])
    with pytest.raises(SkillError, match="tool"):
        parse_skill("---\nname: bad\ndescription: d\nsteps:\n  - args: {}\n---\n본문", Path("bad"))


async def test_skills_run_without_llm(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    from deck_factory import eval_sheet, make_deck, past_report

    cfg = load_config(_offline_config(tmp_path))
    async with AgentRuntime(cfg) as rt:
        assert rt.llm_status()[0] is False
        r, path = await rt.run_skill("monthly_sales_report", {"month": "2026-10", "dept": "IT운영팀"})
        assert r.success and [e.tool for e in r.tool_events] == ["download_sales_excel", "query_table", "query_table"]  # 조건으로 1단계 건너뜀
        assert "부서: IT운영팀" in r.output and "| B제품 |" in r.output and path.exists()
        r, _ = await rt.run_skill("notice_digest", {"date": "2026-11-01"})
        assert r.success and "첨부파일" in r.output and any(f["name"].endswith("_첨부.csv") for f in r.files)
        # 규칙 학습도 토큰 없이: 만들기 → 학습 → 점검 → 엑셀
        past = rt.workspace.uploads / "past"
        make_deck(past / "a.pptx", past_report("뮤즈"))
        make_deck(past / "b.pptx", past_report("AI 비서", numbering=False))
        eval_sheet(past / "e.xlsx", "x")
        r, _ = await rt.run_skill("learn_rules", {"rulebook": "tech", "paths": "uploads/past"})
        assert r.success and "| 경쟁사 비교 |" in r.output and ".xlsx" in r.output, r.output
        r, _ = await rt.run_skill("learn_rules", {"rulebook": "tech", "paths": "uploads/past"})  # 이미 있으면 만들기 단계만 실패하고 계속
        assert r.success and "이미 있습니다" in r.tool_events[0].result
        r, _ = await rt.run_skill("product_analysis", {"product": "뮤즈 3", "rulebook": "tech"})
        assert r.success and "장별 계획" in r.output and "경쟁사 비교" in r.output
        # steps 가 없는 스킬은 이유를 알려줌
        sk = tmp_path / "skills" / "plain"
        sk.mkdir()
        (sk / "SKILL.md").write_text("---\nname: plain\ndescription: 지침만 있는 스킬\n---\n본문", encoding="utf-8")
        with pytest.raises(SkillError, match="steps"):
            await rt.run_skill("plain", {})
        # 실패한 단계에서 멈춤 / on_error continue
        bad = tmp_path / "skills" / "broken_wf"
        bad.mkdir()
        (bad / "SKILL.md").write_text("---\nname: broken_wf\ndescription: d\nsteps:\n  - tool: no_such_tool\n  - tool: get_current_time\n---\nx", encoding="utf-8")
        r, _ = await rt.run_skill("broken_wf", {})
        assert r.stopped_reason == "error" and "멈췄습니다" in r.output and len(r.tool_events) == 1


async def test_workflow_mode_with_llm_does_not_call_llm(tmp_path: Path) -> None:
    p = write_config(tmp_path)
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    llm = ScriptedLLM([])  # 호출되면 IndexError
    async with AgentRuntime(load_config(p), llm=llm) as rt:
        assert rt.llm_status()[0] is True
        r, _ = await rt.run_skill("monthly_sales_report", {"month": "2026-07"}, mode="workflow")
        assert r.success and llm.calls == []


def test_web_slash_commands_offline(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(_offline_config(tmp_path))
    app = create_app(cfg, runtime=AgentRuntime(cfg), enable_scheduler=False)
    H = {"X-Agent-User": "kim"}

    def events(c, rid):
        with c.stream("GET", f"/api/runs/{rid}/events", headers=H) as s:
            return [json.loads(x[6:]) for x in "".join(s.iter_text()).splitlines() if x.startswith("data: ")]

    with TestClient(app) as c:
        info = c.get("/api/info").json()
        assert "auth" not in info and info["llm"]["ready"] is False  # 토큰 없이 사용
        cmds = c.get("/api/commands", headers=H).json()
        by = {(x["kind"], x["name"]): x for x in cmds["items"]}
        assert by[("skill", "monthly_sales_report")]["offline"] is True and by[("skill", "monthly_sales_report")]["steps"] == 4
        assert ("tool", "analyze_file") in by and ("tool", "demo__list_notices") in by and ("command", "help") in by
        assert cmds["llm"]["ready"] is False

        # /스킬 실행 (자동 → 워크플로)
        run = c.post("/api/runs", json={"skill": "monthly_sales_report", "inputs": {"month": "2026-10"}, "new_session": True}, headers=H).json()
        ev = events(c, run["id"])
        assert ev[0]["mode"] == "auto" and ev[-1]["status"] == "completed" and "매출 집계" in ev[-1]["output"]
        assert [e["tool"] for e in ev if e["type"] == "tool_end"] == ["download_sales_excel", "query_table", "query_table"]
        assert any(e["type"] == "plan" for e in ev)
        # /도구 직접 실행
        run2 = c.post("/api/runs", json={"tool": "get_current_time", "args": {}, "session_id": run["session_id"]}, headers=H).json()
        ev2 = events(c, run2["id"])
        assert ev2[-1]["status"] == "completed" and "Asia/Seoul" in ev2[-1]["output"]
        d = c.get(f"/api/sessions/{run['session_id']}", headers=H).json()
        assert [t["prompt"] for t in d["turns"]] == ['/monthly_sales_report {"month": "2026-10"}', "/get_current_time {}"]
        # LLM 없이 자유 질문 → 사용법 안내 (오류 아님)
        run3 = c.post("/api/runs", json={"prompt": "안녕"}, headers=H).json()
        ev3 = events(c, run3["id"])
        assert ev3[-1]["status"] == "completed" and "/ 를 치면" in ev3[-1]["output"].replace("**", "")
        # 스킬 대화에 이어서 한 말은 (LLM 없으면) 워크플로를 다시 돌리지 않고 안내
        run4 = c.post("/api/runs", json={"prompt": "이번엔 9월로", "session_id": run["session_id"]}, headers=H).json()
        ev4 = events(c, run4["id"])
        assert "/ 를 치면" in ev4[-1]["output"].replace("**", "") and not [e for e in ev4 if e["type"] == "tool_end"]
        plan = [e for e in ev if e["type"] == "plan"][-1]["steps"]
        assert [x["status"] for x in plan] == ["done", "done", "skipped", "done"]  # 조건으로 건너뛴 단계는 skipped
        # 잘못된 요청
        assert c.post("/api/runs", json={"tool": "no_such_tool"}, headers=H).status_code == 400
        assert c.post("/api/runs", json={"prompt": "x", "mode": "agent"}, headers=H).status_code == 400
        assert c.post("/api/runs", json={"skill": "learn_rules", "inputs": {"paths": "x"}, "mode": "nope"}, headers=H).status_code == 400
        # 화면에 / 메뉴와 스킬 버튼이 있음
        html = c.get("/").text
        assert 'id="slash"' in html and 'id="chat-skill"' in html and "runSlashText" in html


def test_bundled_skills_have_workflows() -> None:
    skills, errors = SkillStore(ROOT / "skills").list()
    assert not errors
    assert {s.name for s in skills if s.steps} == {"monthly_sales_report", "notice_digest", "learn_rules", "product_analysis", "product_rule_loop"}
