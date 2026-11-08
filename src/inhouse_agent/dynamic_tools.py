"""동적 도구: 필요한 tool 이 없을 때 에이전트가 파이썬 코드로 새 tool 을 만들어 즉시 등록·실행.

- 코드는 tools/<이름>.py 로 저장되어 재시작 후에도 재사용됩니다 (웹 UI '도구' 탭에서 검토/수정/테스트).
- 실행은 별도 파이썬 프로세스(제한시간, 최소 환경변수, 작업폴더=workspace)에서 합니다.
  ※ 프로세스 격리 + 정적 검사 + 승인 절차이지 완전한 보안 샌드박스는 아닙니다.
- 도구 코드는 ctx.call_tool() 로 기존 MCP tool 을 조합할 수 있습니다 (예: 30일치 반복 조회).
- 승인 모드(dynamic_tools.mode): off | approve(사용자 승인 후 실행) | auto
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .interaction import current_run

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RUNNER = Path(__file__).with_name("_tool_runner.py")
FORBIDDEN_CHILD_CALLS = {"create_tool", "run_python", "ask_user", "use_skill", "delete_tool"}

TOOL_TEMPLATE = '''# 에이전트 동적 도구 - inhouse-agent 가 관리합니다.
TOOL = {meta}


{code}
'''

DEFAULT_CODE = '''def run(args, ctx):
    """args: LLM 이 넘긴 인자(dict), ctx: workspace/download_dir 경로, call_tool(), log()"""
    return {"echo": args}
'''


@dataclass
class DynamicToolsConfig:
    mode: str = "approve"  # off | approve | auto
    dir: str = "tools"
    timeout: float = 120.0
    max_output_chars: int = 30000
    blocked_modules: list[str] = field(default_factory=lambda: ["subprocess", "ctypes", "multiprocessing", "winreg", "pty", "pickle", "marshal"])
    blocked_calls: list[str] = field(
        default_factory=lambda: [
            "os.system", "os.popen", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.kill",
            "os.exec*", "os.spawn*", "shutil.rmtree", "shutil.move", "eval", "exec", "compile", "__import__",
        ]
    )
    env_passthrough: list[str] = field(default_factory=list)
    allow_tool_calls: bool = True  # 도구 코드에서 ctx.call_tool 로 다른 tool 호출 허용

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "DynamicToolsConfig":
        d = d or {}
        base = cls()
        raw_mode = d.get("mode", "approve")
        # YAML 에서 따옴표 없는 off/on/no 는 bool 로 읽힘 → off / auto 로 해석
        mode = "off" if raw_mode is False else "auto" if raw_mode is True else str(raw_mode or "approve").lower()
        if mode not in ("off", "approve", "auto"):
            raise ValueError("dynamic_tools.mode 는 off / approve / auto 중 하나여야 합니다.")
        return cls(
            mode=mode,
            dir=str(d.get("dir") or "tools"),
            timeout=float(d.get("timeout") or 120),
            max_output_chars=int(d.get("max_output_chars") or 30000),
            blocked_modules=list(d["blocked_modules"]) if "blocked_modules" in d and d["blocked_modules"] is not None else base.blocked_modules,
            blocked_calls=list(d["blocked_calls"]) if "blocked_calls" in d and d["blocked_calls"] is not None else base.blocked_calls,
            env_passthrough=[str(x) for x in d.get("env_passthrough") or []],
            allow_tool_calls=str(d.get("allow_tool_calls", True)).lower() in ("1", "true", "yes", "on"),
        )


class ToolCodeError(ValueError):
    pass


@dataclass
class CustomTool:
    name: str
    description: str
    parameters: dict[str, Any]
    code: str  # run() 을 포함한 본문 (TOOL 메타 제외)
    path: Path
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "function": f"custom__{self.name}",
            "description": self.description,
            "parameters": self.parameters,
            "created_at": self.meta.get("created_at"),
            "updated_at": self.meta.get("updated_at"),
            "created_by": self.meta.get("created_by"),
            "last_test": self.meta.get("last_test"),
            "path": str(self.path),
        }


# ---------------------------------------------------------------------------
# 정적 검사
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def check_code(code: str, cfg: DynamicToolsConfig, *, require_run: bool) -> list[str]:
    """문법/정책 검사. 문제 목록을 돌려줌 (비어 있으면 통과)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"문법 오류 {e.lineno}행: {e.msg}"]
    problems: list[str] = []
    blocked_mods = set(cfg.blocked_modules)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in blocked_mods:
                    problems.append(f"{node.lineno}행: 금지된 모듈 import '{a.name}'")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in blocked_mods:
                problems.append(f"{node.lineno}행: 금지된 모듈 import '{node.module}'")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            for pat in cfg.blocked_calls:
                if name == pat or (pat.endswith("*") and name.startswith(pat[:-1])):
                    problems.append(f"{node.lineno}행: 금지된 호출 '{name}'")
    if require_run:
        runs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
        if not runs:
            problems.append("최상위에 def run(args, ctx): 함수가 필요합니다.")
        elif len(runs[0].args.args) < 2:
            problems.append("run 함수는 (args, ctx) 두 인자를 받아야 합니다.")
        if any(isinstance(n, ast.Assign) and any(_dotted(t) == "TOOL" for t in n.targets) for n in tree.body):
            problems.append("코드에 TOOL = ... 을 직접 쓰지 마세요 (메타정보는 따로 저장됩니다).")
    return problems


