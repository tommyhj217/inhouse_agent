"""통합 테스트: 실제 샘플 MCP 서버(stdio) + 스크립트된 가짜 LLM / 가짜 Azure 엔드포인트."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

try:
    from fastapi import Request
except ImportError:  # pragma: no cover
    Request = None  # type: ignore[assignment,misc]

from inhouse_agent.agent import Agent
from inhouse_agent.config import load_config
from inhouse_agent.llm import AzureChatModel, LLMResponse, ToolCall
from inhouse_agent.mcp_hub import make_function_name
from inhouse_agent.runtime import AgentRuntime, render_prompt

ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path: Path, extra_llm: str = "") -> Path:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        f"""
llm:
  endpoint: https://example.openai.azure.com
  deployment: test-deploy
  api_key: dummy
{extra_llm}
agent:
  max_iterations: 5
mcp_servers:
  demo:
    description: 데모
    command: {json.dumps(sys.executable)}
    args: [{json.dumps(str(ROOT / "examples" / "sample_mcp_server.py"))}]
  broken:
    description: 죽은 서버
    command: {json.dumps(sys.executable)}
    args: ["-c", "import sys; sys.exit(1)"]
    connect_timeout: 10
schedules:
  - name: notice
    cron: "0 9 * * *"
    servers: [demo]
    prompt: "{{yesterday}} 공지 정리"
