"""스킬 폴더에 들어 있는 스크립트(메뉴 생성, 보고서 생성 등) 실행.

skills/<스킬>/scripts/make_menu.py 같은 파일을 에이전트(또는 steps 워크플로)가 직접 실행합니다.

- .py  : 에이전트와 같은 파이썬으로 실행 (openpyxl 등 설치된 패키지 사용 가능)
         `def run(args, ctx):` 가 있으면 동적 도구처럼 실행 → ctx.call_tool 로 MCP 도구도 호출 가능
- .sh / .bat / .cmd / .ps1 / .js : 해당 인터프리터로 실행
- 인자: args(dict) → `--키 값` 으로 전달 + 환경변수 SKILL_ARGS(JSON), 또는 argv(list) 그대로
- 작업 폴더(cwd): 사용자 files/skill_<스킬>/<시각>/ → 스크립트가 만든 파일은 자동으로 결과 파일로 잡힘
- 환경변수: SKILL_DIR, SKILL_NAME, SKILL_ARGS, AGENT_WORKSPACE, AGENT_DOWNLOAD_DIR, AGENT_OUTPUT_DIR, AGENT_USER

설정 (config.yaml)
skills:
  scripts: on          # on(기본) | approve(실행 전 사용자 승인) | off
  script_timeout: 300  # 초
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .files import file_status, format_status
from .interaction import current_run
from .mcp_hub import BuiltinTool, ToolOutcome
from .skills import SkillError, SkillStore
from .users import current_user

if TYPE_CHECKING:
    from .dynamic_tools import DynamicToolManager

RUNNERS: dict[str, list[str]] = {
    ".py": [sys.executable],
    ".sh": ["bash"],
    ".bat": ["cmd", "/c"],
    ".cmd": ["cmd", "/c"],
    ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    ".js": ["node"],
}
_HAS_RUN = re.compile(r"^def run\(\s*args\s*,\s*ctx\s*\)", re.M)


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _argv(args: dict[str, Any] | None, argv: list[Any] | None) -> list[str]:
    if argv:
        return [str(x) for x in argv]
    out: list[str] = []
    for k, v in (args or {}).items():
        if v is None or v is False:
            continue
        flag = "--" + str(k).replace("_", "-") if len(str(k)) > 1 else "-" + str(k)
        if v is True:
            out.append(flag)
        elif isinstance(v, (list, tuple)):
            out += [flag, *[str(x) for x in v]]
        elif isinstance(v, dict):
            out += [flag, json.dumps(v, ensure_ascii=False)]
        else:
            out += [flag, str(v)]
    return out


def skill_scripts(skill_dir: Path) -> list[str]:
    """스킬 폴더 안의 실행 가능한 스크립트 목록 (상대 경로)."""
    return sorted(
        str(p.relative_to(skill_dir)).replace("\\", "/")
        for p in skill_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in RUNNERS and not any(x.startswith((".", "_")) for x in p.relative_to(skill_dir).parts[:-1])
        and not p.name.startswith(".")
    )


class SkillScriptRunner:
    def __init__(self, skills: SkillStore, workspace: Any, cfg: dict[str, Any] | None = None, dynamic: "DynamicToolManager | None" = None, links: Any = None):
        cfg = cfg or {}
        mode = cfg.get("scripts", "on")
        self.mode = "off" if mode is False else ("on" if mode in (True, None) else str(mode).lower())
        self.timeout = float(cfg.get("script_timeout") or 300)
        self.skills = skills
        self.ws = workspace
        self.dynamic = dynamic
        self.links = links

    def resolve(self, name: str, rel: str) -> tuple[Path, Path]:
        sk = self.skills.get(name)
        d = sk.path.resolve()
        rel = str(rel).replace("\\", "/").lstrip("/")
        p = (d / rel).resolve()
        if d not in p.parents or not p.is_file():
            # 파일 이름만 준 경우 scripts/ 등 하위 폴더에서 찾기
            cands = [d / s for s in skill_scripts(d) if Path(s).name == Path(rel).name]
            if len(cands) == 1:
                p = cands[0].resolve()
            else:
                avail = ", ".join(skill_scripts(d)) or "(없음)"
                raise SkillError(f"스킬 '{name}' 에 스크립트 '{rel}' 이(가) 없습니다. 실행 가능한 스크립트: {avail}")
        if p.suffix.lower() not in RUNNERS:
            raise SkillError(f"실행할 수 없는 형식입니다: {p.name} (지원: {', '.join(RUNNERS)})")
        return d, p

    async def run(self, name: str, rel: str, args: dict[str, Any] | None = None, argv: list[Any] | None = None,
                  timeout: float | None = None, cwd: str = "output", stdin: str | None = None) -> ToolOutcome:
        if self.mode == "off":
            return ToolOutcome("오류: 스킬 스크립트 실행이 꺼져 있습니다 (skills.scripts: off).", True)
        try:
            sdir, script = self.resolve(name, rel)
        except SkillError as e:
            return ToolOutcome(f"오류: {e}", True)
        if self.mode == "approve":
            ctx = current_run.get()
            if ctx is None or ctx.ask is None:
                return ToolOutcome("오류: 스크립트 실행에 승인이 필요한데 사용자에게 물을 수 없는 실행입니다.", True)
            ans = await ctx.ask(f"스킬 '{name}' 의 스크립트를 실행할까요?\n{script.name} {' '.join(_argv(args, argv))}", ["승인", "거부"], "거부")
            if ans.strip() not in ("승인", "예", "yes", "y", "ok", "허용"):
                return ToolOutcome(f"사용자가 스크립트 실행을 거부했습니다 (답변: {ans}).", True)

        out_dir = Path(self.ws.files) / f"skill_{name}" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        out_dir.mkdir(parents=True, exist_ok=True)
        timeout = min(float(timeout or self.timeout), max(self.timeout, 1800))
        t0 = time.monotonic()

        code = script.read_text(encoding="utf-8", errors="replace") if script.suffix.lower() == ".py" else ""
        if code and _HAS_RUN.search(code) and self.dynamic is not None and not argv:
            # def run(args, ctx) 형태 → 동적 도구 실행기로 (ctx.call_tool 로 MCP 도구 호출 가능)
            r = await self.dynamic.execute("tool", script, dict(args or {}), timeout)
            text = self.dynamic.format_result(r)
            ok, rc, stdout, stderr = r["ok"], 0 if r["ok"] else 1, "", ""
        else:
            cmd = [*RUNNERS[script.suffix.lower()], str(script), *_argv(args, argv)]
            if shutil.which(cmd[0]) is None and not Path(cmd[0]).is_file():
                return ToolOutcome(f"오류: '{cmd[0]}' 실행 프로그램이 없습니다 ({script.name}).", True)
            env = dict(os.environ)
            env.update(
                PYTHONIOENCODING="utf-8", PYTHONUTF8="1", SKILL_DIR=str(sdir), SKILL_NAME=name,
                SKILL_ARGS=json.dumps(args or {}, ensure_ascii=False), AGENT_WORKSPACE=str(self.ws.root),
                AGENT_DOWNLOAD_DIR=str(self.ws.downloads), AGENT_OUTPUT_DIR=str(out_dir), AGENT_USER=current_user.get() or "",
            )
            for k in [k for k in env if k.upper() in ("AZURE_OPENAI_API_KEY", "AGENT_API_TOKEN")]:
                env.pop(k, None)  # 비밀값은 스크립트에 넘기지 않음
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(sdir if cwd == "skill" else out_dir), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                ob, eb = await asyncio.wait_for(proc.communicate((stdin or "").encode("utf-8")), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolOutcome(f"오류: 스크립트가 {timeout:.0f}초 안에 끝나지 않아 중단했습니다: {script.name}", True)
            rc = proc.returncode or 0
            stdout, stderr = _decode(ob), _decode(eb)
            ok = rc == 0
            text = ""

        elapsed = time.monotonic() - t0
        made = sorted(p for p in out_dir.rglob("*") if p.is_file())
        # 스크립트가 output 폴더 대신 workspace 다른 곳에 썼다고 출력한 경로도 결과 파일로
        for m in re.findall(r"([A-Za-z]:[\\/][^\s\"'<>|]+|/[^\s\"'<>|]+)", stdout + "\n" + text):
            p = Path(m.rstrip(".,)"))
            try:
                if p.is_file() and p not in made and (Path(self.ws.root).resolve() in p.resolve().parents):
                    made.append(p)
            except OSError:
                continue
        statuses = [await file_status(p, self.ws, stable_wait=0) for p in made[:50]]
        if not made:
            try:
                out_dir.rmdir()
            except OSError:
                pass
        lines = [f"[스킬 스크립트] {name}/{script.relative_to(sdir).as_posix()} → {'성공' if ok else f'실패(종료코드 {rc})'} ({elapsed:.1f}초)"]
        if text:
            lines.append(text)
        if stdout.strip():
            s = stdout.strip()
            lines.append("[출력]\n" + (s if len(s) <= 12000 else s[:12000] + "\n...(생략)"))
        if stderr.strip():
            e = stderr.strip()
            lines.append("[오류 출력]\n" + (e[-4000:] if len(e) > 4000 else e))
        if statuses:
            lines.append("[만든 파일]")
            for st in statuses:
                lines.append(format_status(st))
                if self.links is not None:
                    try:
                        lines.append(f"  다운로드 링크: {self.links.url(st['path'])}")
                    except Exception:  # noqa: BLE001
                        pass
        data = None
        try:
            data = json.loads(stdout) if stdout.strip()[:1] in "{[" else None
        except ValueError:
            data = None
        return ToolOutcome("\n".join(lines), not ok, statuses, data)

    def tools(self) -> list[BuiltinTool]:
        async def run_skill_script(args: dict[str, Any]) -> ToolOutcome:
            return await self.run(
                str(args["name"]), str(args["path"]), args.get("args") if isinstance(args.get("args"), dict) else None,
                args.get("argv") if isinstance(args.get("argv"), list) else None, args.get("timeout"), str(args.get("cwd") or "output"),
                args.get("stdin"),
            )

        async def list_skill_scripts(args: dict[str, Any]) -> str:
            names = [str(args["name"])] if args.get("name") else [s.name for s in self.skills.list()[0]]
            lines = []
            for n in names:
                try:
                    sk = self.skills.get(n)
                except SkillError as e:
                    lines.append(f"- {n}: {e}")
                    continue
                sc = skill_scripts(sk.path)
                if sc or args.get("name"):
                    lines.append(f"- {n}: " + (", ".join(sc) or "(스크립트 없음)"))
            return "\n".join(lines) or "스크립트가 들어 있는 스킬이 없습니다."

        return [
            BuiltinTool(
                "run_skill_script",
                "스킬 폴더에 들어 있는 스크립트(예: scripts/make_menu.py - 메뉴/보고서 생성 등)를 실행하고 출력과 만든 파일을 돌려줍니다. "
                "스킬 지침에 '스크립트를 실행' 하라고 되어 있으면 이 도구를 쓰세요. args 는 --키 값 으로 전달됩니다(argv 로 직접 줄 수도 있음). "
                "만든 파일은 사용자 files/skill_<스킬>/ 아래에 저장됩니다.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "스킬 이름"},
                        "path": {"type": "string", "description": "스킬 폴더 기준 스크립트 경로 (예: scripts/make_menu.py, 파일 이름만 줘도 됨)"},
                        "args": {"type": "object", "description": "스크립트 인자 {키: 값} → --키 값"},
                        "argv": {"type": "array", "items": {"type": "string"}, "description": "명령줄 인자를 그대로 (args 대신)"},
                        "stdin": {"type": "string", "description": "표준입력으로 넘길 내용 (선택)"},
                        "cwd": {"type": "string", "enum": ["output", "skill"], "description": "작업 폴더: output(기본, 결과 폴더) | skill(스킬 폴더)"},
                        "timeout": {"type": "number", "description": f"최대 실행 초 (기본 {self.timeout:.0f})"},
                    },
                    "required": ["name", "path"],
                },
                run_skill_script,
            ),
            BuiltinTool(
                "list_skill_scripts",
                "스킬 폴더에 들어 있는 실행 가능한 스크립트 목록을 봅니다 (name 생략 시 모든 스킬).",
                {"type": "object", "properties": {"name": {"type": "string"}}},
                list_skill_scripts,
            ),
        ]
