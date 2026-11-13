"""스킬 테스트 러너: 스킬 폴더에 든 테스트 사례를 실제로 실행하고 기대와 비교한다.

Git 에서 받은(Claude Code 등으로 만든) 스킬·스크립트가 이 에이전트에서 그대로 도는지 확인하는 용도.
스킬을 고치거나 Git 에서 새로 받을 때마다 돌리면, 무엇이 깨졌는지 바로 알 수 있다.

사례 파일 (스킬 폴더 안, 먼저 찾은 것)
  tests/cases.yaml | tests/cases.yml | tests/cases.json
    cases:
      - name: 홈 화면이 뜬다
        command: python scripts/view.py home --format json     # 스크립트 한 번 실행 (LLM 없음)
        expect: {contains: ["모델"], json: {view: home}}
      - name: 10월 집계
        inputs: {month: "2026-10"}                              # 스킬 steps 를 실행 (LLM 없음)
        expect: {contains: ["합계"], files: ["*.xlsx"]}
      - name: 말로 시켜도 된다
        prompt: "10월 매출을 부서별로 정리해줘"                   # LLM 으로 스킬 실행 (LLM 이 있고 --llm 일 때만)
        expect: {regex: ["부서.*합계"]}
  evals/evals.json  (Claude 의 skill-creator 형식: {"evals": [{"id", "prompt", "expected_output", "expectations": [...]}]})
    → prompt 사례로 실행. 글로 적힌 기대(expectations)는 자동으로 판정하지 않고 사람이 볼 수 있게 결과와 함께 보여줌.

expect 항목: contains / not_contains (글자 포함), regex, json ({경로: 값}), files (만든 파일 이름 패턴), error (true 면 실패해야 통과)
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import AgentRuntime

CASE_FILES = ("tests/cases.yaml", "tests/cases.yml", "tests/cases.json", "evals/evals.json")


class SkillTestError(ValueError):
    pass


def load_cases(skill_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    """(사례 목록, 사례 파일 경로). 없으면 ([], None)."""
    import yaml

    for rel in CASE_FILES:
        p = skill_dir / rel
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8-sig"))
        except yaml.YAMLError as e:
            raise SkillTestError(f"{rel} 을(를) 읽을 수 없습니다: {e}") from e
        raw = data.get("cases") or data.get("evals") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raise SkillTestError(f"{rel} 에 cases(또는 evals) 목록이 없습니다.")
        cases = []
        for i, c in enumerate(raw, 1):
            if not isinstance(c, dict):
                raise SkillTestError(f"{rel} 의 {i}번째 사례가 객체가 아닙니다.")
            case = dict(c)
            case["name"] = str(c.get("name") or c.get("id") or f"사례 {i}")
            if rel.startswith("evals/"):  # skill-creator 형식
                case["manual"] = [str(x) for x in (c.get("expectations") or c.get("assertions") or [])]
                if c.get("expected_output"):
                    case["manual"].insert(0, str(c["expected_output"]))
            if not any(k in case for k in ("command", "inputs", "prompt")):
                raise SkillTestError(f"{rel} 의 '{case['name']}' 에 command / inputs / prompt 중 하나가 필요합니다.")
            if case.get("expect") is not None and not isinstance(case["expect"], dict):
                raise SkillTestError(f"{rel} 의 '{case['name']}' expect 는 객체여야 합니다.")
            cases.append(case)
        return cases, rel
    return [], None


def check(expect: dict[str, Any] | None, text: str, data: Any, files: list[str], is_error: bool) -> list[str]:
    """기대와 다른 점 목록 (비어 있으면 통과)."""
    from .workflow import walk

    exp = expect or {}
    bad: list[str] = []
    want_error = bool(exp.get("error", False))
    if is_error != want_error:
        bad.append("실패해야 하는데 성공함" if want_error else "실행이 실패함: " + text.strip()[:200])
    for s in _list(exp.get("contains")):
        if s not in text:
            bad.append(f"결과에 '{s}' 이(가) 없음")
    for s in _list(exp.get("not_contains")):
        if s in text:
            bad.append(f"결과에 '{s}' 이(가) 있으면 안 됨")
    for pat in _list(exp.get("regex")):
        try:
            if not re.search(pat, text, re.S):
                bad.append(f"정규식 /{pat}/ 에 맞는 곳이 없음")
        except re.error as e:
            bad.append(f"정규식 /{pat}/ 이(가) 올바르지 않음: {e}")
    if exp.get("json"):
        if data is None:
            bad.append("결과가 JSON 이 아니어서 json 기대를 확인할 수 없음")
        else:
            for path, want in dict(exp["json"]).items():
                got = walk(data, str(path).split("."))
                if got != want:
                    bad.append(f"json {path}: 기대 {want!r}, 실제 {got!r}")
    names = [Path(f).name for f in files]
    for pat in _list(exp.get("files")):
        if not any(fnmatch.fnmatch(n, pat) for n in names):
            bad.append(f"'{pat}' 파일을 만들지 않음 (만든 파일: {', '.join(names) or '없음'})")
    return bad


def _list(v: Any) -> list[str]:
    return [] if v is None else [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _json_of(text: str, data: Any) -> Any:
    if data is not None:
        return data
    for cand in (text, text[text.find("{"):] if "{" in text else "", text[text.find("["):] if "[" in text else ""):
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            continue
    return None


async def run_skill_tests(rt: "AgentRuntime", name: str, use_llm: bool = False, only: str | None = None) -> dict[str, Any]:
    """스킬 하나의 사례를 모두 실행. LLM 이 필요한 사례(prompt)는 use_llm 이고 LLM 이 준비됐을 때만."""
    skill = rt.skills.get(name)
    cases, source = load_cases(skill.path)
    ready, why = rt.llm_status()
    out: dict[str, Any] = {"skill": name, "source": source, "passed": 0, "failed": 0, "skipped": 0, "cases": []}
    for case in cases:
        if only and only not in case["name"]:
            continue
        kind = "command" if case.get("command") else "prompt" if case.get("prompt") else "steps"
        row: dict[str, Any] = {"name": case["name"], "kind": kind, "status": "passed", "problems": [], "manual": case.get("manual") or []}
        out["cases"].append(row)
        if kind == "prompt" and not (use_llm and ready):
            row["status"] = "skipped"
            row["problems"] = ["LLM 이 필요한 사례 (" + ("--llm 으로 실행" if ready else f"LLM 없음: {why}") + ")"]
            out["skipped"] += 1
            continue
        if kind == "steps" and not skill.steps and not skill.entry_commands():
            row["status"] = "skipped"
            row["problems"] = ["스킬에 steps 가 없어 LLM 없이 실행할 수 없음 (command 사례로 쓰거나 prompt 사례로 바꾸세요)"]
            out["skipped"] += 1
            continue
        t0 = time.monotonic()
        try:
            if kind == "command":
                r = await rt.hub.call("run_skill_script", {"name": name, "command": str(case["command"]),
                                                           **({"timeout": case["timeout"]} if case.get("timeout") else {})})
                text, data, files, is_error = r.text, r.data, [f.get("path", "") for f in r.files], r.is_error
            else:
                res, _ = await rt.run_skill(name, case.get("inputs") or {}, str(case.get("prompt") or ""), save=False, ask=None,
                                            mode="workflow" if kind == "steps" else "auto")
                text, data, files, is_error = res.output, None, [f.get("path", "") for f in res.files], not res.success
        except Exception as e:  # noqa: BLE001 - 사례 하나의 실패가 다른 사례를 막지 않게
            text, data, files, is_error = f"{type(e).__name__}: {e}", None, [], True
        row["elapsed"] = round(time.monotonic() - t0, 2)
        row["output"] = text.strip()[:600]
        row["problems"] = check(case.get("expect"), text, _json_of(text, data), files, is_error)
        if row["problems"]:
            row["status"] = "failed"
        out["failed" if row["problems"] else "passed"] += 1
    info = skill.info.get("source") or {}
    rt.skills.set_info(name, last_test={"time": time.time(), "passed": out["passed"], "failed": out["failed"], "skipped": out["skipped"],
                                        "commit": info.get("commit")})
    return out


def format_results(results: list[dict[str, Any]]) -> str:
    lines = []
    total = {"passed": 0, "failed": 0, "skipped": 0}
    for r in results:
        for k in total:
            total[k] += r[k]
        if not r["cases"]:
            lines.append(f"## {r['skill']}: 테스트 사례 없음 (스킬 폴더에 tests/cases.yaml 또는 evals/evals.json 을 넣으세요)")
            continue
        mark = "✅" if not r["failed"] else "❌"
        lines.append(f"## {mark} {r['skill']}: 통과 {r['passed']} · 실패 {r['failed']} · 건너뜀 {r['skipped']}  ({r['source']})")
        for c in r["cases"]:
            icon = {"passed": "✓", "failed": "✗", "skipped": "–"}[c["status"]]
            lines.append(f"- {icon} {c['name']} [{ {'command': '스크립트', 'steps': '단계 실행', 'prompt': 'LLM'}[c['kind']] }]"
                         + (f" {c['elapsed']}초" if c.get("elapsed") is not None else ""))
            lines += [f"    · {p}" for p in c["problems"]]
            if c["status"] == "failed" and c.get("output"):
                lines.append("    · 실제 결과: " + c["output"][:300].replace("\n", " ⏎ "))
            if c["manual"] and c["status"] != "skipped":
                lines.append("    · 사람이 확인할 기대: " + " / ".join(c["manual"][:5]))
    head = f"# 스킬 테스트: 통과 {total['passed']} · 실패 {total['failed']} · 건너뜀 {total['skipped']}"
    return "\n".join([head, *lines])


def make_test_tool(rt: "AgentRuntime") -> Any:
    from .mcp_hub import BuiltinTool, ToolOutcome
    from .skills import SkillError

    async def test_skill(args: dict[str, Any]) -> ToolOutcome:
        names = [str(args["name"])] if args.get("name") else [s.name for s in rt.skills.list()[0]]
        results = []
        for n in names:
            try:
                r = await run_skill_tests(rt, n, bool(args.get("llm")), args.get("only"))
            except (SkillError, SkillTestError) as e:
                r = {"skill": n, "source": None, "passed": 0, "failed": 1, "skipped": 0,
                     "cases": [{"name": "(사례 읽기)", "kind": "command", "status": "failed", "problems": [str(e)], "manual": []}]}
            if r["cases"] or args.get("name"):
                results.append(r)
        if not results:
            return ToolOutcome("테스트 사례가 있는 스킬이 없습니다 (스킬 폴더에 tests/cases.yaml 또는 evals/evals.json).", False, [], {"results": []})
        failed = sum(r["failed"] for r in results)
        return ToolOutcome(format_results(results), failed > 0, [], {"results": results})

    return BuiltinTool(
        "test_skill",
        "스킬의 테스트 사례(스킬 폴더의 tests/cases.yaml 또는 evals/evals.json)를 실제로 실행해 통과·실패를 알려줍니다. "
        "name 을 생략하면 사례가 있는 모든 스킬. 스크립트·단계 사례는 LLM 없이 실행하고, prompt 사례는 llm=true 일 때만 실행합니다.",
        {"type": "object", "properties": {"name": {"type": "string", "description": "스킬 이름 (생략 시 전체)"},
                                          "llm": {"type": "boolean", "description": "LLM 이 필요한 prompt 사례도 실행 (토큰 사용)"},
                                          "only": {"type": "string", "description": "이 글자가 이름에 든 사례만"}}},
        test_skill,
    )
