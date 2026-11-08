"""여러 사용자가 동시에 쓸 때: 공용 다운로드 폴더 파일이 다른 사람 폴더로 가지 않음, 코드 실행은 편집자만."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from test_multiuser_skills import _cfg

from inhouse_agent.config import load_config
from inhouse_agent.mcp_hub import BuiltinTool, UserBaton
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.users import current_user


async def test_concurrent_users_do_not_claim_each_others_downloads(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        shared = rt.base_workspace.downloads  # MCP(웹컨트롤)가 저장하는 공용 다운로드 폴더

        def maker(name: str, delay: float):
            async def handler(args: dict) -> str:
                await asyncio.sleep(delay)
                (shared / name).write_text("부서,금액\n영업,100\n", encoding="utf-8")
                await asyncio.sleep(0.3)
                return f"{name} 내려받기 완료"
            return handler

        rt.hub.set_custom_tools([BuiltinTool("erp", "ERP", {"type": "object"}, maker("erp_a.csv", 0.2)),
                                 BuiltinTool("gw", "그룹웨어", {"type": "object"}, maker("gw_b.csv", 0.1))])

        async def as_user(uid: str, tool: str):
            tok = current_user.set(uid)
            try:
                return await rt.hub.call(tool, {})
            finally:
                current_user.reset(tok)

        a, b = await asyncio.gather(as_user("kim", "custom__erp"), as_user("lee", "custom__gw"))
        kim = sorted(p.name for p in rt.users.get("kim").downloads.iterdir())
        lee = sorted(p.name for p in rt.users.get("lee").downloads.iterdir())
        assert kim == ["erp_a.csv"] and lee == ["gw_b.csv"], (kim, lee, a.text, b.text)
        assert [Path(f["path"]).name for f in a.files] == ["erp_a.csv"] and [Path(f["path"]).name for f in b.files] == ["gw_b.csv"]


async def test_baton_keeps_same_user_parallel_and_is_fair() -> None:
    baton = UserBaton()
    log: list[tuple[str, str, float]] = []
    t0 = time.monotonic()

    async def job(user: str, name: str, dur: float, start: float = 0) -> None:
        await asyncio.sleep(start)
        async with baton.hold(user):
            log.append((name, "in", round(time.monotonic() - t0, 2)))
            await asyncio.sleep(dur)
            log.append((name, "out", round(time.monotonic() - t0, 2)))

    # kim 의 두 호출은 동시에, lee 는 kim 이 끝난 뒤, lee 가 기다리는 동안 들어온 kim 의 세 번째 호출은 lee 다음
    await asyncio.gather(job("kim", "k1", 0.3), job("kim", "k2", 0.3), job("lee", "l1", 0.2, 0.05), job("kim", "k3", 0.1, 0.1))
    order = [n for n, ev, _ in log if ev == "in"]
    assert order[:2] == ["k1", "k2"] and order[2:] == ["l1", "k3"], log
    t = {(n, ev): ts for n, ev, ts in log}
    assert t[("k2", "in")] < t[("k1", "out")]  # 같은 사용자는 동시에
    assert t[("l1", "in")] >= t[("k2", "out")] and t[("k3", "in")] >= t[("l1", "out")]


async def test_nested_tool_call_inside_dynamic_tool_does_not_deadlock(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        async def outer(args: dict) -> str:
            inner = await rt.hub.call("custom__inner", {})  # 동적 도구가 다른 도구를 부르는 경우 (ctx.call_tool)
            return "outer+" + inner.text

        async def inner(args: dict) -> str:
            await asyncio.sleep(0.05)
            return "inner"

        rt.hub.set_custom_tools([BuiltinTool("outer", "o", {"type": "object"}, outer),
                                 BuiltinTool("inner", "i", {"type": "object"}, inner)])

        async def as_user(uid: str, tool: str):
            tok = current_user.set(uid)
            try:
                return await rt.hub.call(tool, {})
            finally:
                current_user.reset(tok)

        res = await asyncio.wait_for(asyncio.gather(as_user("kim", "custom__outer"), as_user("lee", "custom__inner")), 5)
        assert res[0].text.startswith("outer+inner") and res[1].text.startswith("inner")


async def test_run_python_is_editor_only(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path, "skills:\n  editors: [kim]\ndynamic_tools:\n  mode: auto\n"))
    async with AgentRuntime(cfg) as rt:
        code = {"code": "result = 42\n"}
        tok = current_user.set("lee")
        try:
            out = await rt.hub.call("run_python", code)
        finally:
            current_user.reset(tok)
        assert out.is_error and "권한" in out.text
        tok = current_user.set("kim")
        try:
            out = await rt.hub.call("run_python", code)
        finally:
            current_user.reset(tok)
        assert not out.is_error and "42" in out.text
