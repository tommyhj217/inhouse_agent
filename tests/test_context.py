"""토큰 기준 자동 압축: 도구 결과 줄이기 → 요약, 컨텍스트 초과 오류 재시도, 도구 호출/결과 짝 유지."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.context import SUMMARY_PROMPT, ContextManager, estimate_text, is_overflow_error
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime


def pairs_ok(messages: list[dict[str, Any]]) -> bool:
    """모든 tool 결과가 바로 앞 assistant 의 tool_call id 와 짝이 맞는지 (API 가 요구하는 형식)."""
    open_ids: set[str] = set()
    for m in messages:
        if m["role"] == "assistant":
            if open_ids:
                return False
            open_ids = {tc["id"] for tc in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            if m["tool_call_id"] not in open_ids:
                return False
            open_ids.discard(m["tool_call_id"])
        elif open_ids:
            return False
    return not open_ids


def test_estimate_and_overflow_detection() -> None:
    assert 80 <= estimate_text("가" * 100) <= 100  # 한글: 글자당 약 0.9
    assert 25 <= estimate_text("a" * 100) <= 30  # 영문: 약 3.6자당 1
    assert is_overflow_error(Exception("Error code: 400 - This model's maximum context length is 128000 tokens (context_length_exceeded)"))
    assert not is_overflow_error(Exception("rate limit"))


def _write(rt: AgentRuntime, name: str, n: int) -> str:
    p = Path(rt.workspace.files) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(("매출 보고서 본문 " * (n // 9 + 1))[:n], encoding="utf-8")
    return str(p)


async def test_old_tool_outputs_are_masked_before_limit(tmp_path: Path) -> None:
    from inhouse_agent.context import estimate

    cfg = load_config(write_config(tmp_path, ""))
    cfg.agent.max_tool_result_chars = 200000
    llm = ScriptedLLM([])
    async with AgentRuntime(cfg, llm=llm) as rt:
        # 기본(시스템 프롬프트+도구 설명) 크기에 맞춰: 파일 1개는 한도 안, 2개째에서 75% 를 넘고, 1개를 줄이면 50% 아래
        base = estimate(rt.agent.new_conversation(), rt.hub.openai_tools())
        f_tokens = base + 8000
        cfg.agent.context_window = 2 * (base + f_tokens + 2000)
        from inhouse_agent.context import estimate_text

        sample = "매출 보고서 본문 " * 1000
        chars = int(f_tokens / (estimate_text(sample) / len(sample)))
        assert chars <= 30000, chars  # read_file_text 한 번에 읽는 최대 글자 수
        a, b = _write(rt, "a.txt", chars), _write(rt, "b.txt", chars)
        llm.responses = [
            LLMResponse(content=None, tool_calls=[ToolCall("c1", "read_file_text", json.dumps({"path": a, "max_chars": 30000}))]),
            LLMResponse(content=None, tool_calls=[ToolCall("c2", "read_file_text", json.dumps({"path": b, "max_chars": 30000}))]),
            LLMResponse(content="두 파일 정리 완료"),
        ]
        events: list[tuple[str, dict]] = []
        res = await rt.agent.run("a, b 파일을 읽고 정리해줘", on_event=lambda k, d: events.append((k, d)))
        assert res.success and res.output == "두 파일 정리 완료"
        compacts = [d for k, d in events if k == "compact"]
        assert compacts and compacts[0]["masked"] == 1 and compacts[0]["after"] < compacts[0]["before"]
        last_call = llm.calls[-1]
        tools = [m for m in last_call if m["role"] == "tool"]
        assert tools[0]["content"].startswith("[자동 압축") and "a.txt" in tools[0]["content"]  # 파일 경로는 남김
        assert not tools[1]["content"].startswith("[자동 압축")  # 방금 읽은 결과는 그대로
        assert pairs_ok(last_call[1:])


class SmartLLM:
    """요약 요청에는 요약을, 처음 일반 호출에는 '컨텍스트 초과' 오류를, 그다음엔 답을 주는 가짜 LLM."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.overflowed = False

    async def chat(self, messages, tools=None, **kw):
        self.calls.append([dict(m) for m in messages])
        if messages[0]["content"] == SUMMARY_PROMPT:
            assert "3월 매출 합계 1,234,567원" in messages[1]["content"]
            return LLMResponse(content="사용자는 3월 매출 보고서를 원함. 확인: 3월 매출 합계 1,234,567원. 파일: files/3월.xlsx")
        if not self.overflowed:
            self.overflowed = True
            raise RuntimeError("Error code: 400 - This model's maximum context length is 8000 tokens (context_length_exceeded)")
        return LLMResponse(content="이어서 정리했습니다", usage={"prompt_tokens": 900, "completion_tokens": 10, "total_tokens": 910})


async def test_long_history_is_summarized_and_overflow_retried(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path, ""))
    cfg.agent.context_window = 8000
    llm = SmartLLM()
    async with AgentRuntime(cfg, llm=llm) as rt:
        msgs = rt.agent.new_conversation()
        for i in range(12):  # 이전에 했던 긴 작업 (도구 호출 포함)
            msgs.append({"role": "user", "content": f"{i}번째 요청: 3월 매출을 조회해줘"})
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"t{i}", "type": "function", "function": {"name": "query_table", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "3월 매출 합계 1,234,567원 " + "행 데이터 " * 150})
            msgs.append({"role": "assistant", "content": "3월 매출 합계는 1,234,567원입니다."})
        events: list[tuple[str, dict]] = []
        res = await rt.agent.run("그 결과를 부서별로 나눠줘", messages=msgs, on_event=lambda k, d: events.append((k, d)))
        assert res.success and res.output == "이어서 정리했습니다"
        assert any(k == "compact" and d["summarized"] for k, d in events)
        summary = next(m for m in msgs if m.get("_summary"))
        assert "1,234,567원" in summary["content"] and msgs[1] is summary
        prompt = "그 결과를 부서별로 나눠줘"  # 현재 요청은 남긴 최근 부분에 있거나 요약에 원문으로 붙음
        assert any(m.get("content") == prompt for m in msgs[2:]) or prompt in summary["content"]
        assert pairs_ok(msgs[1:]) and res.usage["total_tokens"] >= 910
        assert sum(1 for c in llm.calls if c[0]["content"] == SUMMARY_PROMPT) >= 1


async def test_summary_keeps_current_request_when_cut_inside_turn() -> None:
    cm = ContextManager(window=4000, compact_at=0.5)
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "user", "content": "엑셀 3개를 비교해줘 (영업부만)"}]
    for i in range(4):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"x{i}", "type": "function", "function": {"name": "analyze_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"x{i}", "content": "분석 결과 " * 400})

    class L:
        async def chat(self, messages, tools=None, **kw):
            return LLMResponse(content="요약: 파일 3개 분석 중")

    info = await cm.compact(L(), msgs, force=True)
    assert info and info["summarized"] > 0
    assert msgs[1]["_summary"] and "엑셀 3개를 비교해줘 (영업부만)" in msgs[1]["content"]  # 현재 요청 원문 유지
    assert msgs[2]["role"] == "assistant" and pairs_ok(msgs[1:])
