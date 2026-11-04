"""원격 MCP 서버(다른 PC) 파일 가져오기 테스트.

샘플 MCP 서버를 별도 프로세스(HTTP)로 띄우고, 파일은 에이전트가 볼 수 없는 '원격 디스크' 폴더에 저장하게 해서
path_map(공유폴더/URL), fetch_tool, MCP resource, localhost 링크 변환을 실제로 확인합니다.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_agent import ScriptedLLM

from inhouse_agent.config import load_config
from inhouse_agent.remote_files import _b64_from, find_remote_paths, is_remote, map_path, rewrite_localhost
from inhouse_agent.runtime import AgentRuntime

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 단위
# ---------------------------------------------------------------------------


def test_map_path_variants() -> None:
    pm = {"C:\\mcp\\downloads": "\\\\fileserver\\share\\mcp", "/srv/mcp/out": "http://10.0.0.5:9000/out", "C:/mcp": "D:/other"}
    assert map_path("C:\\mcp\\downloads\\10월 보고서.xlsx", pm).replace("\\", "/") == "//fileserver/share/mcp/10월 보고서.xlsx"
    assert map_path("c:/MCP/downloads/a.csv", pm).replace("\\", "/") == "//fileserver/share/mcp/a.csv"  # 드라이브 대소문자 무시
    assert map_path("C:\\mcp\\x\\b.csv", pm).replace("\\", "/") == "D:/other/x/b.csv"  # 가장 긴 접두어 우선
    assert map_path("/srv/mcp/out/월간 보고서.pdf", pm) == "http://10.0.0.5:9000/out/%EC%9B%94%EA%B0%84%20%EB%B3%B4%EA%B3%A0%EC%84%9C.pdf"
    assert map_path("file:///srv/mcp/out/a.csv", pm) == "http://10.0.0.5:9000/out/a.csv"
    assert map_path("/srv/mcp/output/a.csv", pm) is None  # 접두어가 폴더 경계에서 맞아야 함


def test_find_remote_paths_and_rewrite() -> None:
    text = (
        "저장: C:\\mcp\\downloads\\10월 매출 보고서.xlsx 에 저장\n"
        "리눅스: /srv/mcp/exports/a.csv, 링크 http://host/files/b.xlsx 는 제외\n"
        "UNC: \\\\nas01\\share\\x.pdf\nURI file:///srv/mcp/c.txt\n그냥 /data 에 x.csv 저장"
    )
    found = find_remote_paths(text)
    assert found == ["C:\\mcp\\downloads\\10월 매출 보고서.xlsx", "/srv/mcp/exports/a.csv", "\\\\nas01\\share\\x.pdf", "file:///srv/mcp/c.txt"]
    t, ch = rewrite_localhost("받기 http://localhost:9000/f?id=1 와 http://127.0.0.1/a 그리고 http://erp.local/x", "http://10.1.2.3:8001/mcp")
    assert "http://10.1.2.3:9000/f?id=1" in t and "http://10.1.2.3/a" in t and "http://erp.local/x" in t and len(ch) == 2
    assert rewrite_localhost("http://localhost:1/x", "http://127.0.0.1:8001/mcp")[1] == []  # 같은 PC 면 그대로


def test_is_remote_and_b64() -> None:
    from inhouse_agent.config import MCPServerConfig

    assert not is_remote(MCPServerConfig.from_dict("a", {"command": "python"}))
    assert not is_remote(MCPServerConfig.from_dict("a", {"url": "http://127.0.0.1:8001/mcp"}))
    assert is_remote(MCPServerConfig.from_dict("a", {"url": "http://10.0.0.5:8001/mcp"}))
    assert is_remote(MCPServerConfig.from_dict("a", {"url": "http://erp-mcp.company.com/mcp"}))
    assert is_remote(MCPServerConfig.from_dict("a", {"url": "http://127.0.0.1:8001/mcp", "remote": True}))
    assert not is_remote(MCPServerConfig.from_dict("a", {"url": "http://10.0.0.5/mcp", "remote": False}))
    assert _b64_from({"base64": "aGVsbG8gd29ybGQ="}) == b"hello world"
    assert _b64_from({"filename": "x"}) is None


# ---------------------------------------------------------------------------
# 통합 (원격 PC 흉내)
# ---------------------------------------------------------------------------


def _lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def remote_server(tmp_path_factory):
    disk = tmp_path_factory.mktemp("remote_pc_disk")
    ip = _lan_ip()
    port = _free_port()
    env = dict(os.environ, REMOTE_DIR=str(disk), SAMPLE_HOST="0.0.0.0" if ip else "127.0.0.1")
    proc = subprocess.Popen([sys.executable, str(ROOT / "examples" / "sample_mcp_server.py"), "--http", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    # 원격 PC 의 디스크를 URL 로 내보내는 정적 파일 서버 (path_map URL 대상)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(disk)))
    srv.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": port, "disk": disk, "ip": ip, "static": f"http://127.0.0.1:{srv.server_address[1]}"}
    srv.shutdown()
    proc.terminate()
    proc.wait(10)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


def _config(tmp_path: Path, rs: dict, shared: Path) -> Path:
    url = f"http://127.0.0.1:{rs['port']}/mcp"
    disk = str(rs["disk"]).replace("\\", "/")
    servers = {
        "erp_fetch": {"url": url, "remote": True, "remote_files": {"fetch_tool": "read_server_file"}},
        "erp_share": {"url": url, "remote": True, "remote_files": {"path_map": {disk: str(shared)}, "use_resources": False}},
        "erp_url": {"url": url, "remote": True, "remote_files": {"path_map": {disk: rs["static"]}, "use_resources": False}},
        "erp_res": {"url": url, "remote": True},
        "erp_none": {"url": url, "remote": True, "remote_files": {"use_resources": False}},
    }
    if rs["ip"]:
        servers["erp_lan"] = {"url": f"http://{rs['ip']}:{rs['port']}/mcp", "remote_files": {"use_resources": False}}
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(
        json.dumps({"llm": {"endpoint": "https://x.openai.azure.com", "deployment": "d", "api_key": "k"}, "mcp_servers": servers}, ensure_ascii=False),
        encoding="utf-8",
    )
    return d / "config.yaml"


async def test_remote_file_retrieval(tmp_path: Path, remote_server) -> None:
    rs = remote_server
    shared = tmp_path / "shared_mount"  # 에이전트 PC 에 마운트된 원격 공유폴더라고 가정
    cfg = load_config(_config(tmp_path, rs, shared))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        assert all(v is None for v in rt.start_results.values()), rt.start_results
        assert "fetch_remote_file" in {t["function"]["name"] for t in rt.hub.openai_tools()}

        # 1) fetch_tool: 원격 MCP 가 파일 내용을 돌려줌
        out = await rt.hub.call("erp_fetch__export_report_on_server", {"month": "2026-10"})
        assert "[원격 MCP 파일 처리]" in out.text and "read_server_file" in out.text, out.text
        f = [x for x in out.files if x["name"] == "원격 매출 보고서_2026-10.xlsx"]
        assert f and f[0]["state"] == "complete" and "/downloads/erp_fetch/" in f[0]["path"].replace("\\", "/")
        assert "wait_for_download" not in out.text  # 원격인데 로컬 폴더를 기다리라고 하지 않음

        # 2) path_map → 공유폴더 (원격 디스크 내용이 공유폴더에 보인다고 가정해 복사)
        out = await rt.hub.call("erp_share__export_report_on_server", {"month": "2026-09"})
        assert "공유폴더에 아직 없음" in out.text  # 아직 동기화 전
        import shutil

        shutil.copytree(rs["disk"], shared, dirs_exist_ok=True)
        assert shared.resolve() in rt.workspace.watch_dirs  # 공유폴더는 감시 폴더에 자동 포함
        out = await rt.hub.call("erp_share__export_report_on_server", {"month": "2026-09"})
        assert "공유폴더 경로로 변환" in out.text and any(x["state"] == "complete" for x in out.files), out.text

        # 3) path_map → URL
        out = await rt.hub.call("erp_url__export_report_on_server", {"month": "2026-08"})
        assert "path_map URL 로 받음" in out.text and any(x["name"] == "원격 매출 보고서_2026-08.xlsx" for x in out.files), out.text

        # 4) MCP resource (file:///srv/mcp/exports/notice_list.csv)
        out = await rt.hub.call("erp_res__export_csv_as_resource", {})
        assert "MCP resource 로 받음" in out.text, out.text
        csv_path = Path([x for x in out.files if x["name"] == "notice_list.csv"][0]["path"])
        assert "보안 점검 안내" in csv_path.read_text(encoding="utf-8")

        # 5) 설정이 없으면 이유와 설정 방법을 알려줌
        out = await rt.hub.call("erp_none__export_report_on_server", {"month": "2026-07"})
        assert "가져오지 못함" in out.text and "remote_files" in out.text and not out.files

        # 6) 명시적 fetch_remote_file (대기 포함)
        later = rs["disk"] / "exports" / "later.csv"
        threading.Timer(2.0, lambda: later.write_text("a,b\n1,2\n", encoding="utf-8")).start()
        out = await rt.hub.call("fetch_remote_file", {"server": "erp_fetch", "path": str(later), "wait_sec": 20})
        assert not out.is_error and out.files[0]["state"] == "complete", out.text
        out = await rt.hub.call("fetch_remote_file", {"server": "erp_fetch", "path": str(rs["disk"] / "none.csv")})
        assert out.is_error and "1회 시도" in out.text


async def test_remote_localhost_link_rewrite(tmp_path: Path, remote_server) -> None:
    rs = remote_server
    if not rs["ip"]:
        pytest.skip("LAN IP 가 없어 원격 접속을 흉내낼 수 없음")
    cfg = load_config(_config(tmp_path, rs, tmp_path / "s"))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        assert rt.hub.connections["erp_lan"].is_remote  # IP 주소라 auto 판단으로 원격
        out = await rt.hub.call("erp_lan__get_local_download_link", {"month": "2026-10"})
        assert "localhost" not in out.text.split("[원격 MCP 파일 처리]")[0], out.text
        assert f"http://{rs['ip']}:" in out.text and "[다운로드 링크 감지]" in out.text
        detected = out.text.split("[다운로드 링크 감지] ")[1].split("\n")[0]
        assert "localhost" not in detected  # 원래 localhost 링크는 다시 잡히지 않음
        url = detected.split()[0]
        got = await rt.hub.call("download_url", {"url": url})
        assert not got.is_error and got.files[0]["state"] == "complete", got.text


async def test_wait_for_download_default_300(tmp_path: Path, monkeypatch) -> None:
    from test_agent import write_config

    from inhouse_agent import builtins as b

    cfg = load_config(write_config(tmp_path))
    assert cfg.files.wait_timeout == 300
    seen: dict = {}

    async def fake_wait(ws, pattern, timeout, since, directory):
        seen["timeout"] = timeout
        return {"state": "timeout", "issues": []}

    monkeypatch.setattr(b, "wait_for_download", fake_wait)
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        spec = next(t["function"] for t in rt.hub.openai_tools() if t["function"]["name"] == "wait_for_download")
        assert "기본 300초" in spec["parameters"]["properties"]["timeout_sec"]["description"]
        await rt.hub.call("wait_for_download", {"pattern": "*.xlsx"})
        assert seen["timeout"] == 300
        await rt.hub.call("wait_for_download", {"pattern": "*.xlsx", "timeout_sec": 30})
        assert seen["timeout"] == 30
    from inhouse_agent.files import wait_for_download

    import inspect

    assert inspect.signature(wait_for_download).parameters["timeout"].default == 300
