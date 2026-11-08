"""하위 에이전트(run_subagent): 별도 대화, 동시 실행, 깊이 1, 사용량 합산, LLM 없을 때 안내."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from test_agent import write_config
from test_multiuser_skills import _cfg

from inhouse_agent.config import load_config
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.users import current_user


class RouterLLM:
    """상위/하위 대화를 시스템 프롬프트로 구분해 응답하는 가짜 LLM."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.active = 0
        self.max_active = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls.append([dict(m) for m in messages])
        sub = "하위 작업자 역할" in messages[0]["content"]
        has_tool = any(m["role"] == "tool" for m in messages)
        if sub:
            task = next(m["content"] for m in messages if m["role"] == "user")
            if not has_tool:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.3)  # 두 하위 작업이 겹치는지 확인
                self.active -= 1
                # 하위 작업자가 또 하위 에이전트를 부르려 하면 거절되어야 함
                return LLMResponse(content=None, tool_calls=[
                    ToolCall("s1", "get_current_time", "{}"),
                    ToolCall("s2", "run_subagent", json.dumps({"task": "더 쪼개기"}))],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
            refused = any("다시 부를 수 없습니다" in str(m.get("content")) for m in messages if m["role"] == "tool")
            return LLMResponse(content=f"결과: {task[:6]} 합계 100 (재호출 거절={refused})",
                               usage={"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220})
        if not has_tool:
            return LLMResponse(content=None, tool_calls=[
                ToolCall("p1", "run_subagent", json.dumps({"task": "ERP 3월 매출 조회"})),
                ToolCall("p2", "run_subagent", json.dumps({"task": "그룹웨어 3월 공지 정리"}))],
                usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55})
        return LLMResponse(content="두 결과를 합쳤습니다", usage={"prompt_tokens": 60, "completion_tokens": 6, "total_tokens": 66})


async def test_subagents_run_in_parallel_with_own_context(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    llm = RouterLLM()
    async with AgentRuntime(cfg, llm=llm) as rt:
        events: list[tuple[str, dict]] = []
        tok = current_user.set("kim")
        t0 = time.monotonic()
        try:
            res = await rt.agent.run("ERP 매출과 그룹웨어 공지를 같이 정리해줘", on_event=lambda k, d: events.append((k, d)))
        finally:
            current_user.reset(tok)
        assert res.success and res.output == "두 결과를 합쳤습니다"
        assert llm.max_active == 2 and time.monotonic() - t0 < 2  # 두 하위 작업이 동시에
        parent_tools = [m["content"] for m in llm.calls[-1] if m["role"] == "tool"]
        assert any("결과: ERP 3월" in c for c in parent_tools) and any("결과: 그룹웨어 " in c for c in parent_tools)
        assert all("재호출 거절=True" in c for c in parent_tools)  # 깊이 1
        # 하위 대화는 상위 대화를 보지 않음 (별도 컨텍스트)
        sub_first = next(c for c in llm.calls if "하위 작업자 역할" in c[0]["content"])
        assert not any("같이 정리해줘" in str(m.get("content")) for m in sub_first)
        # 사용량: 상위 55+66 + 하위 2개 × (110+220)
        assert res.usage["total_tokens"] == 55 + 66 + 2 * (110 + 220)
        subs = [d for k, d in events if k == "subagent"]
        assert {d["event"] for d in subs} >= {"start", "tool_end", "done"} and len({d["sid"] for d in subs}) == 2


async def test_subagent_needs_llm_and_task(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path))  # provider: none (토큰 없이)
    async with AgentRuntime(cfg) as rt:
        out = await rt.hub.call("run_subagent", {"task": "x"})
        assert out.is_error and "LLM" in out.text
    (tmp_path / "b").mkdir()
    cfg2 = load_config(write_config(tmp_path / "b"))
    cfg2.agent.subagents = False
    async with AgentRuntime(cfg2, llm=RouterLLM()) as rt2:
        assert rt2.hub.route_of("run_subagent") is None  # 설정으로 끌 수 있음


async def test_subagent_gets_files_as_attachments(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    seen: list[str] = []

    class L:
        async def chat(self, messages, tools=None, **kw):
            if "하위 작업자 역할" in messages[0]["content"]:
                seen.append(messages[-1]["content"])
                return LLMResponse(content="파일 확인 완료")
            if any(m["role"] == "tool" for m in messages):
                return LLMResponse(content="끝")
            return LLMResponse(content=None, tool_calls=[ToolCall("p", "run_subagent", json.dumps({"task": "a.csv 요약", "files": [str(f)]}))])

    async with AgentRuntime(cfg, llm=L()) as rt:
        f = Path(rt.workspace.files) / "a.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("부서,금액\n영업,100\n", encoding="utf-8")
        res = await rt.agent.run("a.csv 를 하위 작업으로 요약")
        assert res.success and seen and "[첨부 파일 1개" in seen[0] and str(f) in seen[0]
