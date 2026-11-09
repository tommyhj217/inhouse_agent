"""Responses API (llm.api: responses): 실제 openai SDK + 가짜 Azure Responses 서버(HTTP)로
주소, 요청 본문, 추론 항목 이어 받기, 다른 모델 대화, 거절 시 자동 끄기, 에이전트 실행까지 확인."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai._base_client as _bc
import pytest
from test_agent import write_config

from inhouse_agent.cli import llm_roundtrip
from inhouse_agent.config import ConfigError, LLMConfig, load_config
from inhouse_agent.llm import (AzureResponsesModel, LLMResponse, ToolCall, make_chat_model, parse_response,
                               responses_base_url, responses_input)
from inhouse_agent.runtime import AgentRuntime

httpx = getattr(_bc, "httpx", None) or getattr(_bc, "httpx2")  # openai 1.x: httpx, 3.x: httpx2


class FakeAzureResponses:
    """Azure Responses API 흉내. store=false 에서 실제 API 가 거절하는 경우를 똑같이 400 으로 돌려줌:
    - 결과(function_call_output)의 call_id 가 앞의 function_call 에 없음
    - 추론 항목에 암호화 내용이 없거나 다른 배포가 만든 것
    - fc_ id 가 있는 function_call 인데 짝인 추론 항목(rs_)이 앞에 없음"""

    def __init__(self, reject_include: bool = False, tool: str = "get_secret_number"):
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.reject_include = reject_include
        self.tool = tool
        self.n = 0

    def handler(self, req: Any) -> Any:
        body = json.loads(req.content)
        self.requests.append((str(req.url), body))
        if self.reject_include and "include" in body:
            return self.err("The parameter 'include' with value 'reasoning.encrypted_content' is not supported with this model.")
        model, items = body["model"], body["input"]
        calls: set[str] = set()
        reasoning_ids: set[str] = set()
        for it in items:
            t = it.get("type")
            if t == "reasoning":
                enc = it.get("encrypted_content") or ""
                if not enc.startswith(f"enc:{model}:"):
                    return self.err(f"The encrypted content for item {it.get('id')} could not be verified.")
                reasoning_ids.add(it["id"])
            elif t == "function_call":
                if it.get("id", "").startswith("fc_") and "rs_" + it["id"][3:] not in reasoning_ids:
                    return self.err(f"Item '{it['id']}' of type 'function_call' was provided without its required 'reasoning' item.")
                calls.add(it["call_id"])
            elif t == "function_call_output" and it["call_id"] not in calls:
                return self.err(f"No tool call found for function call output with call_id {it['call_id']}.")
        self.n += 1
        n = self.n
        rs = {"type": "reasoning", "id": f"rs_{n}", "summary": [], "encrypted_content": f"enc:{model}:{n}"}
        last = items[-1]
        wants_tool = body.get("tools") and body.get("tool_choice") != "none" and last.get("type") != "function_call_output"
        if wants_tool:
            names = [t["name"] for t in body["tools"]]
            out = [rs, {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": self.tool if self.tool in names else names[0],
                        "arguments": "{}", "status": "completed"}]
        else:
            text = "42" if last.get("type") == "function_call_output" else "OK"
            out = [rs, {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}]
        if "reasoning.encrypted_content" not in (body.get("include") or []):
            out[0].pop("encrypted_content")
        cached = 1024 if len(items) > 2 else 0
        return httpx.Response(200, json={
            "id": f"resp_{n}", "object": "response", "created_at": 0, "model": model, "status": "completed", "output": out,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 1500, "output_tokens": 30, "total_tokens": 1530,
                      "input_tokens_details": {"cached_tokens": cached}, "output_tokens_details": {"reasoning_tokens": 20}}})

    @staticmethod
    def err(msg: str) -> Any:
        return httpx.Response(400, json={"error": {"message": msg, "type": "invalid_request_error", "param": None, "code": None}})


def model(fake: FakeAzureResponses, **kw: Any) -> AzureResponsesModel:
    cfg = LLMConfig(endpoint="https://corp.openai.azure.com", api_key="k", deployment=kw.pop("deployment", "sol"), api="responses",
                    max_retries=0, **kw)
    return AzureResponsesModel(cfg, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))


async def test_roundtrip_replays_reasoning_over_real_sdk() -> None:
    fake = FakeAzureResponses()
    m = model(fake, reasoning_effort="low", max_output_tokens=8192)
    line = await llm_roundtrip(m)
    assert line.startswith("✓ 도구 왕복: 답 '42'") and "추론 항목 1개를 이어 받음" in line, line
    url, first = fake.requests[0]
    assert url.startswith("https://corp.openai.azure.com/openai/v1/responses") and "api-version=v1" in url
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"] and first["model"] == "sol"
    assert first["reasoning"] == {"effort": "low"} and first["max_output_tokens"] == 8192 and first["prompt_cache_key"] == "check-roundtrip"
    assert first["tools"][0] == {"type": "function", "name": "get_secret_number", "description": "점검용 비밀 숫자를 돌려줍니다.",
                                 "parameters": {"type": "object", "properties": {}}, "strict": False}
    assert first["input"][0] == {"role": "system", "content": "연결 점검입니다. 도구를 써서 답하세요."}
    second = fake.requests[1][1]["input"]
    assert [i.get("type") or i.get("role") for i in second] == ["system", "user", "reasoning", "function_call", "function_call_output"]
    assert second[2]["encrypted_content"] == "enc:sol:1" and second[3]["id"] == "fc_1"


async def test_other_model_items_are_sent_without_reasoning_or_ids() -> None:
    fake = FakeAzureResponses()
    luna = model(fake, deployment="luna")
    r1 = await luna.chat([{"role": "user", "content": "q"}], [{"type": "function", "function": {"name": "t", "parameters": {}}}])
    msgs = [{"role": "user", "content": "q"}, r1.to_message(), {"role": "tool", "tool_call_id": r1.tool_calls[0].id, "content": "1"}]
    assert msgs[1]["_resp_items"][0]["type"] == "reasoning"
    sol = model(fake, deployment="sol")  # 대화 중 승격: luna 가 만든 추론은 sol 에 보내지 않음
    r2 = await sol.chat(msgs, [{"type": "function", "function": {"name": "t", "parameters": {}}}])
    sent = fake.requests[-1][1]["input"]
    assert r2.content == "42" and not any(i.get("type") == "reasoning" for i in sent)
    fc = next(i for i in sent if i.get("type") == "function_call")
    assert "id" not in fc and fc["call_id"] == r1.tool_calls[0].id
    # Chat Completions 로 만든 예전 대화도 그대로 이어짐 (항목 원본 없음)
    old = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
           {"role": "assistant", "content": "", "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "call_x", "content": "1"}]
    assert (await sol.chat(old)).content == "42"


async def test_turns_off_reasoning_replay_when_deployment_rejects_it() -> None:
    fake = FakeAzureResponses(reject_include=True)
    m = model(fake)
    line = await llm_roundtrip(m)
    assert line.startswith("✓") and "추론 항목 없음" in line, line
    assert "include" in fake.requests[0][1] and all("include" not in b for _, b in fake.requests[1:])
    assert m._replay_ok is False  # 이후로는 처음부터 보내지 않음


async def test_agent_run_with_responses_api(tmp_path: Path) -> None:
    fake = FakeAzureResponses(tool="get_current_time")
    cfg = load_config(write_config(tmp_path, "  api: responses"))
    assert cfg.llm.api == "responses"
    llm = AzureResponsesModel(cfg.llm, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("지금 몇 시야?")
    assert r.success and r.output == "42" and [e.function for e in r.tool_events] == ["get_current_time"]
    second = fake.requests[1][1]
    assert any(i.get("type") == "reasoning" for i in second["input"]) and second["prompt_cache_key"].startswith("c-")
    assert "(보낸 시각:" in json.dumps(second["input"][1], ensure_ascii=False)  # 사용자 메시지 끝의 시각도 그대로
    assert r.usage["cached_tokens"] == 1024 and r.usage["reasoning_tokens"] == 40
    assert any(m.get("_resp_items") for m in r.messages if m.get("role") == "assistant")
    await llm.aclose()


def test_urls_config_and_parsing() -> None:
    base = dict(api_key="k", deployment="sol", api="responses")
    assert responses_base_url(LLMConfig(endpoint="https://r.openai.azure.com/", **base)) == "https://r.openai.azure.com/openai/v1"
    assert responses_base_url(LLMConfig(endpoint="https://r.openai.azure.com/openai/v1", **base)) == "https://r.openai.azure.com/openai/v1"
    gw = LLMConfig(base_url="https://apim.corp/aoai/openai/deployments/sol", **base)  # 게이트웨이 주소에서 …/openai/v1
    assert responses_base_url(gw) == "https://apim.corp/aoai/openai/v1"
    assert responses_base_url(LLMConfig(endpoint="https://r", responses_api_version="2025-04-01-preview", **base)) is None
    assert responses_base_url(LLMConfig(endpoint="https://r", responses_base_url="https://x/y/", **base)) == "https://x/y"
    with pytest.raises(ConfigError):
        LLMConfig(endpoint="https://r", api_key="k", deployment="sol", api="assistants").validate()
    assert type(make_chat_model(LLMConfig(endpoint="https://r", **base))).__name__ == "AzureResponsesModel"
    assert type(make_chat_model(LLMConfig(endpoint="https://r", api_key="k", deployment="sol"))).__name__ == "AzureChatModel"

    cut = parse_response({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                          "output": [{"type": "reasoning", "id": "rs_1", "summary": []}]}, "s")
    assert cut.finish_reason == "length" and cut.content is None and cut.raw_items is None  # 암호화 내용 없는 추론은 버림
    refused = parse_response({"status": "completed", "output": [{"type": "message", "id": "m", "role": "assistant",
                                                                 "content": [{"type": "refusal", "refusal": "안 됩니다"}]}]}, "s")
    assert "거절" in refused.content
    noenc = parse_response({"status": "completed", "output": [
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "t", "arguments": "{}"}]}, "s")
    assert noenc.raw_items == [{"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"}]  # 짝 검사 피하게 id 도 뺌
    with pytest.raises(RuntimeError, match="server_error"):
        parse_response({"error": {"code": "server_error", "message": "x"}}, "s")


def test_input_conversion_images_and_unexecuted_calls() -> None:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "이거"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    items, _ = responses_input(msgs, True, "s")
    assert items[0]["content"] == [{"type": "input_text", "text": "이거"},
                                   {"type": "input_image", "image_url": "data:image/png;base64,AAA", "detail": "auto"}]
    items, _ = responses_input(msgs, False, "s")  # 이미지 못 보는 배포면 텍스트 안내로
    assert items[0]["content"][0]["type"] == "input_text" and "이미지" in items[0]["content"][0]["text"]
    # 마지막 정리(tool_choice=none)에서 막은 도구 호출은 항목 원본에서도 빠짐 → 결과 없는 호출 오류 방지
    r = LLMResponse(content="정리", tool_calls=[], raw_items=[
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "e"}, {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "t", "arguments": "{}"},
        {"type": "message", "id": "m1", "role": "assistant", "content": [{"type": "output_text", "text": "정리"}]}], raw_source="s")
    r.tool_calls = []
    assert [i["type"] for i in r.to_message()["_resp_items"]] == ["reasoning", "message"]
    kept = LLMResponse(content=None, tool_calls=[ToolCall("c1", "t", "{}")], raw_items=r.raw_items, raw_source="s").to_message()
    assert len(kept["_resp_items"]) == 3


async def test_dated_api_version_url() -> None:
    fake = FakeAzureResponses()
    m = model(fake, responses_api_version="2025-04-01-preview")
    assert (await m.chat([{"role": "user", "content": "q"}])).content == "OK"
    url = fake.requests[0][0]
    assert url.startswith("https://corp.openai.azure.com/openai/responses?") and "api-version=2025-04-01-preview" in url


def test_per_model_api_override_in_pool(tmp_path: Path) -> None:
    from test_routing import MODELS

    from inhouse_agent.routing import ModelPool, ModelRouter
    import yaml

    raw = yaml.safe_load("llm:\n" + MODELS.replace("{name: sol, deployment: sol,", "{name: sol, deployment: sol, api: responses,"))["llm"]
    cfg = LLMConfig(endpoint="https://r", api_key="k", deployment="sol")
    pool = ModelPool(cfg, ModelRouter(raw, "sol"))
    assert type(pool._client("sol")[1]).__name__ == "AzureResponsesModel"
    assert type(pool._client("luna")[1]).__name__ == "AzureChatModel"
