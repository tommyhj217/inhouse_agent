"""다운로드 URL 처리와 동적 도구(create_tool / run_python) 테스트."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.downloader import DownloadConfig, DownloadError, Downloader, filename_from_headers, find_urls
from inhouse_agent.dynamic_tools import DynamicToolsConfig, check_code
from inhouse_agent.files import FileWorkspace
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture()
def file_server():
    hits: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            hits.append({"path": u.path, "cookie": self.headers.get("Cookie"), "auth": self.headers.get("Authorization")})
            if u.path == "/kr":
                body = b"a,b\n1,2\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote("월간 보고서.csv"))
            elif u.path == "/legacy":
                body = b"x\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                # 일부 사내 서버: UTF-8 바이트를 그대로 헤더에 넣음 → 클라이언트에선 latin-1 로 깨져 보임
                self.send_header("Content-Disposition", 'attachment; filename="' + "결재목록.txt".encode().decode("latin-1") + '"')
            elif u.path == "/secure":
                if "SID=ok" in (self.headers.get("Cookie") or ""):
                    body = b"PK\x05\x06" + b"\x00" * 18  # 빈 zip
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition", 'attachment; filename="data.zip"')
                else:
                    body = "<html><body>로그인 하세요</body></html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
            elif u.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/kr")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif u.path == "/big":
                body = b"0" * (2 * 1024 * 1024)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
            else:
                body = b"nope"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    srv.shutdown()


# ---------------------------------------------------------------------------
# 다운로드
# ---------------------------------------------------------------------------


def test_filename_parsing() -> None:
    assert filename_from_headers("attachment; filename*=UTF-8''%EB%A7%A4%EC%B6%9C.xlsx", "http://x/d", None) == "매출.xlsx"
    assert filename_from_headers('attachment; filename="' + "보고서.pdf".encode().decode("latin-1") + '"', "http://x/d", None) == "보고서.pdf"
    assert filename_from_headers('attachment; filename="' + "보고서.pdf".encode("cp949").decode("latin-1") + '"', "http://x/d", None) == "보고서.pdf"
    assert filename_from_headers('attachment; filename="%EC%97%91%EC%85%80.xls"', "http://x/d", None) == "엑셀.xls"
    assert filename_from_headers(None, "http://x/down?fileName=list.csv&id=3", None) == "list.csv"
    assert filename_from_headers(None, "http://x/getFile", "application/pdf") == "getFile.pdf"
    assert filename_from_headers('attachment; filename="../../evil.txt"', "http://x/", None) == "_.._evil.txt"


def test_find_urls() -> None:
    text = "링크: http://gw.local/file/download?id=1, 홈페이지 https://gw.local/main 그리고 https://erp.local/a/report.xlsx."
    assert find_urls(text) == ["http://gw.local/file/download?id=1", "https://erp.local/a/report.xlsx"]
    assert len(find_urls(text, only_downloadish=False)) == 3


def test_downloader_cases(tmp_path: Path, file_server) -> None:
    base, hits = file_server
    ws = FileWorkspace(tmp_path / "ws")
    dl = Downloader(DownloadConfig(retries=0, max_mb=1, sites=[]), ws)
    r = dl.download(f"{base}/kr")
    assert Path(r["path"]).name == "월간 보고서.csv" and Path(r["path"]).read_bytes() == b"a,b\n1,2\n"
    assert Path(dl.download(f"{base}/legacy")["path"]).name == "결재목록.txt"
    r = dl.download(f"{base}/redirect")
    assert r["redirected"] and Path(r["path"]).name.startswith("월간 보고서")
    with pytest.raises(DownloadError, match="404"):
        dl.download(f"{base}/missing")
    with pytest.raises(DownloadError, match="1MB"):
        dl.download(f"{base}/big")
    assert not list(ws.downloads.glob("*.part"))  # 실패 시 임시파일 정리
    with pytest.raises(DownloadError, match="http/https"):
        dl.download("file:///etc/passwd")
    strict = Downloader(DownloadConfig(allowed_hosts=["*.company.com"], retries=0), ws)
    with pytest.raises(DownloadError, match="허용되지 않은"):
        strict.download(f"{base}/kr")
    with pytest.raises(DownloadError, match="링크로컬"):
        Downloader(DownloadConfig(retries=0), ws).download("http://169.254.169.254/latest/meta-data")
    # 사이트 규칙으로 헤더/쿠키 자동 첨부
    from inhouse_agent.downloader import SiteRule

    dl2 = Downloader(DownloadConfig(retries=0, sites=[SiteRule("127.0.0.1", {"Authorization": "Bearer T"}, {"SID": "ok"})]), ws)
    assert Path(dl2.download(f"{base}/secure")["path"]).name == "data.zip"
    assert hits[-1]["auth"] == "Bearer T" and "SID=ok" in hits[-1]["cookie"]


async def test_download_url_tool_and_link_detection(tmp_path: Path, file_server) -> None:
    base, _ = file_server
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        out = await rt.hub.call("download_url", {"url": f"{base}/secure"})
        assert out.files[0]["state"] == "suspicious" and "로그인" in out.text and "cookies" in out.text
        out = await rt.hub.call("download_url", {"url": f"{base}/secure", "cookies": "SID=ok; Path=/"})
        assert out.files[0]["state"] == "complete" and out.files[0]["name"] == "data.zip"
        out = await rt.hub.call("download_url", {"url": "/kr", "base_url": base})
        assert "월간 보고서.csv" in out.text
        out = await rt.hub.call("download_url", {"url": f"{base}/nope"})
        assert out.is_error and "404" in out.text
        # MCP 결과의 링크 감지
        out = await rt.hub.call("demo__get_sales_download_link", {"month": "2026-10"})
        assert "[다운로드 링크 감지]" in out.text and "download_url" in out.text
        # auto_download 켜면 자동으로 받아 상태 점검
        rt.downloader.cfg.auto_download = True
        out = await rt.hub.call("demo__get_sales_download_link", {"month": "2026-11"})
        assert any(f["name"] == "매출현황_2026-11.xlsx" and f["state"] == "complete" for f in out.files), out.text


# ---------------------------------------------------------------------------
# 동적 도구
# ---------------------------------------------------------------------------

TOOL_CODE = '''
def run(args, ctx):
    out = []
    for m in args["months"]:
        rows = ctx.call_tool_data("demo__get_daily_sales", {"date_from": m + "-01", "date_to": m + "-03"})
        out.append({"month": m, "total": sum(r["sales"] for r in rows)})
    print("done", len(out))
    return out
'''


def test_check_code_policy() -> None:
    cfg = DynamicToolsConfig()
    assert check_code("def run(args, ctx):\n    return 1\n", cfg, require_run=True) == []
    probs = check_code("import subprocess\nimport os\ndef run(a, c):\n    os.system('x')\n    eval('1')\n", cfg, require_run=True)
    assert any("subprocess" in p for p in probs) and any("os.system" in p for p in probs) and any("eval" in p for p in probs)
    assert check_code("def run(a):\n    pass", cfg, require_run=True)  # 인자 2개 필요
    assert "문법 오류" in check_code("def run(:", cfg, require_run=True)[0]
    assert check_code("x = 1", cfg, require_run=True)  # run 없음


def _dyn_config(tmp_path: Path, mode: str) -> Path:
    p = write_config(tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + f"\ndynamic_tools:\n  mode: {mode}\n  timeout: 30\n", encoding="utf-8")
    return p


def _create_args(name: str = "sales_sum", code: str = TOOL_CODE, test: dict | None = None) -> dict:
    return {
        "name": name,
        "description": "여러 달의 1~3일 매출 합계",
        "parameters": {"type": "object", "properties": {"months": {"type": "array", "items": {"type": "string"}}}, "required": ["months"]},
        "code": code,
        "test_args": test if test is not None else {"months": ["2026-10"]},
    }


async def test_agent_creates_and_uses_tool_in_same_run(tmp_path: Path) -> None:
    """LLM 이 create_tool → 같은 실행 안에서 custom__ 도구 호출 (tools 목록 즉시 갱신)."""
    cfg = load_config(_dyn_config(tmp_path, "approve"))
    llm = ScriptedLLM(
        [
            LLMResponse(None, [ToolCall("c1", "create_tool", json.dumps(_create_args()))]),
            LLMResponse(None, [ToolCall("c2", "custom__sales_sum", json.dumps({"months": ["2026-08", "2026-09"]}))]),
            LLMResponse("완료"),
        ]
    )
    asked: list[str] = []

    async def ask(q, options, default):
        asked.append(q)
        return "승인"

    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("8~9월 초 매출 합계", ask=ask)
    assert r.success
    assert asked and "sales_sum" in asked[0] and "--- 코드 ---" in asked[0]
    tool_msgs = {m["tool_call_id"]: m["content"] for m in llm.calls[-1] if m["role"] == "tool"}
    assert "도구 등록 완료" in tool_msgs["c1"] and '"month": "2026-10"' in tool_msgs["c1"]
    assert '"month": "2026-08"' in tool_msgs["c2"] and "내부 tool 호출 2회" in tool_msgs["c2"]
    # 두 번째 LLM 호출부터 custom__sales_sum 이 tools 에 포함됨 (실행 중 즉시 반영)
    assert any(t["function"]["name"] == "custom__sales_sum" for t in llm.tools)
    saved = cfg.resolve_path("tools") / "sales_sum.py"
    assert saved.exists() and "created_by': 'agent'" in saved.read_text(encoding="utf-8")

    # 재시작해도 저장된 도구가 다시 등록되고, 승인 없이(예약작업) 바로 사용 가능
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt2:
        out = await rt2.hub.call("custom__sales_sum", {"months": ["2026-07"]})
        assert not out.is_error and "2026-07" in out.text


async def test_create_tool_denied_failed_and_noninteractive(tmp_path: Path) -> None:
    cfg = load_config(_dyn_config(tmp_path, "approve"))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        # 사용자가 없는 실행(예약작업) → 승인 불가로 거부
        out = await rt.hub.call("create_tool", _create_args())
        assert out.is_error and "예약작업" in out.text
        # 거부
        from inhouse_agent.interaction import RunContext, current_run

        async def deny(q, o, d):
            return "거부"

        async def allow(q, o, d):
            return "승인"

        tok = current_run.set(RunContext(ask=deny, interactive=True))
        out = await rt.hub.call("create_tool", _create_args())
        current_run.reset(tok)
        assert out.is_error and "거부" in out.text
        tok = current_run.set(RunContext(ask=allow, interactive=True))
        # 테스트 실패 → 등록 안 됨, traceback 전달
        bad = "def run(args, ctx):\n    return 1 / 0\n"
        out = await rt.hub.call("create_tool", _create_args("bad_tool", bad, {}))
        assert out.is_error and "ZeroDivisionError" in out.text and "traceback" in out.text
        assert not (rt.dynamic.root / "bad_tool.py").exists()
        # 정책 위반은 승인 묻기 전에 거절
        out = await rt.hub.call("create_tool", _create_args("evil", "import subprocess\ndef run(a, c):\n    return 1\n"))
        assert out.is_error and "subprocess" in out.text
        # 도구 안에서 create_tool 재귀 호출 금지
        rec = "def run(args, ctx):\n    return ctx.call_tool('create_tool', {})\n"
        out = await rt.hub.call("create_tool", _create_args("recur", rec, {}))
        assert "호출할 수 없습니다" in out.text
        # 이미 있는 이름은 overwrite 없이 거절
        out = await rt.hub.call("create_tool", _create_args("recur", rec, {}))
        assert out.is_error and "이미 있습니다" in out.text
        current_run.reset(tok)


async def test_run_python_and_mode_off(tmp_path: Path) -> None:
    cfg = load_config(_dyn_config(tmp_path, "auto"))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        (rt.workspace.downloads / "n.csv").write_text("v\n1\n2\n3\n", encoding="utf-8")
        code = (
            "from inhouse_agent import analyzers\n"
            "t = analyzers.load_tables(ctx.path('downloads/n.csv'))[0]\n"
            "print('rows', len(t.rows))\n"
            "result = {'sum': int(sum(analyzers.to_number(r[0]) for r in t.rows)), 'k': args['k']}\n"
        )
        out = await rt.hub.call("run_python", {"code": code, "purpose": "합계", "args": {"k": "x"}})
        assert not out.is_error and '"sum": 6' in out.text and "rows 3" in out.text
        out = await rt.hub.call("run_python", {"code": "import os\nprint(sorted(k for k in os.environ if 'AZURE' in k or 'KEY' in k))", "purpose": "env"})
        assert "[]" in out.text  # API 키 등은 자식 프로세스에 전달되지 않음
        out = await rt.hub.call("run_python", {"code": "while True:\n    pass", "purpose": "loop", "timeout_sec": 1})
        assert out.is_error and "제한 시간" in out.text

    (tmp_path / "off").mkdir()
    cfg_off = load_config(_dyn_config(tmp_path / "off", "off"))  # YAML 의 off(=False) 도 꺼짐으로 처리
    async with AgentRuntime(cfg_off, llm=ScriptedLLM([])) as rt:
        names = {t["function"]["name"] for t in rt.hub.openai_tools()}
        assert "create_tool" not in names and "run_python" not in names and "download_url" in names


def test_web_ctools_and_fetch(tmp_path: Path, file_server) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    base, _ = file_server
    cfg = load_config(_dyn_config(tmp_path, "approve"))
    app = create_app(cfg, runtime=AgentRuntime(cfg, llm=ScriptedLLM([])), enable_scheduler=False)
    with TestClient(app) as c:
        d = c.get("/api/ctools").json()
        assert d["mode"] == "approve" and d["tools"] == [] and "def run" in d["template"]
        code = "def run(args, ctx):\n    return {'double': args['n'] * 2}\n"
        assert c.post("/api/ctools/dbl/check", json={"code": "import ctypes\n" + code}).json()["ok"] is False
        r = c.post("/api/ctools/dbl/test", json={"args": {"n": 4}, "code": code}).json()
        assert r["ok"] and r["value"] == {"double": 8}
        assert c.put("/api/ctools/Bad-Name", json={"description": "x", "code": code}).status_code == 400
        s = c.put("/api/ctools/dbl", json={"description": "두 배", "parameters": {"type": "object", "properties": {"n": {"type": "number"}}}, "code": code})
        assert s.status_code == 200 and s.json()["created_by"] == "user"
        assert any(t["name"] == "custom__dbl" for t in c.get("/api/tools").json())
        assert c.post("/api/ctools/dbl/test", json={"args": {"n": 1}}).json()["value"] == {"double": 2}
        assert c.get("/api/ctools/dbl").json()["code"].strip() == code.strip()
        assert c.delete("/api/ctools/dbl").json()["ok"]
        assert not any(t["name"] == "custom__dbl" for t in c.get("/api/tools").json())
        f = c.post("/api/files/fetch", json={"url": f"{base}/kr"}).json()
        assert not f["is_error"] and f["files"][0]["state"] == "complete"
