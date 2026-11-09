"""프롬프트 캐시: 대화별 캐시 키, 캐시 토큰 집계, 대화가 이어져도 앞부분(시스템 프롬프트·도구)이 그대로인지, 캐시 단가 반영."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_agent import ScriptedLLM, write_config
from test_routing import cfg_with_models, FakePool, router

from inhouse_agent.config import LLMConfig, load_config
from inhouse_agent.llm import AzureChatModel, LLMResponse, ToolCall, _api_messages
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.usage import format_usage


class BadRequest(Exception):
    status_code = 400


class FakeCompletions:
    """AsyncAzureOpenAI().chat.completions 대신. 보낸 인자를 기록하고 정해진 usage 로 답함."""

    def __init__(self, reject_cache_key: bool = False, cached: int = 0):
        self.sent: list[dict[str, Any]] = []
        self.reject = reject_cache_key
        self.cached = cached

    async def create(self, **params: Any) -> Any:
        self.sent.append(params)
        if self.reject and "prompt_cache_key" in (params.get("extra_body") or {}):
            raise BadRequest("Error code: 400 - {'error': {'message': 'Unrecognized request argument supplied: prompt_cache_key'}}")
        usage = SimpleNamespace(prompt_tokens=2000, completion_tokens=50, total_tokens=2050,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=self.cached))
        msg = SimpleNamespace(content="답", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


def azure_model(fake: FakeCompletions, **kw: Any) -> AzureChatModel:
    m = AzureChatModel(LLMConfig(endpoint="https://x.openai.azure.com", api_key="k", deployment="sol", **kw))
    m._client = SimpleNamespace(chat=SimpleNamespace(completions=fake), close=None)
    return m


async def test_azure_sends_cache_key_and_reads_cached_tokens() -> None:
    fake = FakeCompletions(cached=1536)
    m = azure_model(fake, extra_body={"foo": 1})
    r = await m.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "안녕", "_sent": "2026-11-09 14:03 (Mon)"}],
                     cache_key="c-" + "x" * 100)
    p = fake.sent[0]
    assert p["extra_body"] == {"foo": 1, "prompt_cache_key": ("c-" + "x" * 100)[:64]}  # 설정의 extra_body 와 합침, 64자 제한
    assert p["messages"][1]["content"] == "안녕\n\n(보낸 시각: 2026-11-09 14:03 (Mon))"  # 시각은 사용자 메시지 끝에
    assert r.usage == {"prompt_tokens": 2000, "completion_tokens": 50, "total_tokens": 2050, "cached_tokens": 1536}
    await m.chat([{"role": "user", "content": "x"}])  # 캐시 키가 없으면 보내지 않음
    assert fake.sent[1]["extra_body"] == {"foo": 1}
    off = FakeCompletions()
    await azure_model(off, prompt_cache_key=False).chat([{"role": "user", "content": "x"}], cache_key="c-1")
    assert "extra_body" not in off.sent[0]


async def test_azure_drops_cache_key_when_deployment_rejects_it() -> None:
    fake = FakeCompletions(reject_cache_key=True)
    m = azure_model(fake)
    r = await m.chat([{"role": "user", "content": "x"}], cache_key="c-1")
    assert r.content == "답" and len(fake.sent) == 2 and "extra_body" not in fake.sent[1]  # 빼고 다시 보내서 성공
    await m.chat([{"role": "user", "content": "y"}], cache_key="c-1")
    assert len(fake.sent) == 3 and "extra_body" not in fake.sent[2]  # 이후로는 처음부터 보내지 않음
    other = FakeCompletions()
    other.create_orig = other.create

    async def boom(**params: Any) -> Any:
        raise BadRequest("Error code: 400 - invalid messages")

    other.create = boom  # type: ignore[method-assign]
    with pytest.raises(BadRequest):  # 캐시 키와 상관없는 400 은 그대로 오류
        await azure_model(other).chat([{"role": "user", "content": "x"}], cache_key="c-1")


def test_sent_time_on_image_message() -> None:
    out = _api_messages([{"role": "user", "content": [{"type": "text", "text": "이거"}], "_sent": "14:03"}], vision=True)
    assert out[0]["content"][-1] == {"type": "text", "text": "(보낸 시각: 14:03)"} and "_sent" not in out[0]


async def test_conversation_prefix_and_cache_key_stay_same(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM([
        LLMResponse(content=None, tool_calls=[ToolCall("c1", "get_current_time", "{}")]),
        LLMResponse(content="첫 답"),
        LLMResponse(content="둘째 답"),
    ])
    async with AgentRuntime(cfg, llm=llm) as rt:
        msgs = rt.agent.new_conversation()
        await rt.agent.run("첫 질문", messages=msgs)
        first_kw = llm.kw
        await rt.agent.run("둘째 질문", messages=msgs)
        # 첫 턴 마지막 호출에 보낸 메시지가 둘째 턴 요청의 앞부분과 똑같아야 캐시가 적중
        assert llm.calls[2][: len(llm.calls[1])] == llm.calls[1]
        assert first_kw["cache_key"] == llm.kw["cache_key"] == msgs[0]["_cache_key"]
        assert "현재 시각:" not in msgs[0]["content"] and "오늘 날짜" in msgs[0]["content"]
        other = rt.agent.new_conversation()
        assert other[0]["_cache_key"] != msgs[0]["_cache_key"]  # 대화마다 다른 키


async def test_old_conversation_without_key_gets_one(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM([LLMResponse(content="답")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        msgs = [{"role": "system", "content": "옛날 프롬프트"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        await rt.agent.run("이어서", messages=msgs)
    assert msgs[0]["_cache_key"].startswith("c-") and llm.kw["cache_key"] == msgs[0]["_cache_key"]


def test_cost_uses_cached_price() -> None:
    r = router()  # test_routing 의 sol/terra/luna (캐시 단가 없음)
    assert r.get("sol").cost(1_000_000, 0, 600_000) == pytest.approx(5.0)  # cost_cached 없으면 할인 없이
    import yaml

    from inhouse_agent.routing import ModelRouter

    raw = yaml.safe_load("""models:
  - {name: sol, deployment: sol, tier: high, cost_in: 2.0, cost_out: 10.0, cost_cached: 0.1}
  - {name: luna, deployment: luna, tier: low, cost_in: 0.1, cost_out: 0.5, cost_cached: 0.01}