def _validate_schema(params: Any) -> dict[str, Any]:
    if params in (None, "", {}):
        return {"type": "object", "properties": {}}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as e:
            raise ToolCodeError(f"parameters 는 JSON 스키마여야 합니다: {e}") from e
    if not isinstance(params, dict):
        raise ToolCodeError("parameters 는 JSON 스키마 객체여야 합니다.")
    if params.get("type") != "object":
        params = {"type": "object", "properties": params.get("properties", params)}
    params.setdefault("properties", {})
    json.dumps(params)  # 직렬화 가능 확인
    return params


# ---------------------------------------------------------------------------
# 저장소 + 실행기
# ---------------------------------------------------------------------------


class DynamicToolManager:
    def __init__(self, cfg: DynamicToolsConfig, root: Path, workspace: Any):
        self.cfg = cfg
        self.root = root
        self.ws = workspace
        self.hub: Any = None  # MCPHub (runtime 에서 연결)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------- 저장소 ----------------
    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise ToolCodeError("도구 이름은 영문 소문자로 시작하고 소문자/숫자/_ 만 사용할 수 있습니다 (2~41자).")
        return self.root / f"{name}.py"

    def load(self, name: str) -> CustomTool:
        p = self._path(name)
        if not p.is_file():
            raise ToolCodeError(f"도구 '{name}' 이(가) 없습니다.")
        return self._parse_file(p)

    def _parse_file(self, p: Path) -> CustomTool:
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        meta: dict[str, Any] = {}
        body_start = 0
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(_dotted(t) == "TOOL" for t in node.targets):
                meta = ast.literal_eval(node.value)
                body_start = node.end_lineno or 0
                break
        if not meta:
            raise ToolCodeError(f"{p.name}: TOOL 메타정보가 없습니다.")
        code = "\n".join(src.splitlines()[body_start:]).strip("\n") + "\n"
        return CustomTool(meta["name"], meta.get("description", ""), meta.get("parameters") or {"type": "object", "properties": {}}, code, p, meta)

    def list(self) -> tuple[list[CustomTool], dict[str, str]]:
        tools, errors = [], {}
        for p in sorted(self.root.glob("*.py")):
            if p.name.startswith("_"):
                continue
            try:
                tools.append(self._parse_file(p))
            except Exception as e:  # noqa: BLE001
                errors[p.stem] = f"{type(e).__name__}: {e}"
        return tools, errors

    def save(self, name: str, description: str, parameters: Any, code: str, *, created_by: str, last_test: dict | None = None) -> CustomTool:
        p = self._path(name)
        params = _validate_schema(parameters)
        problems = check_code(code, self.cfg, require_run=True)
        if problems:
            raise ToolCodeError("코드 검사 실패:\n- " + "\n- ".join(problems))
        now = datetime.now().isoformat(timespec="seconds")
        meta: dict[str, Any] = {"name": name, "description": description.strip(), "parameters": params, "updated_at": now, "created_by": created_by}
        if p.exists():
            old = self._parse_file(p)
            meta["created_at"] = old.meta.get("created_at", now)
            hist = self.root / ".history"
            hist.mkdir(exist_ok=True)
            shutil.copy2(p, hist / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.py")
        else:
            meta["created_at"] = now
        if last_test is not None:
            meta["last_test"] = last_test
        p.write_text(TOOL_TEMPLATE.format(meta=_pretty(meta), code=code.strip("\n")), encoding="utf-8")
        tool = self._parse_file(p)
        self.register_all()
        return tool

    def delete(self, name: str) -> None:
        p = self._path(name)
        if p.exists():
            trash = self.root / ".trash"
            trash.mkdir(exist_ok=True)
            shutil.move(str(p), str(trash / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.py"))
        self.register_all()

    def register_all(self) -> None:
        """저장된 도구를 hub 에 custom__<이름> 으로 등록."""
        if self.hub is None:
            return
        from .mcp_hub import BuiltinTool

        tools, errors = self.list()
        for n, e in errors.items():
            log.warning("동적 도구 '%s' 로드 실패: %s", n, e)
        specs = []
        for t in tools:
            async def handler(args: dict[str, Any], _t: CustomTool = t) -> Any:
                return await self.run_tool(_t, args)

            specs.append(BuiltinTool(t.name, f"[동적 도구] {t.description}"[:1024], t.parameters, handler))
        self.hub.set_custom_tools(specs)

    # ---------------- 실행 ----------------
    def _env(self) -> dict[str, str]:
        keep = {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", *self.cfg.env_passthrough,
        }
        env = {k: v for k, v in os.environ.items() if k in keep}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["AGENT_WORKSPACE"] = str(self.ws.root)
        env["AGENT_DOWNLOAD_DIR"] = str(self.ws.downloads)
        return env

    async def execute(self, mode: str, path: Path, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """자식 프로세스 실행. 반환: {ok, value, stdout, stderr, error, traceback, elapsed, tool_calls}"""
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(RUNNER),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self.ws.root), env=self._env(), limit=32 * 1024 * 1024,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        init = {"mode": mode, "path": str(path), "args": args, "workspace": str(self.ws.root), "download_dir": str(self.ws.downloads)}
        proc.stdin.write((json.dumps(init, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()
        stderr_chunks: list[bytes] = []

        async def read_err() -> None:
            while True:
                b = await proc.stderr.read(4096)
                if not b:
                    break
                if sum(map(len, stderr_chunks)) < 200_000:
                    stderr_chunks.append(b)

        err_task = asyncio.create_task(read_err())
        calls: list[str] = []
        final: dict[str, Any] | None = None

        async def protocol() -> None:
            nonlocal final
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "call":
                    name = str(msg.get("name"))
                    calls.append(name)
                    text, is_err, data, files = await self._child_call(name, msg.get("args") or {})
                    reply = {"id": msg.get("id"), "text": text, "is_error": is_err, "data": data, "files": files}
                    proc.stdin.write((json.dumps(reply, ensure_ascii=False, default=str) + "\n").encode())
                    await proc.stdin.drain()
                elif msg.get("type") in ("result", "error"):
                    final = msg
                    return

        limit = timeout or self.cfg.timeout
        try:
            await asyncio.wait_for(protocol(), timeout=limit)
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            final = {"type": "error", "error": f"제한 시간 {limit:.0f}초 초과로 중단했습니다."}
        except asyncio.CancelledError:
            proc.kill()
            raise
        finally:
            try:
                await asyncio.wait_for(err_task, timeout=3)
            except asyncio.TimeoutError:
                err_task.cancel()
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        if final is None:
            final = {"type": "error", "error": f"프로세스가 결과 없이 종료되었습니다 (종료코드 {proc.returncode})"}
        return {
            "ok": final["type"] == "result",
            "value": final.get("value"),
            "stdout": final.get("stdout", ""),
            "stderr": stderr[-5000:],
            "error": final.get("error"),
            "traceback": final.get("traceback"),
            "elapsed": round(time.monotonic() - t0, 2),
            "tool_calls": calls,
        }

    async def _child_call(self, name: str, args: dict[str, Any]) -> tuple[str, bool, Any, list[dict[str, Any]]]:
        if not self.cfg.allow_tool_calls or self.hub is None:
            return "오류: 도구 코드에서 다른 tool 호출이 허용되지 않았습니다 (dynamic_tools.allow_tool_calls).", True, None, []
        if name in FORBIDDEN_CHILD_CALLS or name.startswith("custom__"):
            return f"오류: 도구 코드에서는 '{name}' 을(를) 호출할 수 없습니다.", True, None, []
        out = await self.hub.call(name, args)
        files = [{k: f.get(k) for k in ("path", "state", "detected_type", "size", "issues")} for f in out.files]
        return out.text, out.is_error, out.data, files

    def format_result(self, r: dict[str, Any]) -> str:
        lim = self.cfg.max_output_chars
        if r["ok"]:
            v = r["value"]
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1, default=str)
            parts = [text or "(반환값 없음)"]
            if r["stdout"].strip():
                parts.append("[print 출력]\n" + r["stdout"].strip())
        else:
            parts = [f"오류: {r['error']}"]
            if r.get("traceback"):
                parts.append("[traceback]\n" + r["traceback"].strip())
            if r["stdout"].strip():
                parts.append("[print 출력]\n" + r["stdout"].strip())
        if r["stderr"].strip():
            parts.append("[로그]\n" + r["stderr"].strip()[-3000:])
        if r["tool_calls"]:
            parts.append(f"(내부 tool 호출 {len(r['tool_calls'])}회: {', '.join(dict.fromkeys(r['tool_calls']))})")
        parts.append(f"(실행 {r['elapsed']}s)")
        out = "\n\n".join(parts)
        return out if len(out) <= lim else out[: lim - 200] + f"\n...(출력 {len(out)}자 중 생략)"

    async def run_tool(self, tool: CustomTool, args: dict[str, Any]) -> Any:
        from .mcp_hub import ToolOutcome

        r = await self.execute("tool", tool.path, args)
        return ToolOutcome(self.format_result(r), not r["ok"])

    def _off(self) -> dict[str, Any] | None:
        if self.cfg.mode != "off":
            return None
        return {"ok": False, "error": "동적 도구 기능이 꺼져 있습니다 (dynamic_tools.mode: off).", "value": None, "stdout": "", "stderr": "",
                "traceback": None, "elapsed": 0, "tool_calls": []}

    async def run_script(self, code: str, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if (off := self._off()) is not None:  # 어느 경로로 불러도 꺼져 있으면 실행하지 않음
            return off
        problems = check_code(code, self.cfg, require_run=False)
        if problems:
            return {"ok": False, "error": "코드 검사 실패: " + "; ".join(problems), "value": None, "stdout": "", "stderr": "", "traceback": None, "elapsed": 0, "tool_calls": []}
        sdir = self.ws.root / "_scripts"
        sdir.mkdir(exist_ok=True)
        p = sdir / f"script_{datetime.now():%Y%m%d_%H%M%S_%f}.py"  # 감사(audit)용으로 남김
        p.write_text(code, encoding="utf-8")
        return await self.execute("script", p, args, timeout)

    async def test_draft(self, name: str, code: str, args: dict[str, Any]) -> dict[str, Any]:
        if (off := self._off()) is not None:
            return off
        problems = check_code(code, self.cfg, require_run=True)
        if problems:
            return {"ok": False, "error": "코드 검사 실패: " + "; ".join(problems), "value": None, "stdout": "", "stderr": "", "traceback": None, "elapsed": 0, "tool_calls": []}
        ddir = self.root / ".drafts"
        ddir.mkdir(exist_ok=True)
        p = ddir / f"{name}.py"
        p.write_text(TOOL_TEMPLATE.format(meta=_pretty({"name": name}), code=code.strip("\n")), encoding="utf-8")
        return await self.execute("tool", p, args)

    # ---------------- 승인 ----------------
    async def approve(self, title: str, detail: str) -> tuple[bool, str]:
        """mode 에 따라 실행 허용 여부 결정. (허용?, 사유)"""
        if self.cfg.mode == "off":
            return False, "동적 도구 기능이 꺼져 있습니다 (dynamic_tools.mode: off)."
        if self.cfg.mode == "auto":
            return True, "auto"
        ctx = current_run.get()
        if ctx is None or ctx.ask is None:
            return False, (
                "승인이 필요한 작업인데 사용자에게 물을 수 없는 실행(예약작업 등)입니다. "
                "이미 저장된 도구만 사용할 수 있습니다. 웹 UI 나 대화형 모드에서 먼저 도구를 만들어 두세요."
            )
        ans = await ctx.ask(f"{title}\n\n{detail}", ["승인", "거부"], "거부")
        ok = ans.strip() in ("승인", "예", "yes", "y", "ok", "허용")
        return ok, "사용자 승인" if ok else f"사용자가 거부했습니다 (답변: {ans})"


def _pretty(meta: dict[str, Any]) -> str:
    import pprint

    return pprint.pformat(meta, width=100, sort_dicts=False)
