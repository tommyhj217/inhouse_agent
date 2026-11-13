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
_INTERP = re.compile(r"^(python[\d.]*|py|python[\d.]*\.exe|py\.exe|node|node\.exe|bash|sh|zsh|powershell|pwsh|powershell\.exe|cmd|cmd\.exe|uv|run)$", re.I)
_INTERP_FLAGS = {"-3", "-u", "-B", "-X", "utf8", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "/c", "/C"}
_SHELL_OPS = {";", "&&", "||", "|", ">", ">>", "<", "&"}
# 스크립트에 넘길 환경변수 (skills.script_env 로 바꿀 수 있음). {user} {skills_dir} {skill_dir} {skill} {data_dir} {workspace} {output_dir}
DEFAULT_SCRIPT_ENV = {
    # 예: model-agents 스킬 - 사용자별 모델/이력, 다른 스킬(에이전트) 찾을 폴더
    "MODEL_AGENTS_USER": "{user}",
    "MODEL_AGENTS_SKILL_ROOTS": "{skills_dir}",
}


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


def format_view(v: dict[str, Any]) -> str:
    """화면 문서(JSON) → 짧은 텍스트 (번호 목록 + 다음 명령)."""
    out = [f"## {v.get('title') or ''}".rstrip()]
    if v.get("subtitle"):
        out.append(str(v["subtitle"]))
    out += [f"- {c.get('label')}: {c.get('value')}" for c in v.get("card") or []]
    n = 0
    for sec in v.get("sections") or []:
        out.append(f"**{sec.get('title') or ''}**")
        for it in sec.get("items") or []:
            n += 1
            badges = " · ".join(b.get("text", "") for b in it.get("badges") or [])
            cmd = (it.get("action") or {}).get("command")
            out.append(f"{n}. {it.get('title') or it.get('id')}" + (f" ({it['subtitle']})" if it.get("subtitle") else "")
                       + (f" [{badges}]" if badges else "") + (f" → `{cmd}`" if cmd else ""))
    f = v.get("form")
    if isinstance(f, dict):
        vals = f.get("values") or {}
        for x in f.get("fields") or []:
            opts = f" (선택: {', '.join(map(str, x.get('options') or []))})" if x.get("options") else ""
            out.append(f"- 입력 {x.get('name')} [{x.get('label') or ''}] = {vals.get(x.get('name'), x.get('default', ''))}{opts}")
        out += [f"⚠ {e}" for e in f.get("errors") or []]
        if (f.get("submit") or {}).get("command"):
            out.append(f"값 바꾸기 → `{f['submit']['command']} --set \"이름=값\"`")
    inv = v.get("invoke") or {}
    if inv.get("skill"):
        out.append(f"실행 → 스킬 {inv['skill']}: {inv.get('prompt') or ''}")
    if (v.get("after") or {}).get("record_use"):
        out.append(f"실행 후 기록 → `{v['after']['record_use']}`")
    for a in v.get("actions") or []:
        cmd = (a.get("action") or {}).get("command")
        out.append(f"[{a.get('id')}] {a.get('label')}" + (f" → `{cmd}`" if cmd else ""))
    for k in ("note", "empty", "prompt"):
        if v.get(k):
            out.append(str(v[k]))
    return "\n".join(out)


