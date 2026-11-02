"""파일 상태/분석, 스킬, ask_user, 웹 API(실행 스트리밍) 테스트."""

from __future__ import annotations

import asyncio
import json
import threading
import zipfile
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent import analyzers
from inhouse_agent.config import load_config
from inhouse_agent.files import FileAccessError, FileWorkspace, file_status, wait_for_download
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.skills import SkillError, SkillStore, parse_skill

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 파일 상태
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path: Path) -> FileWorkspace:
    return FileWorkspace(tmp_path / "ws", watch_dirs=[tmp_path / "browser_dl"])


async def test_file_states(ws: FileWorkspace) -> None:
    d = ws.downloads
    (d / "ok.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (d / "empty.xlsx").write_bytes(b"")
    (d / "login.xlsx").write_text("<html><body>로그인이 필요합니다</body></html>", encoding="utf-8")
    (d / "cut.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")  # %%EOF 없음 → 잘린 파일
    (d / "bad.xlsx").write_bytes(b"PK\x03\x04" + b"\x00" * 100)  # 깨진 zip
    (d / "big.zip.crdownload").write_bytes(b"x" * 10)
    states = {}
    for name in ("ok.csv", "empty.xlsx", "login.xlsx", "cut.pdf", "bad.xlsx", "big.zip", "none.txt"):
        st = await file_status(d / name, ws, stable_wait=0)
        states[name] = st["state"]
    assert states == {
        "ok.csv": "complete",
        "empty.xlsx": "empty",
        "login.xlsx": "suspicious",
        "cut.pdf": "corrupt",
        "bad.xlsx": "corrupt",
        "big.zip": "downloading",
        "none.txt": "missing",
    }


async def test_redownload_in_progress(ws: FileWorkspace) -> None:
    (ws.downloads / "r.csv").write_text("old\n", encoding="utf-8")
    (ws.downloads / "r.csv.crdownload").write_bytes(b"new...")
    st = await file_status(ws.downloads / "r.csv", ws, stable_wait=0)
    assert st["state"] == "downloading"


async def test_size_change_means_downloading(ws: FileWorkspace) -> None:
    p = ws.downloads / "growing.csv"
    p.write_text("a\n", encoding="utf-8")

    async def grow():
        await asyncio.sleep(0.2)
        p.write_text("a\n1\n2\n", encoding="utf-8")

    t = asyncio.create_task(grow())
    st = await file_status(p, ws, stable_wait=0.5)
    await t
    assert st["state"] == "downloading"


async def test_wait_for_download_in_extra_watch_dir(ws: FileWorkspace, tmp_path: Path) -> None:
    dl = tmp_path / "browser_dl"
    dl.mkdir()

    def browser():
        import time

        tmp = dl / "report.xlsx.crdownload"
        tmp.write_bytes(b"partial")
        time.sleep(0.8)
        with zipfile.ZipFile(tmp, "w") as z:
            z.writestr("xl/workbook.xml", "<x/>")
        tmp.replace(dl / "report.xlsx")

    threading.Thread(target=browser, daemon=True).start()
    st = await wait_for_download(ws, "*.xlsx", timeout=10, poll=0.2)
    assert st["state"] == "complete" and st["name"] == "report.xlsx"
    st2 = await wait_for_download(ws, "nothing*.pdf", timeout=0.5, poll=0.2)
    assert st2["state"] == "timeout"


def test_path_sandbox(ws: FileWorkspace) -> None:
    with pytest.raises(FileAccessError):
        ws.resolve("/etc/passwd")
    with pytest.raises(FileAccessError):
        ws.resolve("../../outside.txt", write=True)
    assert ws.resolve("files/x.md", write=True).parent == ws.files


# ---------------------------------------------------------------------------
# 분석
# ---------------------------------------------------------------------------


def test_analyze_csv_cp949_and_query(tmp_path: Path) -> None:
    p = tmp_path / "sales.csv"
    p.write_bytes("보고서 제목\n\n부서,금액,비고\n인사팀,\"1,000\",\n인사팀,2000,a\nIT운영팀,500,b\n".encode("cp949"))
    text = analyzers.analyze(p)
    assert "3행 × 3열" in text and "합계=3,500" in text and "상단 2행" in text
    q = analyzers.query_table(p, group_by=["부서"], aggregates=[{"column": "금액", "func": "sum"}])
    assert "| 인사팀 | 3,000 |" in q and "| IT운영팀 | 500 |" in q
    q2 = analyzers.query_table(p, filters=[{"column": "금액", "op": ">", "value": 900}], columns=["부서"])
    assert "행 2개" in q2
    with pytest.raises(KeyError):
        analyzers.query_table(p, filters=[{"column": "없는열", "op": "==", "value": 1}])


def test_analyze_html_xls_docx_zip(tmp_path: Path) -> None:
    x = tmp_path / "export.xls"  # 사내 시스템이 흔히 내보내는 HTML 표 형식 xls
    x.write_text("<html><table><tr><th>이름</th><th>점수</th></tr><tr><td>가</td><td>10</td></tr><tr><td>나</td><td>20</td></tr></table></html>", encoding="utf-8")
    assert "2행 × 2열" in analyzers.analyze(x)

    d = tmp_path / "memo.docx"
    with zipfile.ZipFile(d, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>첫 문단</w:t></w:r></w:p><w:p><w:r><w:t>둘째 문단</w:t></w:r></w:p></w:body></w:document>")
    out = analyzers.analyze(d)
    assert "첫 문단" in out and "둘째 문단" in out

    zp = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("a.csv", "x\n1\n")
        z.writestr("../evil.txt", "no")
    assert "2개 항목" in analyzers.analyze(zp)
    files = analyzers.extract_zip(zp, tmp_path / "out")
    assert [f.name for f in files] == ["a.csv"]  # zip slip 차단


def test_analyze_xlsx(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    p = tmp_path / "r.xlsx"
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "요약"
    ws1.append(["월 보고서"])
    ws1.append(["항목", "값"])
    ws1.append(["A", 1])
    ws2 = wb.create_sheet("상세")
    ws2.append(["일자", "금액"])
    ws2.append(["2026-10-01", 100])
    wb.save(p)
    out = analyzers.analyze(p)
    assert "시트/표 2개" in out and "요약(1행)" in out and "상세(1행)" in out
    assert "| 2026-10-01 | 100 |" in analyzers.query_table(p, sheet="상세")


# ---------------------------------------------------------------------------
# 스킬
# ---------------------------------------------------------------------------

SKILL = """---
name: t1
description: 테스트 스킬
servers: [demo]
inputs:
  - name: month
    default: "{last_month}"
    required: true
  - name: dept
    type: select
    options: [전체, 인사팀]
    default: 전체
---
{month} 의 {dept} 자료를 정리
"""


def test_skill_parse_render_and_store(tmp_path: Path) -> None:
    sk = parse_skill(SKILL, tmp_path / "t1")
    vals = sk.resolve_inputs({"dept": "인사팀"}, "Asia/Seoul")
    assert vals["month"].count("-") == 1 and vals["dept"] == "인사팀"
    assert "인사팀 자료" in sk.render(vals, "Asia/Seoul")
    with pytest.raises(SkillError):
        sk.resolve_inputs({"dept": "없는팀"}, "Asia/Seoul")
    with pytest.raises(SkillError):
        parse_skill("no front matter", tmp_path)
    with pytest.raises(SkillError):
        parse_skill("---\nname: x\n---\nbody", tmp_path)  # description 없음

    store = SkillStore(tmp_path / "skills")
    store.save("t1", SKILL)
    store.save("t1", SKILL.replace("테스트 스킬", "수정본"))
    assert store.get("t1").description == "수정본"
    assert len(store.history("t1")) == 1
    with pytest.raises(SkillError):
        store.save("other", SKILL)  # 폴더명 ≠ name
    (tmp_path / "skills" / "broken").mkdir()
    (tmp_path / "skills" / "broken" / "SKILL.md").write_text("깨진 파일", encoding="utf-8")
    skills, errors = store.list()
    assert [s.name for s in skills] == ["t1"] and "broken" in errors
    store.delete("t1")
    assert store.list()[0] == []


def test_bundled_skills_are_valid() -> None:
    skills, errors = SkillStore(ROOT / "skills").list()
    assert not errors and {"monthly_sales_report", "notice_digest"} <= {s.name for s in skills}


async def test_skill_run_with_ask_user_and_files(tmp_path: Path) -> None:
    """스킬 실행: 지침이 시스템 프롬프트에 들어가고, ask_user 답변이 tool 결과로 돌아오며, 다운로드 파일이 감지된다."""
    cfg = load_config(write_config(tmp_path))
    (cfg.skills_path / "t1").mkdir(parents=True)
    (cfg.skills_path / "t1" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    llm = ScriptedLLM(
        [
            LLMResponse(None, [ToolCall("a1", "ask_user", json.dumps({"question": "부서?", "options": ["전체", "인사팀"]}))]),
            LLMResponse(None, [ToolCall("a2", "demo__download_sales_excel", json.dumps({"month": "2026-10", "wait": True}))]),
            LLMResponse(None, [ToolCall("a3", "query_table", json.dumps({"path": "downloads/매출현황_2026-10.xlsx", "group_by": ["부서"], "aggregates": [{"column": "금액", "func": "sum"}]}))]),
            LLMResponse("완료"),
        ]
    )
    asked = []

    async def ask(q, options, default):
        asked.append((q, options))
        return "인사팀"

    async with AgentRuntime(cfg, llm=llm) as rt:
        result, path = await rt.run_skill("t1", {"month": "2026-10"}, ask=ask)
    assert result.success and asked == [("부서?", ["전체", "인사팀"])]
    system = llm.calls[0][0]["content"]
    assert "지금 실행할 스킬: t1" in system and "2026-10 의 전체 자료를 정리" in system
    tool_msgs = {m["tool_call_id"]: m["content"] for m in llm.calls[-1] if m["role"] == "tool"}
    assert "사용자 답변: 인사팀" in tool_msgs["a1"]
    assert "[파일 감지" in tool_msgs["a2"] and "정상" in tool_msgs["a2"]
    assert "| 인사팀 |" in tool_msgs["a3"]
    assert any(f["state"] == "complete" for f in result.files)
    assert path is not None and path.parent.name == "skill_t1" and "매출현황_2026-10.xlsx" in path.read_text(encoding="utf-8")
    # servers: [demo] → broken 서버 tool 은 노출 안 됨
    assert not any(t["function"]["name"].startswith("broken__") for t in llm.tools)


async def test_ask_user_without_ui_uses_default(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM([LLMResponse(None, [ToolCall("q", "ask_user", '{"question":"어느 달?","default":"10월"}')]), LLMResponse("ok")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        await rt.agent.run("예약 실행")
    tool = [m for m in llm.calls[-1] if m["role"] == "tool"][0]["content"]
    assert "질문할 수 없는 실행" in tool and "10월" in tool


# ---------------------------------------------------------------------------
# 웹 API: 스킬 저장 → 실행 → SSE 이벤트 → ask_user 답변
# ---------------------------------------------------------------------------


def test_web_skill_flow(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM(
        [
            LLMResponse(None, [ToolCall("a1", "ask_user", '{"question":"계속할까요?","options":["예","아니오"]}')]),
            LLMResponse("스킬 결과입니다"),
            LLMResponse("후속 답변"),
        ]
    )
    rt = AgentRuntime(cfg, llm=llm)
    app = create_app(cfg, runtime=rt, enable_scheduler=False)
    with TestClient(app) as c:
        assert "사내 자료수집 에이전트" in c.get("/").text
        r = c.post("/api/skills", json={"name": "web1", "description": "웹 테스트"})
        assert r.status_code == 200, r.text
        bad = c.put("/api/skills/web1", json={"content": "형식 오류"})
        assert bad.status_code == 400
        assert c.post("/api/skills/validate", json={"content": SKILL}).json()["ok"]
        assert c.put("/api/skills/web1", json={"content": SKILL.replace("name: t1", "name: web1")}).status_code == 200
        detail = c.get("/api/skills/web1").json()
        assert [i["name"] for i in detail["parsed"]["inputs"]] == ["month", "dept"] and detail["history"]

        run = c.post("/api/runs", json={"skill": "web1", "inputs": {"month": "2026-09"}}).json()
        rid = run["id"]
        # 질문이 올 때까지 대기
        for _ in range(100):
            info = c.get(f"/api/runs/{rid}").json()
            if info["status"] == "waiting_input":
                break
            import time

            time.sleep(0.05)
        assert info["question"]["question"] == "계속할까요?"
        assert c.post(f"/api/runs/{rid}/answer", json={"answer": "예"}).status_code == 200
        # SSE 스트림 끝까지 읽기
        with c.stream("GET", f"/api/runs/{rid}/events") as s:
            body = "".join(s.iter_text())
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        types = [e["type"] for e in events]
        assert types[0] == "start" and "ask" in types and "answer" in types and types[-1] == "done"
        done = events[-1]
        assert done["status"] == "completed" and done["output"] == "스킬 결과입니다" and done["saved_to"]

        # 같은 세션으로 후속 지시 → 스킬 지침 유지
        follow = c.post("/api/runs", json={"prompt": "표로 바꿔줘", "session_id": run["session_id"]}).json()
        with c.stream("GET", f"/api/runs/{follow['id']}/events") as s:
            body = "".join(s.iter_text())
        assert "후속 답변" in body
        assert "지금 실행할 스킬: web1" in llm.calls[-1][0]["content"]
        assert any(m["content"] == "표로 바꿔줘" for m in llm.calls[-1] if m["role"] == "user")

        # 파일 업로드 → 상태/분석
        up = c.post("/api/files/upload?name=up.csv", content="부서,금액\n인사팀,10\n".encode("utf-8")).json()
        assert c.get("/api/files/status", params={"path": up["path"]}).json()["state"] == "complete"
        assert "1행 × 2열" in c.get("/api/files/analyze", params={"path": up["display"]}).json()["text"]
        assert c.get("/api/files/status", params={"path": "/etc/passwd"}).status_code == 403
        assert any(f["name"] == "up.csv" for f in c.get("/api/files").json())
        assert c.get("/api/skills/web1/results").json()
