"""스킬 러너: Claude Code 로 만든 스킬·스크립트가 든 Git 저장소를 받아 설치하고, 실행하고, 테스트 사례를 돌린다."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.llm import LLMResponse, ToolCall
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.skill_tests import check, load_cases

BOM_MD = """---
name: bom-check
description: 모델의 BOM 을 점검한다. "BOM 점검", "부품 확인" 요청에 사용.
argument-hint: <모델명> [색상]
allowed-tools: Bash(python:*), Read
---

# BOM 점검

모델: $0 / 색상: $ARGUMENTS[1] / 전체 인자: $ARGUMENTS

1. Bash 로 점검 스크립트를 실행한다:
   `python ${CLAUDE_SKILL_DIR}/scripts/check.py $0`
2. 결과의 problems 를 사용자에게 정리해 준다. 규칙은 Read 로 reference/rules.md 를 읽는다.
"""

CHECK_PY = '''import json, os, sys
model = sys.argv[1] if len(sys.argv) > 1 else ""
skill_dir = os.environ.get("CLAUDE_SKILL_DIR", "")
rules = open(os.path.join(skill_dir, "reference", "rules.md"), encoding="utf-8").read().strip()
if not model:
    print("모델명이 필요합니다", file=sys.stderr)
    sys.exit(2)
print(json.dumps({"model": model, "problems": [] if model.startswith("FW") else ["모델명 규칙 위반"], "rules": rules,
                  "version": VERSION}, ensure_ascii=False))
'''

CASES = """cases:
  - name: 정상 모델은 문제 없음
    command: python scripts/check.py FW2409W.AKOR
    expect: {contains: ["FW2409W.AKOR"], json: {problems: [], version: 1}}
  - name: 규칙에 안 맞는 모델을 잡는다
    command: python ${CLAUDE_SKILL_DIR}/scripts/check.py XX1
    expect: {contains: ["모델명 규칙 위반"]}
  - name: 모델명이 없으면 실패한다
    command: python scripts/check.py
    expect: {error: true}
  - name: 말로 시켜도 된다
    prompt: FW2409W.AKOR 의 BOM 을 점검해줘
    expect: {contains: ["문제"]}
