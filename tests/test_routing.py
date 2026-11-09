"""비용 기준 모델 선택: 난이도 판별, 승격, 다른 모델로 넘어가기, 요약은 싼 모델, 모델별 비용·절약 계산."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_agent import write_config

from inhouse_agent.config import load_config
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.routing import ModelRouter
from inhouse_agent.runtime import AgentRuntime

MODELS = """  models:
    - {name: sol, deployment: sol, tier: high, cost_in: 5.0, cost_out: 15.0, context_window: 272000}
    - {name: terra, deployment: terra, tier: mid, cost_in: 1.0, cost_out: 4.0}
    - {name: luna, deployment: luna, tier: low, cost_in: 0.2, cost_out: 0.8, vision: false}
  routing: {mode: auto, escalate: true, summary_model: luna, currency: "$"}
"""


def cfg_with_models(tmp_path: Path, extra: str = ""):
    p = write_config(tmp_path, MODELS)
    text = p.read_text(encoding="utf-8").replace("deployment: test-deploy", "deployment: sol")
    p.write_text(text + extra, encoding="utf-8")
    return load_config(p)


class FakePool:
    """모델 이름별로 응답을 정하는 가짜 모델 묶음 (ModelPool 대신)."""

    def __init__(self, script: dict[str, list[Any]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, list[dict]]] = []

    async def chat(self, messages, tools=None, *, model=None, **kw):
        self.calls.append((model, [dict(m) for m in messages]))
        item = self.script[model].pop(0)
        if isinstance(item, Exception):
            raise item
        r = LLMResponse(content=item.content, tool_calls=item.tool_calls, finish_reason=item.finish_reason,
                        usage=item.usage or {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100})
        r.model = model
        return r


def router() -> ModelRouter:
    import yaml

    return ModelRouter(yaml.safe_load("llm:\n" + MODELS)["llm"], "sol")


async def test_router_choices() -> None:
    r = router()
    assert [s.name for s in r.specs] == ["luna", "terra", "sol"]  # 싼 순
    assert (await r.choose(None, "오늘 공지 목록 알려줘")).model == "luna"
    assert (await r.choose(None, "3월 매출 엑셀을 부서별로 정리해서 요약해줘")).model in ("luna", "terra")
    assert (await r.choose(None, "두 보고서를 비교 분석해서 차이와 원인을 정리한 보고서를 작성해줘", attachments=2)).model == "sol"
    assert (await r.choose(None, "이 화면 캡처 뭐야?", images=1)).model == "terra"  # luna 는 이미지 미지원
    assert (await r.choose(None, "아무거나", override="sol")).reason == "사용자가 선택"
    assert (await r.choose(None, "x", skill_meta={"model": "terra"})).model == "terra"
    assert (await r.choose(None, "계속", last_model="sol")).model == "sol"  # 이어서 하기는 같은 모델
    assert (await r.choose(None, "틀렸어 다시 해줘", last_model="luna")).model == "terra"  # 고쳐 달라면 한 단계 위
    assert (await r.choose(None, "아주 긴 분석", max_tier=2, attachments=3)).model == "terra"  # 하위 에이전트 상한
    assert r.escalate("luna").name == "terra" and r.escalate("terra").name == "sol" and r.escalate("sol") is None
    assert r.fallback("terra", {"terra"}).name == "sol" and r.fallback("sol", {"sol"}).name == "terra"
    cost, base = r.cost_of({"luna": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})
    assert cost == 0.2 and base == 5.0
    fixed = ModelRouter({"models": ["a", "b"], "routing": {"mode": "fixed"}}, "a")
    assert (await fixed.choose(None, "비교 분석 보고서")).model == "a"


async def test_llm_classifier_uses_cheapest_model() -> None:
    import yaml

    r = ModelRouter(yaml.safe_load("llm:\n" + MODELS.replace("mode: auto", "mode: auto, classifier: llm"))["llm"], "sol")
    pool = FakePool({"luna": [LLMResponse(content='{"level": "high", "reason": "여러 단계"}')]})
    d = await r.choose(pool, "이번 달 실적 정리")
    assert d.model == "sol" and pool.calls[0][0] == "luna" and "luna 판별" in d.reason


async def test_simple_request_uses_cheap_model_and_reports_savings(tmp_path: Path) -> None:
    cfg = cfg_with_models(tmp_path)
    pool = FakePool({"luna": [LLMResponse(content=None, tool_calls=[ToolCall("c1", "get_current_time", "{}")]), LLMResponse(content="오늘은 11월 9일")]})
    async with AgentRuntime(cfg, llm=pool) as rt:
        events: list[tuple[str, dict]] = []
        r = await rt.agent.run("오늘 날짜 알려줘", on_event=lambda k, d: events.append((k, d)))
    assert r.success and [c[0] for c in pool.calls] == ["luna", "luna"]
    assert r.models[0]["model"] == "luna" and next(d for k, d in events if k == "model")["kind"] == "start"
    u = r.usage
    assert u["models"]["luna"]["calls"] == 2 and u["cost"] == pytest.approx(2 * (1000 * 0.2 + 100 * 0.8) / 1e6)
    assert u["baseline_cost"] == pytest.approx(2 * (1000 * 5 + 100 * 15) / 1e6) and r.messages[-1]["_model"] == "luna"


async def test_escalates_on_tool_errors_and_loops(tmp_path: Path) -> None:
    cfg = cfg_with_models(tmp_path)
    bad = [LLMResponse(content=None, tool_calls=[ToolCall(f"b{i}", "get_current_time", "{")]) for i in range(2)]  # 잘못된 인자
    pool = FakePool({"luna": bad, "terra": [LLMResponse(content="terra 가 해결")]})
    async with AgentRuntime(cfg, llm=pool) as rt:
        r = await rt.agent.run("오늘 공지 목록 알려줘")
    assert r.success and r.output == "terra 가 해결"
    assert [m["model"] for m in r.models] == ["luna", "terra"] and "도구 오류가 이어짐" in r.models[1]["reason"]
    # 같은 호출 되풀이 → 승격하고 안내 메시지, 승격한 모델이 다른 방법으로 답
    same = [LLMResponse(content=None, tool_calls=[ToolCall(f"s{i}", "list_past_work", '{"query": "x"}')]) for i in range(3)]
    pool2 = FakePool({"luna": same, "terra": [LLMResponse(content="되풀이 대신 정리")]})
    async with AgentRuntime(cfg, llm=pool2) as rt:
        r2 = await rt.agent.run("이전 작업 찾아줘")
    assert r2.success and [m["model"] for m in r2.models] == ["luna", "terra"]
    assert any("같은 호출을 반복하지 말고" in str(m.get("content")) for m in pool2.calls[-1][1])


async def test_budget_escalation_before_stopping(tmp_path: Path) -> None:
    cfg = cfg_with_models(tmp_path)
    cfg.agent.max_iterations, cfg.agent.max_iterations_cap, cfg.agent.extend_iterations = 2, 2, 2
    # 진전 없는(같은 결과를 내는 기다리기 도구) 반복 → 멈추기 전에 승격해서 2회 더
    wait = [LLMResponse(content=None, tool_calls=[ToolCall(f"w{i}", "list_downloads", "{}")]) for i in range(2)]
    pool = FakePool({"luna": wait, "terra": [LLMResponse(content="다운로드 없음 확인")]})
    async with AgentRuntime(cfg, llm=pool) as rt:
        r = await rt.agent.run("다운로드 확인")
    assert r.success and r.max_iterations == 4 and "반복 상한" in r.models[1]["reason"]


async def test_falls_back_when_model_unavailable(tmp_path: Path) -> None:
    class RateLimit(Exception):
        status_code = 429

    cfg = cfg_with_models(tmp_path)
    pool = FakePool({"luna": [RateLimit("Rate limit is exceeded")], "terra": [LLMResponse(content="terra 로 답함")]})
    async with AgentRuntime(cfg, llm=pool) as rt:
        r = await rt.agent.run("오늘 공지 목록 알려줘")
    assert r.success and r.output == "terra 로 답함" and "응답하지 못함" in r.models[1]["reason"]


async def test_compaction_summary_uses_cheapest_model(tmp_path: Path) -> None:
    cfg = cfg_with_models(tmp_path)
    cfg.agent.context_window, cfg.agent.max_history_messages = 8000, 10
    pool = FakePool({"luna": [LLMResponse(content="요약: 3월 매출 1,234원")], "sol": [LLMResponse(content="부서별로 나눔")]})
    async with AgentRuntime(cfg, llm=pool) as rt:
        msgs = rt.agent.new_conversation()
        for i in range(8):
            msgs += [{"role": "user", "content": f"{i} 조회"}, {"role": "assistant", "content": "3월 매출 1,234원 " + "자료 " * 300}]
        r = await rt.agent.run("두 보고서를 비교 분석해서 보고서로 작성", messages=msgs, model="sol")
    assert r.success and pool.calls[0][0] == "luna" and pool.calls[0][1][0]["content"].startswith("당신은 업무 에이전트의 작업 기록")
    assert set(r.usage["models"]) == {"luna", "sol"}


def test_server_model_choice_and_info(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = cfg_with_models(tmp_path)
    pool = FakePool({"terra": [LLMResponse(content="terra 답")]})
    app = create_app(cfg, runtime=AgentRuntime(cfg, llm=pool), enable_scheduler=False)
    H = {"X-Agent-User": "kim"}
    with TestClient(app) as c:
        info = c.get("/api/info").json()["llm"]
        assert [m["name"] for m in info["models"]] == ["luna", "terra", "sol"] and info["routing"] == "auto" and info["currency"] == "$"
        run = c.post("/api/runs", json={"prompt": "날짜 알려줘", "model": "terra", "new_session": True}, headers=H).json()
        with c.stream("GET", f"/api/runs/{run['id']}/events", headers=H) as s:
            body = "".join(s.iter_text())
        done = json.loads([ln for ln in body.splitlines() if ln.startswith("data:") and '"type": "done"' in ln][-1][5:])
        assert done["models"][0] == {"model": "terra", "reason": "사용자가 선택", "from": None} and done["usage"]["cost"] > 0
        assert done["currency"] == "$"
        turn = c.get(f"/api/sessions/{run['session_id']}", headers=H).json()["turns"][-1]
        assert turn["models"] == ["terra"] and turn["usage"]["models"]["terra"]["calls"] == 1
        u = c.get("/api/usage?days=1", headers=H).json()
        assert u["models"]["terra"]["calls"] == 1 and "절약" in u["text"]
        assert "chosenModel" in c.get("/").text
