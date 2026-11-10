"""출력 한도(max_output_tokens) 오류 재현과 자동 복구.

재현: 추론 모델은 보이지 않는 추론 토큰도 출력 한도를 씀 → 한도가 작으면 추론만 하다 답 없이
status=incomplete(reason=max_output_tokens) 로 끝남 (Azure 는 error 칸까지 채워 보내기도 함 → 예전엔 RuntimeError 로 실행이 끝남).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_agent import ScriptedLLM, write_config
from test_responses_api import FakeAzureResponses, httpx

from inhouse_agent.config import LLMConfig, load_config
from inhouse_agent.llm import AzureChatModel, AzureResponsesModel
from inhouse_agent.runtime import AgentRuntime


class BudgetedResponses(FakeAzureResponses):
    """출력 한도가 need(추론 강도별) 보다 작으면 추론만 하고 잘린 응답. too_large 보다 크면 400."""

    def __init__(self, need: dict[str, int], too_large: int | None = None, with_error: bool = True, **kw: Any):
        super().__init__(**kw)
        self.need, self.too_large, self.with_error = need, too_large, with_error

    def handler(self, req: Any) -> Any:
        body = json.loads(req.content)
        budget = body.get("max_output_tokens") or 10**9
        effort = (body.get("reasoning") or {}).get("effort", "medium")
        if self.too_large and budget > self.too_large:
            self.requests.append((str(req.url), body))
            return self.err(f"max_output_tokens is too large: {budget}. This model supports at most {self.too_large} completion tokens.")
        if budget < self.need.get(effort, 10**9):
            self.requests.append((str(req.url), body))
            data = {"id": "resp_x", "object": "response", "created_at": 0, "model": body["model"], "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "id": "rs_x", "summary": [], "encrypted_content": f"enc:{body['model']}:x"}],
                    "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                    "usage": {"input_tokens": 30000, "output_tokens": budget, "total_tokens": 30000 + budget,
                              "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": budget}}}
            if self.with_error:  # 일부 Azure 응답처럼 error 칸까지 채움
                data["error"] = {"code": "max_output_tokens", "message": "Response incomplete: max_output_tokens"}
            return httpx.Response(200, json=data)
        return super().handler(req)


def model(fake: FakeAzureResponses, **kw: Any) -> AzureResponsesModel:
    cfg = LLMConfig(endpoint="https://corp.openai.azure.com", api_key="k", deployment="sol", api="responses", max_retries=0, **kw)
    return AzureResponsesModel(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))


Q = [{"role": "user", "content": "긴 보고서 분석"}]


async def test_raises_budget_and_remembers_it() -> None:
    fake = BudgetedResponses({"medium": 12000})
    m = model(fake, max_output_tokens=8192, reasoning_effort="medium")
    r = await m.chat(Q)
    assert r.content == "OK" and r.finish_reason == "stop"
    assert [b["max_output_tokens"] for _, b in fake.requests] == [8192, 64000]  # 바로 상한(기본 64000)으로 올려 다시
    assert r.usage["output_retries"] == 1 and r.usage["prompt_tokens"] == 30000 + 1500  # 다시 보낸 호출 토큰도 합산
    assert r.budget_notes == ["출력 한도 8,192 → 64,000"]
    await m.chat(Q)
    assert fake.requests[-1][1]["max_output_tokens"] == 64000  # 올린 값을 기억 → 다음 요청은 바로 성공 (낭비 1번뿐)
    assert len(fake.requests) == 3


async def test_lowers_reasoning_effort_at_cap() -> None:
    fake = BudgetedResponses({"high": 10**9, "medium": 30000})
    m = model(fake, max_output_tokens=16000, max_output_tokens_cap=20000, reasoning_effort="high")
    r = await m.chat(Q)
    sent = [(b["max_output_tokens"], b["reasoning"]["effort"]) for _, b in fake.requests]
    # 상한(20000)까지 올리고 → 그래도 잘리면 추론 강도를 한 단계 낮춤 → 그래도 안 되면 잘린 그대로 돌려줌 (낭비 최대 2번)
    assert sent == [(16000, "high"), (20000, "high"), (20000, "medium")]
    assert r.finish_reason == "length" and r.content is None and r.usage["output_retries"] == 2
    fake2 = BudgetedResponses({"high": 10**9, "medium": 18000})
    r2 = await model(fake2, max_output_tokens=16000, max_output_tokens_cap=20000, reasoning_effort="high").chat(Q)
    assert r2.content == "OK" and r2.budget_notes == ["출력 한도 16,000 → 20,000", "추론 강도 high → medium"]


async def test_shrinks_when_deployment_says_too_large() -> None:
    fake = BudgetedResponses({"medium": 0}, too_large=32768)
    m = model(fake, max_output_tokens=100000)
    r = await m.chat(Q)
    assert r.content == "OK" and [b["max_output_tokens"] for _, b in fake.requests] == [100000, 50000, 25000]
    await m.chat(Q)
    assert fake.requests[-1][1]["max_output_tokens"] == 25000


async def test_agent_reports_output_limit_clearly_instead_of_runtime_error(tmp_path: Path) -> None:
    fake = BudgetedResponses({"medium": 10**9, "low": 10**9, "minimal": 10**9})  # 무엇을 해도 부족
    cfg = load_config(write_config(tmp_path, "  api: responses\n  max_output_tokens: 8192\n  max_output_tokens_cap: 16000\n  reasoning_effort: medium"))
    llm = AzureResponsesModel(cfg.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    events: list[tuple[str, dict]] = []
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("긴 보고서 분석", on_event=lambda k, d: events.append((k, d)))
    assert r.stopped_reason == "error" and r.error["code"] == "output_limit", r.output
    assert "출력 한도(max_output_tokens)에 닿아" in r.output and "추론에 썼고" in r.output and "reasoning_effort" in r.output
    assert "RuntimeError" not in r.output
    notes = [d["notes"] for k, d in events if k == "output_budget"]
    assert notes and "출력 한도 8,192 → 16,000" in notes[0]  # 상한 16000


async def test_agent_recovers_automatically(tmp_path: Path) -> None:
    fake = BudgetedResponses({"medium": 12000}, tool="get_current_time")
    cfg = load_config(write_config(tmp_path, "  api: responses\n  max_output_tokens: 8192\n  reasoning_effort: medium"))
    llm = AzureResponsesModel(cfg.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("지금 몇 시야?")
    assert r.success and r.output == "42" and r.usage["output_retries"] == 1


async def test_chat_completions_path_also_retries() -> None:
    sent: list[dict[str, Any]] = []

    class Completions:
        async def create(self, **params: Any) -> Any:
            sent.append(params)
            short = params["max_completion_tokens"] < 12000
            msg = SimpleNamespace(content=None if short else "답", tool_calls=None)
            usage = SimpleNamespace(prompt_tokens=100, completion_tokens=params["max_completion_tokens"] if short else 10, total_tokens=0,
                                    prompt_tokens_details=None, completion_tokens_details=SimpleNamespace(reasoning_tokens=8000 if short else 5))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="length" if short else "stop")], usage=usage)

    m = AzureChatModel(LLMConfig(endpoint="https://x", api_key="k", deployment="sol", max_output_tokens=8192))
    m._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    r = await m.chat(Q)
    assert r.content == "답" and [p["max_completion_tokens"] for p in sent] == [8192, 64000] and r.usage["reasoning_tokens"] == 8005


async def test_large_tool_result_is_saved_to_file_and_readable(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    cfg.agent.max_tool_result_chars = 2000
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        big = "".join(f"{i:05d}행 매출 {i * 10}\n" for i in range(3000))
        out = rt.agent._fit_result(big, "demo__get_daily_sales")
        assert len(out) < 2400 and "전체 " in out and "tool_results/" in out
        path = out.split("전체는 '")[1].split("'")[0]
        back = await rt.hub.call("read_file_text", {"path": path, "start": 30000, "max_chars": 100})
    assert not back.is_error and big[30000:30020] in back.text  # 잘린 가운데 부분도 다시 읽을 수 있음
