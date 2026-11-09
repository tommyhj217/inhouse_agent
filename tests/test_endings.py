"""실행이 끝나는 방식: 반복 예산 자동 연장, 되풀이·오류·시간 감지 후 정리, 오류 원인 분류, 화면 표시."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.errors import describe_error, status_label
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime


def call(i: int, name: str, args: dict) -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[ToolCall(f"c{i}", name, json.dumps(args, ensure_ascii=False))])


async def test_budget_extends_while_progressing_then_stops(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    cfg.agent.max_iterations, cfg.agent.extend_iterations, cfg.agent.max_iterations_cap = 3, 2, 7
    # 매번 다른 인자로 성공하는 호출 = 진전 → 3 → 5 → 7(상한) 까지 늘어난 뒤 멈춤
    llm = ScriptedLLM([call(i, "save_file", {"path": f"p/{i}.txt", "content": "x"}) for i in range(7)] + [LLMResponse(content="7개 저장함, 나머지 남음")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        events: list[tuple[str, dict]] = []
        r = await rt.agent.run("파일 많이 저장", on_event=lambda k, d: events.append((k, d)))
    assert r.stopped_reason == "max_iterations" and r.iterations == 7 and r.max_iterations == 7
    assert [(d["from"], d["to"]) for k, d in events if k == "budget"] == [(3, 5), (5, 7)]
    assert r.output.startswith("7개 저장함") and "반복 한도에 도달" in r.output and "**계속**" in r.output and "최대 7회" in r.detail


async def test_no_extension_without_progress_and_loop_detection(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    cfg.agent.max_iterations = 3
    # 같은 인자로 같은 도구 되풀이 → 3번째에서 '되풀이' 로 멈춤
    llm = ScriptedLLM([call(i, "list_past_work", {"query": "매출"}) for i in range(3)] + [LLMResponse(content="같은 조회만 반복해 멈춤")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("이전 작업 찾아줘")
    assert r.stopped_reason == "loop" and "list_past_work" in r.detail and r.iterations == 3
    # 기다리기 같은 도구는 되풀이로 보지 않음
    cfg2 = load_config(write_config(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path))
    cfg2.agent.max_iterations, cfg2.agent.max_iterations_cap = 4, 4
    llm2 = ScriptedLLM([call(i, "list_downloads", {}) for i in range(4)] + [LLMResponse(content="정리")])
    async with AgentRuntime(cfg2, llm=llm2) as rt:
        r2 = await rt.agent.run("다운로드 확인")
    assert r2.stopped_reason == "max_iterations" and r2.iterations == 4


async def test_time_limit_and_skill_budget(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    cfg.agent.max_minutes = 0.0001  # 바로 시간 초과
    class SlowLLM(ScriptedLLM):
        async def chat(self, *a, **k):
            import asyncio

            await asyncio.sleep(0.05)  # 0.05초 > 한도(0.006초)
            return await super().chat(*a, **k)

    llm = SlowLLM([call(0, "get_current_time", {}), LLMResponse(content="시간이 부족해 여기까지")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("긴 작업", budget={"max_minutes": 0.0001})
    assert r.stopped_reason == "time_limit" and r.output.startswith("시간이 부족해")


class Boom:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def chat(self, *a, **k):
        raise self.exc


class FakeAPIError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: dict | None = None) -> None:
        super().__init__(msg)
        self.status_code, self.body = status, body


@pytest.mark.parametrize("exc, code, retry", [
    (FakeAPIError("Access denied due to invalid subscription key", 401), "auth", False),
    (FakeAPIError("The API deployment for this resource does not exist", 404, {"error": {"code": "DeploymentNotFound"}}), "deployment", False),
    (FakeAPIError("Rate limit is exceeded", 429), "rate_limit", True),
    (FakeAPIError("The response was filtered", 400, {"error": {"code": "content_filter"}}), "content_filter", False),
    (FakeAPIError("This model's maximum context length is 128000 tokens", 400), "context", False),
    (TimeoutError("Request timed out."), "timeout", True),
    (ConnectionError("Connection error."), "network", True),
    (FakeAPIError("upstream", 503), "server", True),
    (KeyError("x"), "internal", True),
])
def test_error_classification(exc: Exception, code: str, retry: bool) -> None:
    info = describe_error(exc, "gpt-sol")
    assert info["code"] == code and info["retryable"] is retry and info["title"] and info["hint"]


async def test_llm_error_ends_with_clear_message(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=Boom(FakeAPIError("The API deployment for this resource does not exist", 404))) as rt:
        events: list[tuple[str, dict]] = []
        r = await rt.agent.run("질문", on_event=lambda k, d: events.append((k, d)))
    assert r.stopped_reason == "error" and r.error["code"] == "deployment" and not r.success
    assert r.output.startswith("❌ **모델 배포를 찾을 수 없습니다") and "- 조치: config.yaml 의 llm.deployment" in r.output
    err = next(d for k, d in events if k == "error")
    assert err["code"] == "deployment" and err["hint"]
    assert status_label("loop") == "같은 작업을 되풀이해 멈췄습니다"


async def test_truncated_filtered_and_empty_answers(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([LLMResponse(content="긴 답변의 앞부분", finish_reason="length")])) as rt:
        r = await rt.agent.run("길게 써줘")
    assert r.success and "최대 출력 길이" in r.output and "**이어서**" in r.output
    async with AgentRuntime(cfg, llm=ScriptedLLM([LLMResponse(content=None, finish_reason="content_filter")])) as rt:
        r = await rt.agent.run("x")
    assert r.stopped_reason == "error" and r.error["code"] == "content_filter"
    async with AgentRuntime(cfg, llm=ScriptedLLM([LLMResponse(content=""), LLMResponse(content="두 번째에 답함")])) as rt:
        r = await rt.agent.run("x")  # 빈 답변은 한 번 다시 받음
    assert r.success and r.output == "두 번째에 답함"
    async with AgentRuntime(cfg, llm=ScriptedLLM([LLMResponse(content=""), LLMResponse(content="")])) as rt:
        r = await rt.agent.run("x")
    assert r.stopped_reason == "error" and r.error["code"] == "empty"


def test_server_done_event_and_saved_turn_explain_failures(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(write_config(tmp_path))
    H = {"X-Agent-User": "kim"}

    def done_of(c, body):
        run = c.post("/api/runs", json=body, headers=H).json()
        with c.stream("GET", f"/api/runs/{run['id']}/events", headers=H) as s:
            text = "".join(s.iter_text())
        line = [ln for ln in text.splitlines() if ln.startswith("data:") and '"type": "done"' in ln][-1]
        return run, json.loads(line[5:])

    rt = AgentRuntime(cfg, llm=Boom(FakeAPIError("Rate limit is exceeded", 429)))
    with TestClient(create_app(cfg, runtime=rt, enable_scheduler=False)) as c:
        run, d = done_of(c, {"prompt": "매출 알려줘", "new_session": True})
        assert d["status"] == "error" and d["level"] == "error" and d["label"] == "오류로 실행을 마치지 못했습니다"
        assert d["error"]["code"] == "rate_limit" and d["error"]["retryable"] and d["output"].startswith("❌ **LLM 요청 한도")
        turn = c.get(f"/api/sessions/{run['session_id']}", headers=H).json()["turns"][-1]
        assert turn["status"] == "error" and turn["error"]["code"] == "rate_limit"
        assert c.get("/api/runs", headers=H).json()[0]["label"] == "오류로 실행을 마치지 못했습니다"
        # 워크플로(토큰 없이 도구 실행) 단계 실패도 같은 형식
        run, d = done_of(c, {"tool": "read_file_text", "args": {"path": "files/없는파일.txt"}, "new_session": True})
        assert d["status"] == "error" and d["error"]["code"] == "step_failed" and "'read_file_text' 실행 중 오류" in d["error"]["title"]
        assert d["output"].startswith("❌ **'read_file_text'") and "없는파일" in d["error"]["detail"]
        html = c.get("/").text
        assert "endBox" in html and "계속 진행" in html and "다시 시도" in html
