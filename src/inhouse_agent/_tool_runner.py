"""동적 도구 / run_python 을 별도 파이썬 프로세스에서 실행하는 러너 (자식 프로세스 쪽).

부모(에이전트)와는 stdin/stdout 의 JSON 한 줄 프로토콜로 통신합니다.
  부모 → 자식 (첫 줄): {"mode": "tool"|"script", "path": 코드파일, "args": {...}, "workspace": ..., "download_dir": ...}
  자식 → 부모: {"type": "call", "id": n, "name": tool, "args": {...}}   ← ctx.call_tool()
  부모 → 자식: {"id": n, "text": "...", "is_error": false}
  자식 → 부모 (마지막): {"type": "result", "value": ..., "stdout": "..."} 또는 {"type": "error", ...}
사용자 코드의 print() 는 결과의 stdout 으로 모아서 돌려줍니다.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import traceback
from pathlib import Path

PROTO_OUT = sys.stdout
PROTO_IN = sys.stdin
MAX_STDOUT = 50_000


def _send(obj: dict) -> None:
    PROTO_OUT.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    PROTO_OUT.flush()


class ToolCallError(RuntimeError):
    pass


class Context:
    """사용자 코드에 전달되는 ctx 객체."""

    def __init__(self, workspace: str, download_dir: str, args: dict):
        self.workspace = Path(workspace)
        self.download_dir = Path(download_dir)
        self.files_dir = self.workspace / "files"
        self.args = args
        self._n = 0

    def _call(self, name: str, args: dict | None) -> dict:
        self._n += 1
        _send({"type": "call", "id": self._n, "name": name, "args": args or {}})
        line = PROTO_IN.readline()
        if not line:
            raise ToolCallError("부모 프로세스와 연결이 끊어졌습니다.")
        return json.loads(line)

    def call_tool(self, name: str, args: dict | None = None, *, raise_on_error: bool = False) -> str:
        """에이전트의 다른 tool(MCP tool 포함)을 호출하고 결과 텍스트를 받음. 예: ctx.call_tool("erp__get_daily_sales", {...})"""
        resp = self._call(name, args)
        if resp.get("is_error") and raise_on_error:
            raise ToolCallError(resp.get("text", ""))
        return resp.get("text", "")

    def call_tool_data(self, name: str, args: dict | None = None):
        """tool 결과를 구조화 데이터(dict/list)로 받음. 구조화가 안 되면 텍스트. 오류면 ToolCallError."""
        resp = self._call(name, args)
        if resp.get("is_error"):
            raise ToolCallError(resp.get("text", ""))
        if resp.get("data") is not None:
            return resp["data"]
        text = resp.get("text", "")
        try:
            return json.loads(text)
        except ValueError:
            return text

    def call_tool_files(self, name: str, args: dict | None = None) -> list:
        """tool 을 호출하고 그 호출로 생긴/받은 파일 상태 목록을 받음 (download_url 등). [{path, state, ...}]"""
        resp = self._call(name, args)
        if resp.get("is_error") and not resp.get("files"):
            raise ToolCallError(resp.get("text", ""))
        return resp.get("files") or []

    def log(self, *parts) -> None:
        print(*parts, file=sys.stderr, flush=True)

    def path(self, rel: str) -> Path:
        """workspace 기준 경로 (절대경로면 그대로)."""
        p = Path(rel)
        return p if p.is_absolute() else self.workspace / p


def main() -> None:
    init = json.loads(PROTO_IN.readline())
    sys.path.insert(0, str(Path(init["path"]).parent))
    ctx = Context(init["workspace"], init["download_dir"], init.get("args") or {})
    buf = io.StringIO()
    sys.stdout = buf  # 사용자 print 캡처 (프로토콜 채널과 분리)
    try:
        if init["mode"] == "tool":
            spec = importlib.util.spec_from_file_location("agent_custom_tool", init["path"])
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            if not hasattr(mod, "run"):
                raise RuntimeError("도구 코드에 run(args, ctx) 함수가 없습니다.")
            value = mod.run(ctx.args, ctx)
        else:
            code = Path(init["path"]).read_text(encoding="utf-8")
            g = {"__name__": "__agent_script__", "args": ctx.args, "ctx": ctx, "result": None}
            exec(compile(code, init["path"], "exec"), g)  # noqa: S102 - 승인된 스크립트만 실행
            value = g.get("result")
        out = buf.getvalue()
        _send({"type": "result", "value": value, "stdout": out[-MAX_STDOUT:]})
    except BaseException as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _send({"type": "error", "error": f"{type(e).__name__}: {e}", "traceback": tb[-8000:], "stdout": buf.getvalue()[-MAX_STDOUT:]})


if __name__ == "__main__":
    main()