""",
        encoding="utf-8",
    )
    return cfg_dir / "config.yaml"


class ScriptedLLM:
    """미리 정한 순서대로 응답하는 가짜 LLM. 받은 messages 를 기록."""

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, **kw):
        self.calls.append([dict(m) for m in messages])
        self.tools = tools
        self.kw = kw
        return self.responses.pop(0)


async def test_agent_calls_mcp_tools_and_saves(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall("c1", "demo__list_notices", json.dumps({"date_from": "2026-10-31"})),
                    ToolCall("c2", "demo__get_daily_sales", json.dumps({"date_from": "2026-10-01", "date_to": "2026-10-02"})),
                    ToolCall("c3", "save_file", json.dumps({"path": "a/b.csv", "content": "x,y\n1,2\n"})),
                ],
            ),
            LLMResponse(content=None, tool_calls=[ToolCall("c4", "demo__get_daily_sales", '{"date_from":"2026-01-01","date_to":"2026-12-31"}')]),
            LLMResponse(content="정리 완료"),
        ]
    )
    async with AgentRuntime(cfg, llm=llm) as rt:
        assert rt.start_results["demo"] is None
        assert rt.start_results["broken"] is not None  # 죽은 서버는 건너뛰고 계속 동작
        names = {t["function"]["name"] for t in rt.hub.openai_tools()}
        assert {"demo__list_notices", "demo__get_notice", "save_file"} <= names

        result = await rt.agent.run("어제 공지와 매출 정리")
        assert result.success and result.output == "정리 완료"
        ev = {e.function: e for e in result.tool_events}
        assert "N20261031" in ev["demo__list_notices"].result
        assert (cfg.workspace_path / "files" / "a" / "b.csv").read_text(encoding="utf-8-sig") == "x,y\n1,2\n"
        # 두 번째 호출은 서버가 오류(92일 초과)를 돌려주고, 에이전트는 그것을 LLM 에 전달
        err = [e for e in result.tool_events if e.is_error]
        assert err and "92" in err[0].result

        # LLM 에 tool 결과가 tool_call_id 와 함께 전달되었는지
        last_msgs = llm.calls[-1]
        tool_msgs = [m for m in last_msgs if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3", "c4"]

        # 예약작업 실행 + 저장
        llm.responses = [LLMResponse(content="예약 결과")]
        res, path = await rt.run_job(cfg.get_schedule("notice"))
        assert path.exists() and "예약 결과" in path.read_text(encoding="utf-8")
        assert (path.parent / "latest.md").exists()
        # servers 제한 → demo + builtin 만 노출 (broken 서버 tool 은 없음)
        assert not any(t["function"]["name"].startswith("broken__") for t in llm.tools)


async def test_server_crash_reconnect(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        conn = rt.hub.connections["demo"]
        conn._stop.set()  # 연결을 강제로 끊음
        await asyncio.sleep(0.5)
        assert not conn.connected
        out = await rt.hub.call("demo__get_notice", {"notice_id": "N1"})
        assert not out.is_error and "N1" in out.text


async def test_bad_json_and_max_iterations(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    loop_resp = [LLMResponse(content=None, tool_calls=[ToolCall(f"x{i}", "get_current_time", "{")]) for i in range(3)]
    llm = ScriptedLLM(loop_resp + [LLMResponse(content="한도 도달 요약")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("테스트")
        # 도구 오류(잘못된 JSON 인자)가 3번 이어지면 한도를 다 쓰지 않고 멈추고, 도구 없이 정리한 답 + 안내
        assert r.stopped_reason == "tool_errors" and r.output.startswith("한도 도달 요약") and "계속" in r.output
        assert all(e.is_error and "JSON" in e.result for e in r.tool_events) and len(r.tool_events) == 3
        assert "도구를 부르지 말고" in llm.calls[-1][-1]["content"]
        # 마지막 정리: 도구 목록은 그대로 두고(프롬프트 캐시 유지) tool_choice=none 으로만 막음
        assert llm.tools and llm.kw.get("tool_choice") == "none" and llm.kw.get("parallel_tool_calls") is None


def test_function_name_rules() -> None:
    used: set[str] = set()
    a = make_function_name("site.a", "get data", used)
    assert a == "site_a__get_data"
    long = make_function_name("s" * 40, "t" * 40, used)
    assert len(long) <= 64
    dup = make_function_name("site_a", "get_data", used)
    assert dup != a and len(dup) <= 64


def test_render_prompt() -> None:
    now = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))  # 월요일
    out = render_prompt('{yesterday} {last_week_start}~{last_week_end} {last_month} {"json": 1}', "Asia/Seoul", now)
    assert out == '2026-11-01 2026-10-26~2026-11-01 2026-10 {"json": 1}'


def test_history_trim() -> None:
    from inhouse_agent.config import AgentConfig

    agent = Agent(ScriptedLLM([]), None, AgentConfig(max_history_messages=5))  # type: ignore[arg-type]
    msgs = [{"role": "system", "content": "s"}]
    for i in range(4):
        msgs += [
            {"role": "user", "content": f"u{i}"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]},
            {"role": "tool", "tool_call_id": f"t{i}", "content": "r"},
            {"role": "assistant", "content": "a"},
        ]
    agent._trim_history(msgs)
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user" and len(msgs) == 5


# ---------------------------------------------------------------------------
# 가짜 Azure OpenAI 엔드포인트로 실제 openai SDK 경로 검증
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def fake_azure():
    fastapi = pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    app = fastapi.FastAPI()
    seen: list[dict[str, Any]] = []

    @app.post("/openai/deployments/{dep}/chat/completions")
    async def completions(dep: str, request: Request):
        body = await request.json()
        seen.append({"dep": dep, "query": dict(request.query_params), "headers": dict(request.headers), "body": body})
        has_tool_result = any(m["role"] == "tool" for m in body["messages"])
        if body.get("tools") and not has_tool_result:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "demo__get_notice", "arguments": '{"notice_id":"N7"}'}}
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "최종: " + body["messages"][-1]["content"][:20]}
            finish = "stop"
        return {
            "id": "x",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": dep,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", seen
    server.should_exit = True
    th.join(5)


async def test_azure_sdk_roundtrip(tmp_path: Path, fake_azure, monkeypatch) -> None:
    url, seen = fake_azure
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg = load_config(write_config(tmp_path, "  extra_headers:\n    Ocp-Apim-Subscription-Key: sub123\n"))
    cfg.llm.endpoint = url
    cfg.llm.max_retries = 0
    async with AgentRuntime(cfg, llm=AzureChatModel(cfg.llm)) as rt:
        r = await rt.agent.run("공지 N7 보여줘")
    assert r.success, r.output
    assert r.tool_events[0].tool == "get_notice" and "N7" in r.tool_events[0].result
    first = seen[0]
    assert first["dep"] == "test-deploy"
    assert first["query"]["api-version"] == cfg.llm.api_version
    assert first["headers"]["api-key"] == "dummy"
    assert first["headers"]["ocp-apim-subscription-key"] == "sub123"
    assert first["body"]["max_completion_tokens"] == 4096
    assert "temperature" not in first["body"]
    second = seen[1]["body"]["messages"]
    assert second[-2]["tool_calls"][0]["id"] == "call_1" and second[-1]["role"] == "tool"
    assert r.usage["total_tokens"] == 30


async def test_model_pool_routes_to_azure_deployments(tmp_path: Path, fake_azure, monkeypatch) -> None:
    """여러 배포(sol/luna)를 등록하면 실제 Azure 호출이 고른 배포로 가는지 (ModelPool)."""
    url, seen = fake_azure
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg = load_config(write_config(tmp_path, "  models:\n    - {name: sol, deployment: test-deploy, tier: high, cost_in: 5, cost_out: 15}\n"
                                             "    - {name: luna, deployment: luna-dep, tier: low, cost_in: 0.2, cost_out: 0.8, max_output_tokens: 1000}\n"))
    cfg.llm.endpoint = url
    cfg.llm.max_retries = 0
    async with AgentRuntime(cfg) as rt:
        r = await rt.agent.run("공지 N7 목록 알려줘")  # 단순 조회 → luna
        assert r.success, r.output
        assert [s["dep"] for s in seen] == ["luna-dep", "luna-dep"] and seen[0]["body"]["max_completion_tokens"] == 1000
        r2 = await rt.agent.run("다시", model="sol")
        assert seen[-1]["dep"] == "test-deploy" and r2.usage["models"]["sol"]["calls"] == 2 and r.usage["cost"] > 0
