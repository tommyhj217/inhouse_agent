"""과거 자료 학습 → 규칙 → 검증 → 규칙 갱신 루프 테스트."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pptx")

from deck_factory import eval_sheet, make_deck, past_report  # noqa: E402
from test_agent import ScriptedLLM, write_config  # noqa: E402

from inhouse_agent.config import load_config  # noqa: E402
from inhouse_agent.docstruct import _pptx_fallback, doc_structure, outline_text  # noqa: E402
from inhouse_agent.llm import LLMResponse, ToolCall  # noqa: E402
from inhouse_agent.rulebook import RulebookError, RulebookStore, check_rule, same_rule, title_match  # noqa: E402
from inhouse_agent.runtime import AgentRuntime  # noqa: E402


@pytest.fixture()
def corpus(tmp_path: Path) -> dict[str, Path]:
    d = tmp_path / "past"
    return {
        "p1": make_deck(d / "뮤즈_분석.pptx", past_report("뮤즈")),
        "p2": make_deck(d / "AI비서_분석.pptx", past_report("AI 비서", numbering=False)),
        "p3": make_deck(d / "번역봇_분석.pptx", past_report("번역봇", swot=False)),
        "x1": eval_sheet(d / "평가_뮤즈.xlsx", "뮤즈"),
        "x2": eval_sheet(d / "평가_비서.xlsx", "AI 비서"),
    }


def test_doc_structure_per_slide(corpus) -> None:
    d = doc_structure(corpus["p1"])
    assert d["kind"] == "pptx" and len(d["sections"]) == 7
    s = {x["title"]: x for x in d["sections"]}
    assert s["2. 시장 현황"]["charts"] and s["2. 시장 현황"]["charts"][0]["categories"] == ["2024", "2025", "2026"]
    assert s["3. 경쟁사 비교"]["tables"][0]["columns"] == ["기업", "제품", "핵심 기능", "가격"]
    assert any("출처" in t for t in s["5. 시사점 및 제언"]["texts"])
    fb = _pptx_fallback(corpus["p1"])  # python-pptx 없이도 기본 구조는 읽힘
    assert [x["title"] for x in fb] == [x["title"] for x in d["sections"]] and fb[4]["tables"][0]["columns"][0] == "기업"
    x = doc_structure(corpus["x1"])
    assert x["kind"] == "xlsx" and x["sheets"][0]["columns"] == ["평가항목", "가중치", "점수", "근거"] and "가중치" in x["sheets"][0]["numeric"]
    text = outline_text(d)
    assert "#5 [3. 경쟁사 비교] | 표 2행×4열(열: 기업, 제품, 핵심 기능, 가격)" in text


def test_title_matching() -> None:
    assert title_match("3. 경쟁사 비교", ["경쟁사 비교"], 0.5) == 1.0
    assert title_match("경쟁사비교 분석", ["경쟁사 비교"], 0.5) == 1.0
    assert title_match("경쟁 현황 비교", ["경쟁사 비교"], 0.5) > 0
    assert title_match("시장 현황", ["경쟁사 비교"], 0.5) == 0


def test_learn_validate_and_self_update(tmp_path: Path, corpus) -> None:
    rb = RulebookStore(tmp_path / "rb").get("tech_report")
    rb.create("기술·제품 분석 보고서", user="kim")
    r = rb.learn(list(corpus.values()), user="kim")
    names = [b["name"] for b in r["blueprint"] if b["support"] >= 0.5]
    assert names == ["기술 분석 보고서", "목차", "개요", "시장 현황", "경쟁사 비교", "SWOT 분석", "시사점 및 제언"]  # 제품명은 빠진 공통 이름
    d = rb.load()
    by = {x["statement"]: x for x in d["rules"]}
    assert by["'경쟁사 비교' 장이 있어야 한다"]["status"] == "active" and by["'경쟁사 비교' 장이 있어야 한다"]["severity"] == "must"
    assert by["'SWOT 분석' 장이 있어야 한다"]["status"] == "candidate"  # 3건 중 2건 → 후보
    assert by["'경쟁사 비교' 표에는 기업, 제품, 핵심 기능, 가격 열이 있어야 한다"]["support"]["ratio"] == 1.0
    assert by["'평가표' 시트에는 평가항목, 가중치, 점수, 근거 열이 있어야 한다"]["status"] == "active"
    assert not any("뮤즈" in x["statement"] for x in d["rules"])
    # 같은 자료로 다시 학습해도 중복 규칙이 생기지 않음
    again = rb.learn(list(corpus.values()))
    assert again["added"] == [] and again["new_candidates"] == []

    # 새 제품 보고서 검증 (경쟁사 비교 없음)
    new = make_deck(tmp_path / "new" / "코파일럿.pptx", past_report("코파일럿", competitor_table=False))
    v = rb.validate(new, product="코파일럿", user="kim")
    fails = {x["rule"] for x in v["results"] if x["result"] == "fail"}
    comp_req = by["'경쟁사 비교' 장이 있어야 한다"]["id"]
    assert comp_req in fails and v["score"] < 100
    chk = {c["section"]: c for c in v["checklist"]}
    assert chk["경쟁사 비교"]["found"] is False and chk["시장 현황"]["has"] == ["chart"]
    assert rb.validate(corpus["x1"])["score"] == 100.0  # 엑셀은 엑셀 규칙으로만

    # 사람이 만든 규칙 + 판단형 규칙
    src = rb.add_rule({"type": "text_regex", "params": {"pattern": r"출처\s*[:：]"}, "statement": "출처를 표기해야 한다", "rationale": "모든 과거 자료 결론 장에 출처", "severity": "must", "status": "active"}, "kim")
    assert src["rule"]["support"]["ratio"] == 1.0
    with pytest.raises(RulebookError, match="같은 내용"):
        rb.add_rule({"type": "text_regex", "params": {"pattern": r"출처\s*[:：]"}, "statement": "중복"})
    jr = rb.add_rule({"type": "judgment", "params": {"question": "시사점이 앞 장 데이터로 뒷받침되는가"}, "statement": "시사점 근거", "status": "active"})["rule"]
    v1 = rb.validate(corpus["p1"])
    assert jr["id"] in v1["pending_judgments"]
    v1 = rb.judge(v1["id"], jr["id"], "pass", "시장 규모 차트를 근거로 권고", "kim")
    assert not v1["pending_judgments"]
    assert next(x for x in rb.load()["rules"] if x["id"] == jr["id"])["support"]["ratio"] == 1.0

    # 학습 루프: 경쟁사 비교 없이도 좋은 평가를 받은(승인된) 새 스타일 보고서들이 쌓이면 규칙이 재검토로 바뀜
    for i in range(3):
        ok = make_deck(tmp_path / "approved" / f"신규{i}.pptx", past_report(f"신규{i}", competitor_table=False))
        rb.validate(ok, label="exemplar", product=f"신규{i}")
    rule = next(x for x in rb.load()["rules"] if x["id"] == comp_req)
    assert rule["status"] == "needs_review" and rule["support"]["ratio"] == 0.5
    assert any("상태 active → needs_review" in h["change"] for h in rule["history"])
    # 사람이 '그래도 필수' 로 확정하면 잠겨서 자동 변경 안 됨
    rb.update_rule(comp_req, {"status": "active", "rationale": "경쟁 분석은 필수 (기획팀 확인)"}, "팀 기준", "kim")
    rb.learn([make_deck(tmp_path / "approved" / "신규9.pptx", past_report("신규9", competitor_table=False))])
    rule = next(x for x in rb.load()["rules"] if x["id"] == comp_req)
    assert rule["status"] == "active" and rule["locked"] is True and rule["version"] == 2

    # 스스로 점검
    ref = rb.reflect()
    kinds = {(s.get("rule"), s["kind"]) for s in ref["suggestions"]}
    assert (comp_req, "too_strict") in kinds
    # 변경기록 / 버전 백업
    log = rb.changelog()
    assert {"create", "learn", "validate", "add_rule", "judge", "update_rule"} <= {x["action"] for x in log}
    assert list((rb.dir / ".history").glob("rulebook_v*.json"))
    # 장별 계획
    plan = rb.outline("뮤즈 2")
    comp = next(p for p in plan if p["section"] == "경쟁사 비교")
    assert "table" in comp["needs"] and comp["columns"][:2] == ["기업", "제품"] and any(comp_req in x for x in comp["rules"])
    assert next(p for p in plan if p["section"] == "(문서 전체)")["rules"]


def test_rule_checks_units(tmp_path: Path, corpus) -> None:
    d = doc_structure(corpus["p1"])
    r = lambda t, p: check_rule({"type": t, "params": p, "statement": ""}, d)["result"]  # noqa: E731
    assert r("section_element", {"patterns": ["시장 현황"], "element": "chart"}) == "pass"
    assert r("section_element", {"patterns": ["개요"], "element": "table"}) == "fail"
    assert r("section_element", {"patterns": ["없는 장"], "element": "table"}) == "na"
    assert r("section_keywords", {"patterns": ["시사점"], "all": ["리스크"]}) == "pass"
    assert r("table_columns", {"patterns": ["경쟁사"], "columns": ["기업명", "가격", "출시일"], "min_match": 2}) == "pass"
    assert r("section_order", {"before": ["SWOT"], "after": ["개요"]}) == "fail"
    assert r("section_length", {"patterns": ["개요"], "min_words": 50}) == "fail"
    assert r("slide_count", {"min": 5, "max": 10}) == "pass"
    x = doc_structure(corpus["x1"])
    rx = lambda t, p: check_rule({"type": t, "params": p, "statement": ""}, x)["result"]  # noqa: E731
    assert rx("excel_values", {"sheet": "평가표", "column": "점수", "min": 1, "max": 5}) == "pass"
    assert rx("excel_values", {"sheet": "평가표", "column": "점수", "min": 1, "max": 3}) == "fail"
    assert rx("excel_values", {"sheet": "평가표", "column": "가중치", "not_empty": True}) == "fail"  # 비고 행 빈칸
    assert same_rule({"type": "section_required", "params": {"patterns": ["경쟁사 비교", "경쟁 현황"]}},
                     {"type": "section_required", "params": {"patterns": ["경쟁사 비교"]}})


async def test_agent_tools_product_loop(tmp_path: Path, corpus) -> None:
    """에이전트 tool 로: 학습 → 장별 계획 → PPT 작성(과거 보고서 템플릿) → 검증 → 규칙 후보 추가 → 제품 기록 → 내보내기."""
    cfg = load_config(write_config(tmp_path))
    slides = [{"title": "뮤즈 2 기술 분석 보고서", "cover": True}, {"title": "목차", "bullets": ["개요", "시장 현황", "경쟁사 비교", "SWOT", "시사점"]},
              {"title": "1. 개요", "bullets": ["뮤즈 2 는 사내 AI 비서", "핵심 기능 요약", "출시 일정 2027"]},
              {"title": "2. 시장 현황", "bullets": ["시장 규모"], "chart": {"type": "bar", "categories": ["2025", "2026"], "series": [{"name": "시장", "values": [13, 17]}]}},
              {"title": "3. 경쟁사 비교", "table": {"columns": ["기업", "제품", "핵심 기능", "가격"], "rows": [["A사", "A1", "요약", "무료"]]}},
              {"title": "4. SWOT 분석", "table": {"columns": ["강점", "약점", "기회", "위협"], "rows": [["정확도", "비용", "수요", "규제"]]}},
              {"title": "5. 시사점 및 제언", "bullets": ["도입 권고: 시범 적용", "리스크 관리 필요", "출처: 사내 조사, 2026"]}]
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        past = rt.workspace.uploads / "과거보고서"
        past.mkdir(parents=True)
        for f in corpus.values():
            (past / f.name).write_bytes(f.read_bytes())
        out = await rt.hub.call("rulebook_create", {"name": "tech_report", "description": "기술 분석"})
        assert "생성" in out.text
        out = await rt.hub.call("rulebook_learn", {"name": "tech_report", "paths": ["uploads/과거보고서"]})
        assert "새 자료 5건" in out.text and "| 경쟁사 비교 |" in out.text, out.text
        out = await rt.hub.call("rulebook_outline", {"name": "tech_report", "product": "뮤즈 2"})
        assert "경쟁사 비교" in out.text and "표 열: 기업, 제품, 핵심 기능, 가격" in out.text
        out = await rt.hub.call("write_pptx", {"path": "reports/뮤즈2_분석", "slides": slides, "template": "uploads/과거보고서/뮤즈_분석.pptx"})
        assert not out.is_error and out.files[0]["state"] == "complete", out.text
        made = Path(out.files[0]["path"])
        st = doc_structure(made)
        assert [s["title"] for s in st["sections"]] == [s["title"] for s in slides]  # 템플릿 기존 슬라이드는 지워짐
        assert st["sections"][3]["charts"] and st["sections"][4]["tables"][0]["columns"][0] == "기업"
        out = await rt.hub.call("rulebook_validate", {"name": "tech_report", "path": str(made), "product": "뮤즈 2"})
        assert "점수 100.0점" in out.text and "장별 체크리스트" in out.text, out.text
        out = await rt.hub.call("rulebook_add_rule", {"name": "tech_report", "type": "section_required", "params": {"patterns": ["보안 리스크", "데이터 보안"]},
                                                      "statement": "생성형 AI 제품은 '데이터·보안 리스크' 장이 있어야 한다", "rationale": "뮤즈 2 분석 중 필요성 확인", "status": "candidate"})
        assert "지지율(좋은 예시): 0%" in out.text and "조건을 바꾸거나" in out.text
        out = await rt.hub.call("rulebook_record_product", {"name": "tech_report", "product": "뮤즈 2", "summary": "사내 AI 비서, 시범 적용 권고", "facts": {"시장규모": "17조"}, "tags": ["AI 비서"]})
        assert "분석 1회" in out.text
        out = await rt.hub.call("rulebook_reflect", {"name": "tech_report"})
        assert "weak_candidate" in out.text or "uncovered_section" in out.text or "few_exemplars" in out.text
        out = await rt.hub.call("rulebook_export", {"name": "tech_report"})
        assert out.files and out.files[0]["state"] == "complete"
        import openpyxl

        wb = openpyxl.load_workbook(out.files[0]["path"])
        assert wb.sheetnames == ["장별구성", "규칙", "검증이력", "제품"]
        out = await rt.hub.call("doc_structure", {"path": str(made), "sections": [5]})
        assert "#5 [3. 경쟁사 비교]" in out.text and "#1" not in out.text
        # 시스템 프롬프트에 규칙집 목록
        assert "tech_report" in rt.agent.build_system_prompt()


def test_web_rulebook_api(tmp_path: Path, corpus) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(write_config(tmp_path))
    rt = AgentRuntime(cfg, llm=ScriptedLLM([]))
    app = create_app(cfg, runtime=rt, enable_scheduler=False)
    H = {"X-Agent-User": "kim"}
    with TestClient(app) as c:
        for f in corpus.values():
            c.post(f"/api/files/upload?name={f.name}", content=f.read_bytes(), headers=H)
        assert c.post("/api/rulebooks", json={"name": "tech_report", "description": "기술"}, headers=H).json()["ok"]
        assert c.post("/api/rulebooks", json={"name": "tech_report"}, headers=H).status_code == 400
        r = c.post("/api/rulebooks/tech_report/learn", json={"paths": ["uploads"]}, headers=H).json()
        assert len(r["added"]) == 5 and r["new_candidates"]
        d = c.get("/api/rulebooks/tech_report", headers=H).json()
        rid = next(x["id"] for x in d["rules"] if x["statement"] == "'SWOT 분석' 장이 있어야 한다")
        new = make_deck(tmp_path / "n.pptx", past_report("코파일럿", swot=False))
        up = c.post("/api/files/upload?name=코파일럿.pptx", content=new.read_bytes(), headers=H).json()
        v = c.post("/api/rulebooks/tech_report/validate", json={"path": up["path"], "product": "코파일럿"}, headers=H).json()
        assert v["score"] == 100.0 and any(x["section"] == "SWOT 분석" and not x["found"] for x in v["checklist"])  # 후보 규칙은 점수에 안 들어감
        assert c.patch(f"/api/rulebooks/tech_report/rules/{rid}", json={"changes": {"status": "active"}, "reason": "팀 기준"}, headers=H).status_code == 200
        v2 = c.post("/api/rulebooks/tech_report/validate", json={"path": up["path"]}, headers=H).json()
        assert v2["score"] < 100
        assert c.get(f"/api/rulebooks/tech_report/validations/{v2['id']}", headers=H).json()["id"] == v2["id"]
        assert len(c.get("/api/rulebooks/tech_report/validations", headers=H).json()) == 2
        assert c.get("/api/rulebooks/tech_report/reflect", headers=H).json()["suggestions"] is not None
        assert c.get("/api/rulebooks/tech_report/changelog", headers=H).json()[0]["user"] == "kim"
        ex = c.post("/api/rulebooks/tech_report/export", headers=H).json()
        assert ex["files"][0]["state"] == "complete"
        assert c.post("/api/rulebooks/tech_report/learn", json={"paths": ["/etc"]}, headers=H).status_code == 403
        assert c.get("/api/rulebooks", headers=H).json()[0]["name"] == "tech_report"
