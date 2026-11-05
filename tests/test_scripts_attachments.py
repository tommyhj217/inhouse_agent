"""스킬 스크립트 실행, 사용 이력/모델 목록, PPT·엑셀 첨부와 비교 분석, 스킬 zip 가져오기."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from deck_factory import make_deck
from test_agent import ScriptedLLM, write_config
from test_multiuser_skills import _cfg

from inhouse_agent.compare import compare_documents, numbers
from inhouse_agent.config import load_config
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.users import current_user

MENU_SKILL = """---
name: menu_builder
description: 메뉴 생성 스크립트로 메뉴판을 만든다
inputs:
  - name: title
    default: 오늘의 메뉴
steps:
  - id: m
    title: 메뉴 생성
    script: scripts/make_menu.py
    args: {title: "{title}", items: [김치찌개, 제육볶음]}
output: |
  ## 결과
  {steps.m.text}
---
1. scripts/make_menu.py 를 실행해 메뉴 파일을 만든다.
"""

MENU_PY = '''import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument("--title")
p.add_argument("--items", nargs="*")
a = p.parse_args()
with open("menu.html", "w", encoding="utf-8") as f:
    f.write("<h1>%s</h1><ul>%s</ul>" % (a.title, "".join("<li>%s</li>" % x for x in a.items)))
print(json.dumps({"title": a.title, "count": len(a.items), "skill_dir": os.path.basename(os.environ["SKILL_DIR"])}, ensure_ascii=False))
'''

CTX_PY = '''def run(args, ctx):
    t = ctx.call_tool("get_current_time", {})
    return {"time_ok": "Asia" in t, "who": args.get("who")}
'''


def _menu_skill(root: Path) -> None:
    d = root / "skills" / "menu_builder"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(MENU_SKILL, encoding="utf-8")
    (d / "scripts" / "make_menu.py").write_text(MENU_PY, encoding="utf-8")
    (d / "scripts" / "with_ctx.py").write_text(CTX_PY, encoding="utf-8")


def _decks(d: Path) -> tuple[Path, Path, Path, Path]:
    import openpyxl

    a = make_deck(d / "v1.pptx", [{"title": "10월 실적 보고", "cover": True}, {"title": "매출 요약", "bullets": ["총매출 12.3억 원", "전월 대비 15.3% 증가"]},
                                  {"title": "부서별 현황", "table": {"columns": ["부서", "금액"], "rows": [["영업", "800,000,000"], ["기술", "430,000,000"]]}},
                                  {"title": "향후 계획", "bullets": ["신규 고객 확보"]}])
    b = make_deck(d / "v2.pptx", [{"title": "10월 실적 보고", "cover": True}, {"title": "매출 요약", "bullets": ["총매출 13.1억 원", "전월 대비 18.0% 증가"]},
                                  {"title": "부서별 현황", "table": {"columns": ["부서", "금액"], "rows": [["영업", "850,000,000"], ["기술", "460,000,000"]]}},
                                  {"title": "리스크", "bullets": ["환율 변동"]}, {"title": "향후 계획", "bullets": ["신규 고객 확보"]}])
    xs = []
    for name, rows in (("s1.xlsx", [["부서", "금액"], ["영업", 800000000], ["기술", 430000000]]),
                       ("s2.xlsx", [["부서", "금액"], ["영업", 850000000], ["기술", 460000000], ["해외", 1000000]])):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "실적"
        for r in rows:
            ws.append(r)
        wb.save(d / name)
        xs.append(d / name)
    return a, b, xs[0], xs[1]


async def test_run_skill_script_tool_and_workflow(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path))
    _menu_skill(tmp_path)
    async with AgentRuntime(cfg) as rt:
        out = await rt.hub.call("run_skill_script", {"name": "menu_builder", "path": "make_menu.py", "args": {"title": "점심", "items": ["국밥", "냉면"]}})
        assert not out.is_error, out.text
        assert out.data == {"title": "점심", "count": 2, "skill_dir": "menu_builder"}
        menu = Path(out.files[0]["path"])
        assert menu.name == "menu.html" and "<li>냉면</li>" in menu.read_text(encoding="utf-8") and "skill_menu_builder" in str(menu)
        # def run(args, ctx) 형 스크립트 → ctx.call_tool 로 다른 도구 호출
        out = await rt.hub.call("run_skill_script", {"name": "menu_builder", "path": "scripts/with_ctx.py", "args": {"who": "kim"}})
        assert not out.is_error and '"time_ok": true' in out.text and "kim" in out.text, out.text
        # 없는 스크립트 → 실행 가능한 목록 안내, 스킬 폴더 밖 경로는 거부
        bad = await rt.hub.call("run_skill_script", {"name": "menu_builder", "path": "../../etc/passwd"})
        assert bad.is_error and "scripts/make_menu.py" in bad.text
        # 스킬 지침/목록에 스크립트가 보임 → 에이전트가 '실행할 도구가 없다' 고 하지 않음
        assert "run_skill_script" in (await rt.hub.call("use_skill", {"name": "menu_builder"})).text
        assert "scripts/make_menu.py" in (await rt.hub.call("list_skill_scripts", {})).text
        # 토큰 없이 steps 의 script: 단계로 실행
        res, path = await rt.run_skill("menu_builder", {"title": "저녁"})
        assert res.success and "성공" in res.output and "menu.html" in res.output, res.output
        assert any(f["path"].endswith("menu.html") for f in res.files)


async def test_skill_scripts_can_be_disabled(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path, "skills:\n  scripts: off\n"))
    _menu_skill(tmp_path)
    async with AgentRuntime(cfg) as rt:
        out = await rt.hub.call("run_skill_script", {"name": "menu_builder", "path": "scripts/make_menu.py"})
        assert out.is_error and "꺼져" in out.text


def test_compare_documents(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    a, b, s1, s2 = _decks(tmp_path)
    text, data = compare_documents([a, b])
    deck = data["pairs"][0]
    assert deck["type"] == "deck" and [x["title"] for x in deck["added"]] == ["리스크"] and not deck["removed"]
    assert not deck["reordered"]  # 장 추가로 번호가 밀린 것은 순서 변경이 아님
    summary = next(c for c in deck["changed"] if c["title_b"] == "매출 요약")
    assert summary["numbers"] == {"before": ["12.3억", "15.3%"], "after": ["13.1억", "18.0%"]}
    assert "추가된 장: #4 리스크" in text
    text, data = compare_documents([s1, s2])
    sh = data["pairs"][0]["sheets"][0]
    assert sh["key"] == ["부서"] and sh["added_rows"] == 1 and sh["changed_rows"] == 2
    assert sh["sums"][0]["diff"] == 81000000 and "+6.6%" in text
    # PPT 숫자 ↔ 엑셀 교차 확인 (억 단위, 합계 포함)
    _, data = compare_documents([b, s2])
    cross = data["pairs"][0]
    assert cross["type"] == "cross" and cross["matched"] == 3 and [m["value"] for m in cross["unmatched_sample"]] == ["18.0%"]
    assert numbers("3장 2026년 매출 1,234 15%") == [(1234.0, "1,234"), (15.0, "15%")]


def test_attachments_offline_compare_usage_models(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("pptx")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    a, b, _, _ = _decks(tmp_path)
    cfg = load_config(_cfg(tmp_path, "skills:\n  editors: [kim]\n"))
    app = create_app(cfg, runtime=AgentRuntime(cfg), enable_scheduler=False)
    H = {"X-Agent-User": "lee"}
    with TestClient(app) as c:
        up = [c.post("/api/attachments/upload?name=" + p.name, content=p.read_bytes(), headers=H).json() for p in (a, b)]
        assert up[0]["kind"] == "document" and up[0]["summary"] == "PPT 4장" and "/users/lee/uploads/attachments/" in up[0]["path"].replace("\\", "/")
        assert c.post("/api/attachments/upload?name=x.exe", content=b"MZ", headers=H).status_code == 400
        # 토큰 없이: 첨부만 하면 자동 비교 분석
        run = c.post("/api/runs", json={"prompt": "", "attachments": [u["path"] for u in up], "new_session": True}, headers=H).json()
        with c.stream("GET", f"/api/runs/{run['id']}/events", headers=H) as s:
            body = "".join(s.iter_text())
        assert '"status": "completed"' in body and "compare_documents" in body
        done = json.loads([ln for ln in body.splitlines() if '"type": "done"' in ln][-1][len("data: "):])
        assert "추가된 장" in done["output"] and "LLM 없이 자동 분석" in done["output"]
        turn = c.get(f"/api/sessions/{run['session_id']}", headers=H).json()["turns"][0]
        assert turn["attachments"] == [u["path"] for u in up] and turn["kind"] == "chat"
        # 도구 직접 실행도 이력에 남음
        run2 = c.post("/api/runs", json={"tool": "get_current_time", "args": {}, "session_id": run["session_id"]}, headers=H).json()
        with c.stream("GET", f"/api/runs/{run2['id']}/events", headers=H) as s:
            "".join(s.iter_text())
        u = c.get("/api/usage?days=7", headers=H).json()
        assert u["runs"] == 2 and u["kinds"] == {"chat": 1, "tool": 1} and "compare_documents" in u["tools"] and "에이전트 사용 이력" in u["text"]
        assert c.get("/api/usage?all=true", headers=H).status_code == 403  # 다른 사용자 이력은 관리자만
        allu = c.get("/api/usage?all=true", headers={"X-Agent-User": "kim"}).json()
        assert allu["users"]["lee"]["runs"] == 2
        m = c.get("/api/models", headers=H).json()
        assert m["ready"] is False and "모델 목록" in m["text"] and "현재 사용 모델" in m["text"]
        html = c.get("/").text
        assert "/api/attachments/upload" in html and "pastedTable" in html and "st-usage" in html and "sk-import" in html


async def test_agent_gets_attachment_note_and_usage_tool(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    cfg = load_config(write_config(tmp_path))
    up = cfg.workspace_path / "users" / "park" / "uploads" / "attachments"
    up.mkdir(parents=True)
    a, b, _, _ = _decks(up)
    llm = ScriptedLLM([
        LLMResponse(content=None, tool_calls=[ToolCall("c1", "compare_documents", json.dumps({"paths": [str(a), str(b)]})),
                                              ToolCall("c2", "agent_usage", "{}"), ToolCall("c3", "list_models", '{"remote": false}')]),
        LLMResponse(content="비교 완료"),
    ])
    async with AgentRuntime(cfg, llm=llm) as rt:
        tok = current_user.set("park")
        try:
            res = await rt.agent.run("두 보고서 차이 알려줘", attachments=[str(a), str(b)])
        finally:
            current_user.reset(tok)
        user_msg = [m for m in llm.calls[0] if m["role"] == "user"][-1]["content"]
        assert "[첨부 파일 2개" in user_msg and str(a) in user_msg and "compare_documents" in user_msg and "매출 요약" in user_msg
        ev = {e.function: e for e in res.tool_events}
        assert "추가된 장" in ev["compare_documents"].result
        assert "에이전트 사용 이력" in ev["agent_usage"].result and "test-deploy" in ev["list_models"].result
        assert "run_skill_script" in llm.calls[0][0]["content"] and "agent_usage" in llm.calls[0][0]["content"]


def test_skill_zip_import_and_files(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("menu-builder/SKILL.md", MENU_SKILL.replace("name: menu_builder", "name: menu-builder"))
        z.writestr("menu-builder/scripts/make_menu.py", MENU_PY)
        z.writestr("menu-builder/../../evil.py", "x")
    cfg = load_config(_cfg(tmp_path, "skills:\n  editors: [kim]\n"))
    app = create_app(cfg, runtime=AgentRuntime(cfg), enable_scheduler=False)
    KIM, LEE = {"X-Agent-User": "kim"}, {"X-Agent-User": "lee"}
    with TestClient(app) as c:
        assert c.post("/api/skills/import", content=buf.getvalue(), headers=LEE).status_code == 403
        r = c.post("/api/skills/import", content=buf.getvalue(), headers=KIM).json()
        assert r["parsed"]["name"] == "menu-builder" and r["parsed"]["files"] == ["scripts/make_menu.py"]
        assert not (tmp_path / "evil.py").exists() and not (tmp_path / "skills" / "evil.py").exists()
        assert c.post("/api/skills/import", content=buf.getvalue(), headers=KIM).status_code == 400  # 이미 있음
        assert c.post("/api/skills/import?overwrite=true", content=buf.getvalue(), headers=KIM).status_code == 200
        # 파일 추가/보기/삭제 (편집자만), 경로 탈출 거부
        assert c.put("/api/skills/menu-builder/files?path=templates/t.txt", content=b"hi", headers=LEE).status_code == 403
        assert c.put("/api/skills/menu-builder/files?path=templates/t.txt", content=b"hi", headers=KIM).json()["files"] == ["scripts/make_menu.py", "templates/t.txt"]
        assert c.put("/api/skills/menu-builder/files?path=../x.txt", content=b"x", headers=KIM).status_code == 400
        assert c.get("/api/skills/menu-builder/files?path=templates/t.txt", headers=LEE).json()["content"] == "hi"
        assert c.delete("/api/skills/menu-builder/files?path=templates/t.txt", headers=KIM).json()["files"] == ["scripts/make_menu.py"]
        # 가져온 스킬은 모든 사용자 / 목록에 보이고 토큰 없이 스크립트까지 실행
        items = {x["name"]: x for x in c.get("/api/commands", headers=LEE).json()["items"]}
        assert items["menu-builder"]["offline"] is True and items["run_skill_script"]["kind"] == "tool"
        run = c.post("/api/runs", json={"skill": "menu-builder", "inputs": {}, "new_session": True}, headers=LEE).json()
        with c.stream("GET", f"/api/runs/{run['id']}/events", headers=LEE) as s:
            body = "".join(s.iter_text())
        assert '"status": "completed"' in body and "menu.html" in body