"""

EVALS = {"skill_name": "weekly", "evals": [{"id": "e1", "prompt": "이번 주 주간 보고 초안", "expected_output": "주간 보고 초안이 나온다",
                                            "expectations": ["제목에 주차가 들어간다"]}]}


def make_repo(root: Path, version: int = 1) -> Path:
    """Claude Code 프로젝트 모양: .claude/skills/<스킬>/, .claude/commands/**/*.md"""
    sk = root / ".claude" / "skills"
    (sk / "bom-check" / "scripts").mkdir(parents=True, exist_ok=True)
    (sk / "bom-check" / "reference").mkdir(exist_ok=True)
    (sk / "bom-check" / "tests").mkdir(exist_ok=True)
    (sk / "bom-check" / "SKILL.md").write_text(BOM_MD, encoding="utf-8")
    (sk / "bom-check" / "scripts" / "check.py").write_text(f"VERSION = {version}\n" + CHECK_PY, encoding="utf-8")
    (sk / "bom-check" / "reference" / "rules.md").write_text("모델명은 FW 로 시작", encoding="utf-8")
    (sk / "bom-check" / "tests" / "cases.yaml").write_text(CASES, encoding="utf-8")
    (sk / "weekly" / "evals").mkdir(parents=True, exist_ok=True)
    (sk / "weekly" / "SKILL.md").write_text("---\nname: weekly\n---\n\n# 주간 보고\n\n이번 주 한 일을 모아 주간 보고 초안을 만든다.\n", encoding="utf-8")
    (sk / "weekly" / "evals" / "evals.json").write_text(json.dumps(EVALS, ensure_ascii=False), encoding="utf-8")
    cmd = root / ".claude" / "commands" / "review"
    cmd.mkdir(parents=True, exist_ok=True)
    (cmd / "summary.md").write_text("변경 내용을 세 줄로 요약한다. 대상: $ARGUMENTS\n", encoding="utf-8")
    return root


def git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)


async def test_sync_run_and_test_claude_code_repo(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "v1")
    url = f"file://{repo.as_posix()}"
    cfg = load_config(write_config(tmp_path))
    cfg.raw.setdefault("skills", {})["sources"] = [{"url": url, "ref": "main"}]
    llm = ScriptedLLM([])
    async with AgentRuntime(cfg, llm=llm) as rt:
        # 1) Git 에서 받아 설치: 스킬 2개 + 슬래시 명령 1개(스킬로 변환)
        rep = rt.sync_skills()[0]
        assert sorted(rep["installed"]) == ["bom-check", "review-summary", "weekly"] and not rep["errors"], rep
        bom = rt.skills.get("bom-check")
        assert bom.info["source"]["url"] == url and len(bom.info["source"]["commit"]) == 40
        assert [i.name for i in bom.inputs] == ["arguments"] and bom.inputs[0].label == "<모델명> [색상]"  # $ARGUMENTS → 입력 칸
        assert rt.skills.get("weekly").description.startswith("이번 주 한 일을 모아")  # 설명이 없으면 본문 첫 문단
        assert "세 줄로 요약" in rt.skills.get("review-summary").description
        assert (bom.path / "scripts" / "check.py").is_file() and (bom.path / "tests" / "cases.yaml").is_file()

        # 2) Claude Code 문법이 채워짐: $0, $ARGUMENTS[1], $ARGUMENTS, ${CLAUDE_SKILL_DIR}
        text = bom.render({}, "Asia/Seoul", "FW2409W.AKOR 화이트")
        assert "모델: FW2409W.AKOR / 색상: 화이트 / 전체 인자: FW2409W.AKOR 화이트" in text
        assert f"python {bom.path.resolve().as_posix()}/scripts/check.py FW2409W.AKOR" in text

        # 3) LLM 이 스킬 지침대로 스크립트를 실행 (Bash → run_skill_script 안내가 붙음)
        cmd = f"python {bom.path.resolve().as_posix()}/scripts/check.py FW2409W.AKOR"
        llm.responses += [LLMResponse(None, [ToolCall("c1", "run_skill_script", json.dumps({"name": "bom-check", "command": cmd}))]),
                          LLMResponse("문제 없습니다")]
        res, _ = await rt.run_skill("bom-check", {"arguments": "FW2409W.AKOR"}, save=False)
        system = llm.calls[0][0]["content"]
        assert "[도구 이름 대응]" in system and "run_skill_script(command=" in system and "모델: FW2409W.AKOR" in system
        assert res.success and '"problems": []' in res.tool_events[0].result and "모델명은 FW 로 시작" in res.tool_events[0].result  # CLAUDE_SKILL_DIR 로 참고 파일을 읽음

        # 4) 테스트 러너: 스크립트 사례는 LLM 없이 실행, prompt 사례는 건너뜀
        out = await rt.hub.call("test_skill", {"name": "bom-check"})
        r = out.data["results"][0]
        assert (r["passed"], r["failed"], r["skipped"]) == (3, 0, 1) and not out.is_error, out.text
        assert "✅ bom-check: 통과 3 · 실패 0 · 건너뜀 1" in out.text and "– 말로 시켜도 된다 [LLM]" in out.text
        assert rt.skills.get("bom-check").info["last_test"]["passed"] == 3
        # prompt 사례도 실행 (llm=true): 스킬을 LLM 으로 돌려 결과를 기대와 비교
        llm.responses += [LLMResponse("점검 결과 문제 없음")]
        out = await rt.hub.call("test_skill", {"name": "bom-check", "llm": True, "only": "말로"})
        assert out.data["results"][0]["passed"] == 1
        # evals.json (skill-creator 형식): 글로 적힌 기대는 사람이 보도록 함께 보여줌
        llm.responses += [LLMResponse("3주차 주간 보고 초안")]
        out = await rt.hub.call("test_skill", {"name": "weekly", "llm": True})
        assert out.data["results"][0]["passed"] == 1 and "사람이 확인할 기대: 주간 보고 초안이 나온다 / 제목에 주차가 들어간다" in out.text

        # 5) 저장소가 바뀜 (스크립트 v2) → 다시 받으면 갱신, 테스트가 깨진 것을 잡음
        make_repo(repo, version=2)
        git(repo, "commit", "-qam", "v2")
        rep = rt.sync_skills()[0]
        assert rep["updated"] == ["bom-check", "review-summary", "weekly"] and not rep["installed"]
        out = await rt.hub.call("test_skill", {"name": "bom-check"})
        assert out.is_error and "json version: 기대 1, 실제 2" in out.text and "✗ 정상 모델은 문제 없음" in out.text
        assert rt.sync_skills()[0]["unchanged"] == ["bom-check", "review-summary", "weekly"]  # 커밋이 같으면 그대로

        # 6) 직접 만든 같은 이름의 스킬은 덮어쓰지 않음
        rt.skills.delete("weekly")
        rt.skills.save("weekly", "---\nname: weekly\ndescription: 우리 팀이 직접 만든 주간 보고\n---\n본문\n", "kim")
        rep = rt.sync_skills()[0]
        assert any(x.startswith("weekly (다른 곳에서 만든") for x in rep["skipped"])
        assert rt.skills.get("weekly").description == "우리 팀이 직접 만든 주간 보고"


def test_cli_sync_and_test_exit_codes(tmp_path: Path, capsys) -> None:
    from inhouse_agent.cli import main

    repo = make_repo(tmp_path / "repo")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "v1")
    cfg = str(write_config(tmp_path))
    assert main(["--config", cfg, "skills", "sync", f"file://{repo.as_posix()}", "--test"]) == 0
    out = capsys.readouterr().out
    assert "새로 설치: bom-check, review-summary, weekly" in out and "✅ bom-check: 통과 3" in out
    make_repo(repo, version=3)
    git(repo, "commit", "-qam", "v3")
    assert main(["--config", cfg, "skills", "sync", f"file://{repo.as_posix()}"]) == 0
    assert main(["--config", cfg, "skills", "test", "bom-check"]) == 1  # 실패가 있으면 종료 코드 1 (자동화에서 쓰기 좋게)
    assert "기대 1, 실제 3" in capsys.readouterr().out


def test_case_loading_and_checks(tmp_path: Path) -> None:
    assert load_cases(tmp_path) == ([], None)
    assert check({"contains": "a", "regex": ["b+"], "files": ["*.xlsx"]}, "abb", None, ["/x/r.xlsx"], False) == []
    bad = check({"not_contains": ["a"], "json": {"n.0": 2}, "files": ["*.pptx"]}, "a", {"n": [1]}, [], True)
    assert len(bad) == 4 and bad[0].startswith("실행이 실패함") and "json n.0: 기대 2, 실제 1" in bad
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "cases.yaml").write_text("cases:\n  - name: x\n", encoding="utf-8")
    try:
        load_cases(tmp_path)
    except ValueError as e:
        assert "command / inputs / prompt" in str(e)
    else:  # pragma: no cover
        raise AssertionError