""")
    r2 = ModelRouter(raw, "sol")
    # 입력 100만 중 60만 캐시: 40만×2.0 + 60만×0.1 = 0.86
    assert r2.get("sol").cost(1_000_000, 0, 600_000) == pytest.approx(0.86)
    cost, base = r2.cost_of({"luna": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cached_tokens": 600_000}})
    assert cost == pytest.approx(0.4 * 0.1 + 0.6 * 0.01) and base == pytest.approx(0.86)  # 비교 기준도 같은 캐시 적중
    assert r2.catalog()[0]["cost_cached"] == 0.01


async def test_cached_tokens_flow_into_run_usage_and_report(tmp_path: Path) -> None:
    cfg = cfg_with_models(tmp_path)
    pool = FakePool({"luna": [LLMResponse(content="답", usage={"prompt_tokens": 4000, "completion_tokens": 100,
                                                              "total_tokens": 4100, "cached_tokens": 3072})]})
    async with AgentRuntime(cfg, llm=pool) as rt:
        r = await rt.agent.run("오늘 공지 목록 알려줘")
    assert r.usage["cached_tokens"] == 3072 and r.usage["models"]["luna"]["cached_tokens"] == 3072
    # luna: cost_in 0.2, 캐시 단가 없음 → 할인 없이
    assert r.usage["cost"] == pytest.approx((4000 * 0.2 + 100 * 0.8) / 1e6)
    text = format_usage({
        "days": 1, "users": {"kim": {}}, "runs": 1, "sessions": 1, "files": 0, "avg_elapsed": None, "status": {"completed": 1},
        "kinds": {"chat": 1}, "tokens": {"prompt_tokens": 4000, "completion_tokens": 100, "total_tokens": 4100, "cached_tokens": 3072},
        "job_tokens": 0, "skills": {}, "tools": {}, "tool_errors": {}, "models": {"luna": {"prompt_tokens": 4000, "completion_tokens": 100,
                                                                                      "cached_tokens": 3072, "calls": 1}},
        "model_cost": {"luna": 0.00088}, "cost": 0.00088, "baseline_cost": 0.0215, "currency": "$", "stopped": {}, "jobs": {},
        "by_day": {}, "recent": [],
    })
    assert "프롬프트 캐시: 입력의 77% (3,072 토큰)" in text and "| luna | 1 | 4,000 | 3,072 (77%) | 100 |" in text