class SkillScriptRunner:
    def __init__(self, skills: SkillStore, workspace: Any, cfg: dict[str, Any] | None = None, dynamic: "DynamicToolManager | None" = None, links: Any = None):
        cfg = cfg or {}
        mode = cfg.get("scripts", "on")
        self.mode = "off" if mode is False else ("on" if mode in (True, None) else str(mode).lower())
        self.timeout = float(cfg.get("script_timeout") or 300)
        env = cfg.get("script_env")
        self.script_env: dict[str, str] = {str(k): str(v) for k, v in (DEFAULT_SCRIPT_ENV if env is None else (env or {})).items()}
        self.skills = skills
        self.ws = workspace
        self.dynamic = dynamic
        self.links = links
        self.envs: Any = None  # skill_env.SkillEnvManager (스킬별 파이썬 가상환경) - runtime 이 넣음

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

    def parse_command(self, command: str, name: str | None = None) -> tuple[str, str, list[str], dict[str, str]]:
        """스킬 문서에 적힌 명령줄 → (스킬, 스크립트 상대경로, 인자, 환경변수).
        예) python3 .agents/skills/model-agents/scripts/view.py model MDL-101 --format json
            python "C:/agent/skills/model-agents/scripts/models.py" use MDL-101 model-test-plan --summary "완료"
            MODEL_AGENTS_USER=kim python3 <this skill>/scripts/view.py home"""
        import shlex

        cmd = command.strip().replace("<this skill>", str(self.skills.root / name) if name else "<this skill>")
        try:
            toks = shlex.split(cmd.replace("\\", "/"), posix=True)
        except ValueError as e:
            raise SkillError(f"명령줄을 해석할 수 없습니다: {e}") from e
        if any(t in _SHELL_OPS for t in toks):
            raise SkillError("명령은 하나씩 실행하세요 (; && | > 같은 연결/리다이렉트는 쓸 수 없습니다).")
        env: dict[str, str] = {}
        while toks and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]):
            k, v = toks.pop(0).split("=", 1)
            env[k] = v
        while toks and (_INTERP.match(Path(toks[0]).name) or toks[0] in _INTERP_FLAGS):
            toks.pop(0)
        if not toks:
            raise SkillError("명령줄에 실행할 스크립트가 없습니다.")
        script, argv = toks[0], toks[1:]
        root = self.skills.root.resolve()
        p = Path(script)
        if p.is_absolute():
            try:
                rel_parts = p.resolve().relative_to(root).parts
            except ValueError:
                # 다른 위치(예: 원래 저장소 경로)에 있던 스킬 경로 → 경로 속 스킬 이름으로 찾기
                rel_parts = ()
            if len(rel_parts) >= 2:
                return rel_parts[0], "/".join(rel_parts[1:]), argv, env
        parts = [x for x in p.as_posix().split("/") if x not in ("", ".")]
        for k in range(len(parts) - 1, -1, -1):  # …/<스킬이름>/scripts/view.py
            if (root / parts[k] / "SKILL.md").is_file() and k < len(parts) - 1 and (root / parts[k] / "/".join(parts[k + 1:])).is_file():
                return parts[k], "/".join(parts[k + 1:]), argv, env
        if name:  # scripts/view.py 처럼 스킬 기준 경로 (또는 파일 이름만)
            return name, "/".join(parts), argv, env
        raise SkillError(f"명령줄의 스크립트가 어느 스킬 것인지 알 수 없습니다: {script} (name 에 스킬 이름을 주세요)")

    def _env_values(self, name: str, sdir: Path, out_dir: Path) -> dict[str, str]:
        vals = {"user": current_user.get() or "", "skills_dir": self.skills.root.resolve().as_posix(), "skill_dir": sdir.as_posix(), "skill": name,
                "data_dir": (sdir / "data").as_posix(), "workspace": Path(self.ws.root).as_posix(), "output_dir": out_dir.as_posix()}
        out = {}
        for k, v in self.script_env.items():
            for ph, val in vals.items():
                v = v.replace("{" + ph + "}", val)
            out[k] = v
        return out

    async def run(self, name: str, rel: str, args: dict[str, Any] | None = None, argv: list[Any] | None = None,
                  timeout: float | None = None, cwd: str = "output", stdin: str | None = None, extra_env: dict[str, str] | None = None) -> ToolOutcome:
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

        env_note = ""
        code = script.read_text(encoding="utf-8", errors="replace") if script.suffix.lower() == ".py" else ""
        if code and _HAS_RUN.search(code) and self.dynamic is not None and not argv:
            # def run(args, ctx) 형태 → 동적 도구 실행기로 (ctx.call_tool 로 MCP 도구 호출 가능)
            r = await self.dynamic.execute("tool", script, dict(args or {}), timeout)
            text = self.dynamic.format_result(r)
            ok, rc, stdout, stderr = r["ok"], 0 if r["ok"] else 1, "", ""
        else:
            runner = RUNNERS[script.suffix.lower()]
            if script.suffix.lower() == ".py" and self.envs is not None:
                from .skill_env import SkillEnvError

                try:  # requirements.txt·.python-version 이 있으면 그 스킬 전용 가상환경 (처음이면 만들고 검증)
                    plan = await asyncio.to_thread(self.envs.ensure, name, sdir)
                except SkillEnvError as e:
                    return ToolOutcome(f"오류: 스킬 '{name}' 의 파이썬 환경을 준비하지 못했습니다: {e}", True)
                runner = [plan.python]
                env_note = "" if plan.kind == "base" else f" [{'공유' if plan.kind == 'shared' else '전용'} 가상환경]"
            cmd = [*runner, str(script), *_argv(args, argv)]
            if shutil.which(cmd[0]) is None and not Path(cmd[0]).is_file():
                return ToolOutcome(f"오류: '{cmd[0]}' 실행 프로그램이 없습니다 ({script.name}).", True)
            env = dict(os.environ)
            env.update(
                PYTHONIOENCODING="utf-8", PYTHONUTF8="1", SKILL_DIR=str(sdir), SKILL_NAME=name,
                SKILL_ARGS=json.dumps(args or {}, ensure_ascii=False), AGENT_WORKSPACE=str(self.ws.root),
                AGENT_DOWNLOAD_DIR=str(self.ws.downloads), AGENT_OUTPUT_DIR=str(out_dir), AGENT_USER=current_user.get() or "",
                AGENT_SKILLS_DIR=str(self.skills.root.resolve()), AGENT_SKILL_DATA=str(sdir / "data"),
                # Claude Code 스킬 스크립트가 읽는 환경변수
                CLAUDE_SKILL_DIR=str(sdir), CLAUDE_PROJECT_DIR=str(self.ws.root), CLAUDE_PLUGIN_ROOT=str(sdir),
            )
            env.update(self._env_values(name, sdir, out_dir))
            env.update(extra_env or {})
            for k in [k for k, v in env.items() if v == ""]:
                env.pop(k)  # 빈 값(예: 사용자 ID 없음)은 넘기지 않음 → 스크립트 기본값 사용
            for k in [k for k in env if k.upper() in ("AZURE_OPENAI_API_KEY", "AGENT_API_TOKEN", "AGENT_GIT_TOKEN")]:
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
        lines = [f"[스킬 스크립트] {name}/{script.relative_to(sdir).as_posix()} → {'성공' if ok else f'실패(종료코드 {rc})'} ({elapsed:.1f}초)"
                 + env_note]
        if text:
            lines.append(text)
        data = None
        try:
            data = json.loads(stdout) if stdout.strip()[:1] in "{[" else None
        except ValueError:
            data = None
        from .agent import view_of

        if view_of(data):  # 화면 문서: 웹에는 메뉴로 보이고, LLM 에는 짧게 요약 (토큰 절약)
            lines.append("[화면] (웹 화면에 버튼 메뉴로 표시됨)\n" + format_view(data))
        elif stdout.strip():
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
        return ToolOutcome("\n".join(lines), not ok, statuses, data)

    def tools(self) -> list[BuiltinTool]:
        async def run_skill_script(args: dict[str, Any]) -> ToolOutcome:
            name = str(args.get("name") or "") or None
            argv = args.get("argv") if isinstance(args.get("argv"), list) else None
            extra: dict[str, str] = {}
            path = args.get("path")
            if args.get("command"):  # 스킬 문서/화면이 알려준 명령줄 그대로
                try:
                    name, path, argv2, extra = self.parse_command(str(args["command"]), name)
                except SkillError as e:
                    return ToolOutcome(f"오류: {e}", True)
                argv = [*argv2, *(argv or [])]
            if not name or not path:
                return ToolOutcome("오류: name+path 또는 command 가 필요합니다.", True)
            return await self.run(
                name, str(path), args.get("args") if isinstance(args.get("args"), dict) else None,
                argv, args.get("timeout"), str(args.get("cwd") or "output"), args.get("stdin"), extra,
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
                "스킬 문서나 스크립트 출력에 'python3 …/scripts/view.py model MDL-101' 같은 명령줄이 있으면 command 에 그대로 넣으세요. "
                "만든 파일은 사용자 files/skill_<스킬>/ 아래에 저장됩니다.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "스킬 이름"},
                        "path": {"type": "string", "description": "스킬 폴더 기준 스크립트 경로 (예: scripts/make_menu.py, 파일 이름만 줘도 됨)"},
                        "command": {"type": "string", "description": "명령줄 그대로 (예: python3 .agents/skills/model-agents/scripts/view.py home --format json). name/path 대신"},
                        "args": {"type": "object", "description": "스크립트 인자 {키: 값} → --키 값"},
                        "argv": {"type": "array", "items": {"type": "string"}, "description": "명령줄 인자를 그대로 (args 대신)"},
                        "stdin": {"type": "string", "description": "표준입력으로 넘길 내용 (선택)"},
                        "cwd": {"type": "string", "enum": ["output", "skill"], "description": "작업 폴더: output(기본, 결과 폴더) | skill(스킬 폴더)"},
                        "timeout": {"type": "number", "description": f"최대 실행 초 (기본 {self.timeout:.0f})"},
                    },
                    "required": [],
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
