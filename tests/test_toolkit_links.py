"""파일 다운로드 링크와 범용 도구(toolkit) 테스트."""

from __future__ import annotations

import json
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.files import FileWorkspace
from inhouse_agent.interaction import RunContext, current_run
from inhouse_agent.links import LinkError, LinkSigner
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture()
def web():
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            seen.append({"method": "GET", "path": u.path, "query": u.query})
            if u.path == "/page":
                html = "<html><head><title>공지 게시판</title></head><body><nav>메뉴</nav><h1>11월 공지</h1><p>보안 점검 안내</p>" \
                       "<a href='/file/1.xlsx'>첨부</a><a href='https://other.local/x'>외부</a></body></html>"
                self._send(200, "text/html; charset=euc-kr", html.encode("cp949"))
            elif u.path == "/api/items":
                self._send(200, "application/json", json.dumps({"items": [{"id": 1, "name": "가"}], "q": u.query}).encode())
            elif u.path == "/search":
                self._send(200, "application/json", json.dumps({"results": [{"title": "결과1", "url": "http://a/1", "content": "요약"}]}).encode())
            else:
                self._send(404, "text/plain", b"no")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": "POST", "path": self.path, "body": self.rfile.read(n).decode()})
            self._send(201, "application/json", b'{"ok": true}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def _cfg(tmp_path: Path, extra: str = "") -> Path:
    p = write_config(tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + "\n" + extra, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 링크
# ---------------------------------------------------------------------------


def test_link_signer(tmp_path: Path) -> None:
    ws = FileWorkspace(tmp_path / "ws")
    f = ws.files / "보고서 1.xlsx"
    f.write_bytes(b"x")
    s = LinkSigner(ws, "http://agent:8080", 1)
    url = s.url(f)
    assert url.startswith("http://agent:8080/d/") and url.endswith(urllib.parse.quote("보고서 1.xlsx"))
    token = url.split("/d/")[1].split("/")[0]
    assert s.verify(token) == f.resolve()
    with pytest.raises(LinkError):
        s.verify(token[:-2] + ("A" if token[-1] != "A" else "B") + token[-1])  # 위조
    with pytest.raises(LinkError):
        LinkSigner(ws).verify(token.replace(".", "x.", 1))
    expired = s.sign(f, expire_hours=-1)
    with pytest.raises(LinkError, match="만료"):
        s.verify(expired)
    with pytest.raises(PermissionError):
        s.sign("/etc/passwd")
    # 같은 workspace 의 다른 인스턴스(재시작)도 같은 키 사용
    assert LinkSigner(ws).verify(token) == f.resolve()
    assert LinkSigner(ws).url(f).startswith("/d/")  # public_url 없으면 상대 링크


def test_web_links_scan_and_signed_download(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    monkeypatch.setenv("AGENT_API_TOKEN", "secret-token")
    cfg = load_config(_cfg(tmp_path, "server:\n  public_url: http://agent.local:8080\n"))
    rt = AgentRuntime(cfg, llm=ScriptedLLM([]))
    (rt.workspace.files / "reports").mkdir(parents=True)
    f = rt.workspace.files / "reports" / "10월 매출 보고서.xlsx"
    f.write_bytes(b"PK-data")
    (rt.workspace.files / "reports" / "chart.png").write_bytes(b"\x89PNG")
    app = create_app(cfg, runtime=rt, enable_scheduler=False)
    H = {"Authorization": "Bearer secret-token"}
    with TestClient(app) as c:
        assert c.post("/api/files/scan", json={"text": "x"}).status_code == 401
        text = f"엑셀은 {f} 에 저장했고, 차트는 files/reports/chart.png 입니다. 없는파일 /tmp/none.xlsx"
        found = c.post("/api/files/scan", json={"text": text}, headers=H).json()
        matches = {x["match"]: x for x in found}
        assert str(f) in matches and "files/reports/chart.png" in matches  # 공백 있는 파일명도 인식
        assert not any("none.xlsx" in m for m in matches)
        it = matches[str(f)]
        assert it["share_url"].startswith("http://agent.local:8080/d/")
        # 서명 링크는 API 토큰 없이 열림
        r = c.get(it["href"])
        assert r.status_code == 200 and r.content == b"PK-data"
        assert "attachment" in r.headers["content-disposition"]
        assert c.get(it["href"].replace("/d/", "/d/x")).status_code == 403
        # 폴더 → zip
        links = c.post("/api/files/links", json={"paths": ["files/reports", "/etc/passwd"]}, headers=H).json()
        assert links[0]["ok"] and links[0]["is_dir"] and not links[1]["ok"]
        z = c.get(links[0]["href"])
        assert z.status_code == 200
        zp = tmp_path / "got.zip"
        zp.write_bytes(z.content)
        assert sorted(zipfile.ZipFile(zp).namelist()) == ["reports/10월 매출 보고서.xlsx", "reports/chart.png"]
        assert not list(rt.workspace.root.glob("tmp*.zip"))  # 임시 zip 정리
        assert c.get("/api/files/download", params={"path": "files/reports", "token": "secret-token"}).status_code == 200


# ---------------------------------------------------------------------------
# 범용 도구
# ---------------------------------------------------------------------------


async def test_web_and_http_tools(tmp_path: Path, web) -> None:
    base, seen = web
    cfg = load_config(_cfg(tmp_path, f"web_search:\n  provider: searxng\n  url: {base}\n"))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        out = await rt.hub.call("web_fetch", {"url": f"{base}/page"})
        assert "제목: 공지 게시판" in out.text and "보안 점검 안내" in out.text and "메뉴" not in out.text
        assert f"{base}/file/1.xlsx" in out.text  # 상대 링크를 절대 주소로
        out = await rt.hub.call("http_request", {"url": f"{base}/api/items", "params": {"q": "한글"}})
        assert "HTTP 200" in out.text and '"name": "가"' in out.text and "q=%ED%95%9C%EA%B8%80" in out.text
        # POST: 무인 실행 → 거부, 승인 → 실행
        out = await rt.hub.call("http_request", {"url": f"{base}/api/items", "method": "POST", "json": {"a": 1}})
        assert "요청하지 않았습니다" in out.text and not any(s["method"] == "POST" for s in seen)

        async def allow(q, o, d):
            return "승인"

        tok = current_run.set(RunContext(ask=allow, interactive=True))
        out = await rt.hub.call("http_request", {"url": f"{base}/api/items", "method": "POST", "json": {"a": 1}})
        current_run.reset(tok)
        assert "HTTP 201" in out.text and seen[-1]["body"] == '{"a": 1}'
        out = await rt.hub.call("web_search", {"query": "보안"})
        assert "결과1" in out.text and "http://a/1" in out.text
        out = await rt.hub.call("web_fetch", {"url": "http://169.254.169.254/x"})
        assert out.is_error and "링크로컬" in out.text


async def test_data_and_report_tools(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        d = rt.workspace.downloads
        (d / "sales.csv").write_text("사번,부서,금액\n00123,인사팀,\"1,000\"\n00124,IT팀,2500\n00125,인사팀,500\n", encoding="utf-8")
        (d / "emp.csv").write_text("사번,이름\n00123,김가\n00124,이나\n", encoding="utf-8")
        out = await rt.hub.call("sql_query", {
            "sql": 'SELECT s."부서", SUM(s."금액") AS 합계, COUNT(e."이름") AS 명 FROM s LEFT JOIN e ON s."사번" = e."사번" GROUP BY 1 ORDER BY 2 DESC',
            "tables": [{"alias": "s", "path": "downloads/sales.csv"}, {"alias": "e", "path": "downloads/emp.csv"}],
            "save_as": "reports/부서합계.xlsx",
        })
        assert not out.is_error, out.text
        assert "| IT팀 | 2,500 | 1 |" in out.text and "| 인사팀 | 1,500 | 1 |" in out.text  # '00123' 코드 유지되어 조인됨
        assert out.files and out.files[0]["state"] == "complete" and "/d/" in out.text
        assert (await rt.hub.call("sql_query", {"sql": "DROP TABLE s", "tables": []})).is_error

        # 비교
        (d / "old.csv").write_text("번호,상태\n1,대기\n2,완료\n3,대기\n", encoding="utf-8")
        (d / "new.csv").write_text("번호,상태\n1,완료\n3,대기\n4,대기\n", encoding="utf-8")
        out = await rt.hub.call("compare_tables", {"old_path": "downloads/old.csv", "new_path": "downloads/new.csv", "key_columns": ["번호"]})
        assert "추가 1 / 삭제 1 / 변경 1" in out.text and "상태: 대기 → 완료" in out.text

        # 엑셀 (서식 확인)
        out = await rt.hub.call("write_excel", {
            "path": "reports/월보", "title": "10월 보고",
            "sheets": [{"name": "요약", "columns": ["부서", "금액"], "rows": [["인사팀", "1,500"], ["IT팀", 2500]]}],
            "from_files": [{"path": "downloads/sales.csv", "name": "원본"}],
        })
        assert not out.is_error, out.text
        import openpyxl

        wb = openpyxl.load_workbook(rt.workspace.files / "reports" / "월보.xlsx")
        ws = wb["요약"]
        assert wb.sheetnames == ["요약", "원본"] and ws["A1"].value == "10월 보고" and ws["A3"].font.bold
        assert ws["B4"].value == 1500 and ws["B4"].number_format == "#,##0" and ws.freeze_panes == "A4"
        assert wb["원본"]["A4"].value == "00123"  # 코드값은 문자 유지

        # 차트
        pytest.importorskip("matplotlib")
        out = await rt.hub.call("make_chart", {"path": "reports/chart", "chart_type": "bar", "title": "Sales", "labels": ["A", "B"], "series": [{"name": "2026", "values": [1, 2]}]})
        assert not out.is_error and (rt.workspace.files / "reports" / "chart.png").read_bytes()[:4] == b"\x89PNG"

        # zip + 링크
        out = await rt.hub.call("create_zip", {"path": "보고서묶음", "paths": ["files/reports"]})
        names = zipfile.ZipFile(rt.workspace.files / "보고서묶음.zip").namelist()
        assert "reports/월보.xlsx" in names and "reports/chart.png" in names
        out = await rt.hub.call("get_download_link", {"path": "files/보고서묶음.zip", "expire_hours": 2})
        assert "/d/" in out.text and "2시간" in out.text and "public_url" in out.text

        # 파일 내용 검색 (엑셀 안의 값까지)
        out = await rt.hub.call("search_files", {"query": "00124"})
        assert "downloads/sales.csv" in out.text and "files/reports/월보.xlsx" in out.text


async def test_memory_plan_and_notify(tmp_path: Path, web, monkeypatch) -> None:
    base, seen = web
    cfg = load_config(_cfg(tmp_path, f"""server:
  public_url: http://agent.local
notify:
  email:
    smtp_host: smtp.local
    from: agent@company.com
    allowed_recipients: ["*@company.com"]
  webhooks:
    영업팀: {base}/hook
"""))
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append({"host": host})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, msg):
            sent[-1]["msg"] = msg

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    llm = ScriptedLLM([
        LLMResponse(None, [ToolCall("p1", "update_plan", json.dumps({"steps": [{"step": "수집", "status": "done"}, {"step": "보고", "status": "in_progress"}]}))]),
        LLMResponse(None, [ToolCall("m1", "remember", json.dumps({"key": "ERP 조회 요령", "content": "월말은 다음날 9시 이후 조회"}))]),
        LLMResponse("끝"),
        LLMResponse("두번째"),
    ])
    events: list = []
    async with AgentRuntime(cfg, llm=llm) as rt:
        await rt.agent.run("작업", on_event=lambda k, d: events.append((k, d)))
        plan = [d for k, d in events if k == "plan"]
        assert plan and plan[0]["steps"][1] == {"step": "보고", "status": "in_progress"}
        await rt.agent.run("다음 작업")
        assert "저장된 기억 1개" in llm.calls[-1][0]["content"] and "ERP 조회 요령" in llm.calls[-1][0]["content"]
        assert "9시" in (await rt.hub.call("recall", {"query": "erp"})).text
        assert "삭제함" in (await rt.hub.call("forget", {"key": "ERP 조회 요령"})).text

        f = rt.workspace.files / "결과.xlsx"
        f.write_bytes(b"PK")
        out = await rt.hub.call("send_email", {"to": ["kim@gmail.com"], "subject": "x", "body": "y"})
        assert "허용되지 않은 수신자" in out.text and not sent
        out = await rt.hub.call("send_email", {"to": ["kim@company.com"], "subject": "10월 보고", "body": "첨부합니다", "attachments": ["files/결과.xlsx"]})
        assert "발송 완료" in out.text, out.text
        msg = sent[-1]["msg"]
        assert msg["To"] == "kim@company.com" and "http://agent.local/d/" in msg.get_body().get_content()
        assert [a.get_filename() for a in msg.iter_attachments()] == ["결과.xlsx"]

        out = await rt.hub.call("send_message", {"channel": "없는채널", "text": "x"})
        assert "등록되지 않은 채널" in out.text
        out = await rt.hub.call("send_message", {"channel": "영업팀", "text": "보고서 완료", "files": ["files/결과.xlsx"]})
        assert "전송 완료" in out.text
        body = json.loads(seen[-1]["body"])
        assert "보고서 완료" in body["text"] and "http://agent.local/d/" in body["text"]
        # 대화형에서 거부하면 보내지 않음
        n = len(sent)

        async def deny(q, o, d):
            return "거부"

        tok = current_run.set(RunContext(ask=deny, interactive=True))
        out = await rt.hub.call("send_email", {"to": ["kim@company.com"], "subject": "x", "body": "y"})
        current_run.reset(tok)
        assert "보내지 않았습니다" in out.text and len(sent) == n
        # 웹훅 결과 링크 (public_url 설정 시)
        from inhouse_agent.agent import AgentResult

        r = AgentResult("ok", [], files=[{"path": str(f), "state": "complete"}])
        assert rt.file_links(r)[0][0] == "결과.xlsx" and rt.file_links(r)[0][1].startswith("http://agent.local/d/")
