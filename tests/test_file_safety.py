"""파일 안전장치 (opencode 에서 가져옴): 덮어쓰기 전 원본 보관·되돌리기, 읽은 뒤 바뀐 파일 덮어쓰기 막기, 작성 후 자동 검증."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.interaction import RunContext, current_run
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime


async def test_overwrite_keeps_versions_and_restore(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        call = rt.hub.call
        assert not (await call("save_file", {"path": "보고/메모.txt", "content": "첫 판"})).is_error
        assert "이전 버전이 없습니다" in (await call("file_versions", {"path": "files/보고/메모.txt"})).text
        await call("save_file", {"path": "보고/메모.txt", "content": "둘째 판"})
        await call("save_file", {"path": "보고/메모.txt", "content": "셋째 판"})
        vs = (await call("file_versions", {"path": "files/보고/메모.txt"})).data["versions"]
        assert [Path(v["path"]).read_text(encoding="utf-8") for v in vs] == ["둘째 판", "첫 판"]  # 최근 순
        assert "/.versions/files/보고/메모.txt/" in vs[0]["path"].replace("\\", "/")
        r = await call("restore_file_version", {"path": "files/보고/메모.txt", "version": "2"})
        assert not r.is_error and "되돌림" in r.text
        f = Path(rt.workspace.root) / "files" / "보고" / "메모.txt"
        assert f.read_text(encoding="utf-8") == "첫 판"
        vs = (await call("file_versions", {"path": "files/보고/메모.txt"})).data["versions"]
        assert Path(vs[0]["path"]).read_text(encoding="utf-8") == "셋째 판"  # 되돌리기 전 내용도 보관 → 되돌리기를 되돌릴 수 있음
        for i in range(12):  # 파일마다 최근 10개만
            await call("save_file", {"path": "보고/메모.txt", "content": f"판 {i}"})
        assert len((await call("file_versions", {"path": "files/보고/메모.txt"})).data["versions"]) == 10


async def test_refuses_to_overwrite_file_changed_after_reading(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        call = rt.hub.call
        await call("save_file", {"path": "공유/집계.csv", "content": "부서,금액\n영업,10\n"})
        f = Path(rt.workspace.root) / "files" / "공유" / "집계.csv"
        tok = current_run.set(RunContext(ask=None, interactive=False))
        try:
            assert not (await call("read_file_text", {"path": "files/공유/집계.csv"})).is_error
            # 같은 실행에서 읽고 → 쓰고 → 또 쓰는 것은 정상
            assert not (await call("save_file", {"path": "공유/집계.csv", "content": "부서,금액\n영업,11\n"})).is_error
            assert not (await call("save_file", {"path": "공유/집계.csv", "content": "부서,금액\n영업,12\n"})).is_error
            await call("read_file_text", {"path": "files/공유/집계.txt"})  # 없는 파일 읽기는 상관없음
        finally:
            current_run.reset(tok)
        tok = current_run.set(RunContext(ask=None, interactive=False))
        try:
            await call("read_file_text", {"path": "files/공유/집계.csv"})
            f.write_text("부서,금액\n영업,99\n", encoding="utf-8")  # 다른 사람이 그사이 고침
            later = time.time() + 5
            os.utime(f, (later, later))
            r = await call("save_file", {"path": "공유/집계.csv", "content": "덮어쓰기"})
            assert r.is_error and "읽은 뒤 다른 곳에서 바뀌었습니다" in r.text and "다른 이름으로 저장" in r.text
            assert f.read_text(encoding="utf-8") == "부서,금액\n영업,99\n"  # 남의 변경이 보존됨
            assert not (await call("save_file", {"path": "공유/집계_새.csv", "content": "x"})).is_error  # 다른 이름은 됨
        finally:
            current_run.reset(tok)


async def test_agent_checks_files_it_makes(tmp_path: Path) -> None:
    """에이전트가 만든 PPT·엑셀을 그 도구 결과에서 바로 검사 → 모델이 같은 차례에 고칠 수 있음."""
    from test_office_tools import messy_deck

    from inhouse_agent.verify import check_excel

    cfg = load_config(write_config(tmp_path))
    good = {"path": "보고/정상.xlsx", "sheets": [{"name": "요약", "columns": ["부서", "금액"], "rows": [["영업", 10]]}]}
    bad = {"path": "보고/깨짐.xlsx", "sheets": [{"name": "요약", "columns": ["부서", "금액"], "rows": [["영업", "#REF!"]]}]}
    llm = ScriptedLLM([
        LLMResponse(None, [ToolCall("x1", "write_excel", json.dumps(good, ensure_ascii=False)),
                           ToolCall("x2", "write_excel", json.dumps(bad, ensure_ascii=False))]),
        LLMResponse(None, [ToolCall("p1", "edit_pptx", json.dumps({"path": "files/원본.pptx", "output": "보고/수정.pptx",
                                                                  "replace": {"정상 장": "정상 장"}}, ensure_ascii=False))]),
        LLMResponse(None, [ToolCall("c1", "check_pptx_layout", json.dumps({"path": "files/보고/수정.pptx"}))]),
        LLMResponse("끝"),
    ])
    async with AgentRuntime(cfg, llm=llm) as rt:
        messy_deck(Path(rt.workspace.files) / "원본.pptx")
        r = await rt.agent.run("보고서 만들어줘")
    res = {e.function + e.arguments.get("path", ""): e.result for e in r.tool_events}
    assert "[자동 검사] 정상.xlsx: 이상 없음" in res["write_excel보고/정상.xlsx"]
    assert "[자동 검사] 깨짐.xlsx: 문제 1건" in res["write_excel보고/깨짐.xlsx"] and "B2=#REF!" in res["write_excel보고/깨짐.xlsx"]
    ppt = res["edit_pptxfiles/원본.pptx"]
    assert "[자동 검사] 수정.pptx: 레이아웃 문제" in ppt and "#1" in ppt
    assert "[자동 검사]" not in res["check_pptx_layoutfiles/보고/수정.pptx"]  # 검사 도구 자체에는 붙이지 않음
    broken = Path(tmp_path) / "broken.xlsx"
    broken.write_bytes(b"not a zip")
    assert not check_excel(broken)["ok"] and "열리지 않습니다" in check_excel(broken)["issues"][0]


async def test_auto_verify_can_be_turned_off(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    cfg.agent.auto_verify = False
    args = {"path": "a.xlsx", "sheets": [{"name": "s", "columns": ["a"], "rows": [["#REF!"]]}]}
    llm = ScriptedLLM([LLMResponse(None, [ToolCall("x1", "write_excel", json.dumps(args))]), LLMResponse("끝")])
    async with AgentRuntime(cfg, llm=llm) as rt:
        r = await rt.agent.run("엑셀")
    assert "[자동 검사]" not in r.tool_events[0].result
