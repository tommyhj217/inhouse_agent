"""에이전트 내장 tool: 현재 시각 조회, 결과 파일 저장/조회 (outputs 폴더 안으로 제한)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .mcp_hub import BuiltinTool


def _safe_path(root: Path, rel: str) -> Path:
    root = root.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError("outputs 폴더 밖의 경로는 사용할 수 없습니다.")
    return p


def make_builtin_tools(output_root: Path, timezone: str) -> list[BuiltinTool]:
    files_root = output_root / "files"
    tz = ZoneInfo(timezone)

    async def get_current_time(_: dict[str, Any]) -> str:
        now = datetime.now(tz)
        return now.strftime(f"%Y-%m-%d %H:%M:%S ({timezone}, %A)")

    async def save_file(args: dict[str, Any]) -> str:
        path = _safe_path(files_root, str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.get("append") else "w"
        # CSV 를 엑셀에서 바로 열 수 있도록 BOM 포함
        encoding = "utf-8-sig" if path.suffix.lower() == ".csv" and mode == "w" else "utf-8"
        with open(path, mode, encoding=encoding, newline="") as f:
            f.write(str(args.get("content", "")))
        return f"저장 완료: {path}"

    async def read_file(args: dict[str, Any]) -> str:
        path = _safe_path(files_root, str(args["path"]))
        if not path.is_file():
            return f"파일이 없습니다: {args['path']}"
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        limit = int(args.get("max_chars") or 20000)
        return text if len(text) <= limit else text[:limit] + f"\n...(총 {len(text)}자 중 {limit}자만 표시)"

    async def list_files(args: dict[str, Any]) -> str:
        base = _safe_path(files_root, str(args.get("path") or "."))
        if not base.exists():
            return "(저장된 파일 없음)"
        items = sorted(p for p in base.rglob("*") if p.is_file())
        root = files_root.resolve()
        return "\n".join(f"{p.relative_to(root)}\t{p.stat().st_size} bytes" for p in items[:500]) or "(저장된 파일 없음)"

    return [
        BuiltinTool(
            "get_current_time",
            "현재 날짜와 시각을 조회합니다. '오늘', '어제', '이번 주' 등 날짜 계산이 필요할 때 사용하세요.",
            {"type": "object", "properties": {}},
            get_current_time,
        ),
        BuiltinTool(
            "save_file",
            "수집/정리한 자료를 outputs/files 아래에 파일로 저장합니다 (md, csv, json, txt 등).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "outputs/files 기준 상대 경로. 예: 2026-11-01/매출.csv"},
                    "content": {"type": "string", "description": "파일 내용"},
                    "append": {"type": "boolean", "description": "true 면 기존 파일 뒤에 이어쓰기"},
                },
                "required": ["path", "content"],
            },
            save_file,
        ),
        BuiltinTool(
            "read_file",
            "outputs/files 아래에 저장된 파일을 읽습니다 (이전 수집 결과와 비교할 때 등).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "최대 반환 글자 수 (기본 20000)"},
                },
                "required": ["path"],
            },
            read_file,
        ),
        BuiltinTool(
            "list_files",
            "outputs/files 아래에 저장된 파일 목록을 조회합니다.",
            {"type": "object", "properties": {"path": {"type": "string", "description": "하위 폴더(선택)"}}},
            list_files,
        ),
    ]
