"""에이전트 내장 tool (MCP 서버 없이 에이전트가 직접 제공).

- 시간: get_current_time
- 사용자 확인: ask_user (웹/CLI 대화형 실행에서만 실제로 질문, 예약작업은 기본값 진행)
- 파일 상태: file_status, wait_for_download, list_downloads
- 파일 분석: analyze_file, read_file_text, query_table, extract_zip
- 결과 저장: save_file
- 스킬: use_skill, read_skill_file
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import analyzers
from .files import FileWorkspace, file_status, format_status, wait_for_download
from .interaction import current_run
from .mcp_hub import BuiltinTool, ToolOutcome
from .skills import SkillStore


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


def make_builtin_tools(ws: FileWorkspace, timezone: str, skills: SkillStore | None = None) -> list[BuiltinTool]:
    tz = ZoneInfo(timezone)

    # ---------------- 시간 / 사용자 ----------------
    async def get_current_time(_: dict[str, Any]) -> str:
        return datetime.now(tz).strftime(f"%Y-%m-%d %H:%M:%S ({timezone}, %A)")

    async def ask_user(args: dict[str, Any]) -> str:
        ctx = current_run.get()
        question = str(args.get("question", "")).strip()
        options = [str(o) for o in args.get("options") or []] or None
        default = args.get("default")
        if ctx is None or ctx.ask is None:
            return (
                "사용자에게 질문할 수 없는 실행(예약작업/일괄 실행)입니다. "
                + (f"기본값 '{default}' 로 진행하세요." if default else "합리적인 기본값으로 판단해 진행하고, 결과에 가정한 내용을 밝히세요.")
            )
        answer = await ctx.ask(question, options, str(default) if default is not None else None)
        return f"사용자 답변: {answer}" if answer.strip() else "사용자가 답하지 않았습니다(빈 답변). 기본값으로 진행하세요."

    # ---------------- 파일 상태 ----------------
    async def file_status_tool(args: dict[str, Any]) -> ToolOutcome:
        p = ws.resolve(args["path"])
        st = await file_status(p, ws, stable_wait=float(args.get("stable_wait_sec") or 1.0))
        return ToolOutcome(format_status(st) + "\n" + json.dumps(st, ensure_ascii=False, indent=1), False, [st])

    async def wait_tool(args: dict[str, Any]) -> ToolOutcome:
        timeout = min(float(args.get("timeout_sec") or 120), 1800)
        newer = args.get("newer_than_minutes")
        since = time.time() - float(newer) * 60 if newer is not None else None
        directory = ws.resolve(args["directory"]) if args.get("directory") else None
        st = await wait_for_download(ws, str(args.get("pattern") or "*"), timeout, since, directory)
        return ToolOutcome(format_status(st) + "\n" + json.dumps(st, ensure_ascii=False, indent=1), st["state"] == "timeout", [st])

    async def list_downloads(args: dict[str, Any]) -> str:
        files = ws.list_files(str(args.get("pattern") or "*"), args.get("recent_minutes"), str(args.get("where") or "all"))
        if not files:
            return "(파일 없음)  감시 폴더: " + ", ".join(str(d) for d in ws.watch_dirs)
        lines = []
        for p in files[: int(args.get("limit") or 50)]:
            stt = p.stat()
            lines.append(f"{ws.display(p)}\t{stt.st_size:,} bytes\t{datetime.fromtimestamp(stt.st_mtime):%Y-%m-%d %H:%M:%S}")
        return "\n".join(lines)

    # ---------------- 파일 분석 ----------------
    async def _checked(path: str):
        p = ws.resolve(path)
        st = await file_status(p, ws, stable_wait=0.5)
        if st["state"] in ("missing", "downloading", "empty", "too_large", "locked", "directory"):
            raise RuntimeError("분석 불가 - " + format_status(st))
        return p, st

    async def analyze_file(args: dict[str, Any]) -> ToolOutcome:
        p, st = await _checked(args["path"])
        text = await asyncio.to_thread(
            analyzers.analyze, p, args.get("sheet"), int(args.get("sample_rows") or 10), int(args.get("max_chars") or 6000)
        )
        warn = ""
        if st["state"] in ("suspicious", "corrupt"):
            warn = "⚠️ 파일 상태 경고:\n" + format_status(st) + "\n\n"
        return ToolOutcome(warn + text, False, [st])

    async def read_file_text(args: dict[str, Any]) -> str:
        p, _ = await _checked(args["path"])
        return await asyncio.to_thread(analyzers.read_text_range, p, int(args.get("start") or 0), min(int(args.get("max_chars") or 8000), 30000))

    async def query_table(args: dict[str, Any]) -> str:
        p, _ = await _checked(args["path"])
        return await asyncio.to_thread(
            analyzers.query_table,
            p,
            args.get("sheet"),
            args.get("columns"),
            args.get("filters"),
            args.get("group_by"),
            args.get("aggregates"),
            args.get("sort_by"),
            bool(args.get("descending")),
            min(int(args.get("limit") or 50), 500),
            int(args.get("offset") or 0),
        )

    async def extract_zip(args: dict[str, Any]) -> str:
        p, _ = await _checked(args["path"])
        dest = ws.resolve(args.get("dest") or f"downloads/{p.stem}", write=True)
        out = await asyncio.to_thread(analyzers.extract_zip, p, dest)
        return f"{len(out)}개 파일을 {dest} 에 풀었습니다:\n" + "\n".join(ws.display(x) for x in out[:100])

    # ---------------- 저장 ----------------
    async def save_file(args: dict[str, Any]) -> str:
        rel = str(args["path"]).lstrip("/\\")
        path = ws.resolve(f"files/{rel}" if not rel.startswith("files/") else rel, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.get("append") else "w"
        encoding = "utf-8-sig" if path.suffix.lower() == ".csv" and mode == "w" else "utf-8"  # 엑셀 호환 BOM
        with open(path, mode, encoding=encoding, newline="") as f:
            f.write(str(args.get("content", "")))
        return f"저장 완료: {path}"

    tools = [
        BuiltinTool("get_current_time", "현재 날짜와 시각을 조회합니다. '오늘/어제/이번 주' 계산에 사용하세요.", _obj({}), get_current_time),
        BuiltinTool(
            "ask_user",
            "판단이 애매하거나 사용자 확인이 필요할 때(조회 조건 선택, 여러 파일 중 선택, 결과 확인 등) 사용자에게 질문하고 답을 받습니다. "
            "꼭 필요할 때만 사용하세요.",
            _obj(
                {
                    "question": {"type": "string", "description": "사용자에게 할 질문"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "선택지(선택)"},
                    "default": {"type": "string", "description": "답이 없을 때 쓸 기본값(선택)"},
                },
                ["question"],
            ),
            ask_user,
        ),
        BuiltinTool(
            "file_status",
            "파일 상태를 점검합니다: 존재 여부, 크기, 실제 형식(확장자와 일치하는지), 다운로드 진행 중인지(임시파일/크기 변화), "
            "손상 여부(ZIP/PDF 무결성), 로그인·오류 페이지가 파일로 저장된 것인지. 경로는 절대경로 또는 workspace 기준 상대경로.",
            _obj({"path": {"type": "string"}, "stable_wait_sec": {"type": "number", "description": "크기 변화 확인 대기(기본 1초)"}}, ["path"]),
            file_status_tool,
        ),
        BuiltinTool(
            "wait_for_download",
            "다운로드 폴더에 새 파일이 생기고 다운로드가 끝날 때까지 기다린 뒤 상태를 알려줍니다. "
            "MCP tool 로 다운로드 버튼을 누른 직후 사용하세요.",
            _obj(
                {
                    "pattern": {"type": "string", "description": "파일명 패턴 예: *.xlsx, 매출*.csv (기본 *)"},
                    "timeout_sec": {"type": "number", "description": "최대 대기 시간(기본 120초)"},
                    "newer_than_minutes": {"type": "number", "description": "최근 N분 안에 생긴 파일만 (기본 10분)"},
                    "directory": {"type": "string", "description": "특정 폴더만 감시(선택)"},
                }
            ),
            wait_tool,
        ),
        BuiltinTool(
            "list_downloads",
            "다운로드/업로드/저장 폴더의 파일 목록을 최신순으로 보여줍니다.",
            _obj(
                {
                    "pattern": {"type": "string", "description": "파일명 패턴 (기본 *)"},
                    "recent_minutes": {"type": "number", "description": "최근 N분 이내 파일만"},
                    "where": {"type": "string", "enum": ["all", "downloads", "uploads", "files"]},
                    "limit": {"type": "integer"},
                }
            ),
            list_downloads,
        ),
        BuiltinTool(
            "analyze_file",
            "파일 내용을 분석합니다. 엑셀(xlsx/xls/HTML표 xls)·CSV 는 시트별 행/열 수, 열별 통계, 샘플 행을, "
            "PDF·Word·PPT·텍스트는 추출한 텍스트를, ZIP 은 목록을 돌려줍니다. 분석 전에 상태도 자동 점검합니다.",
            _obj(
                {
                    "path": {"type": "string"},
                    "sheet": {"type": "string", "description": "특정 시트만(선택)"},
                    "sample_rows": {"type": "integer", "description": "샘플 행 수(기본 10)"},
                    "max_chars": {"type": "integer", "description": "문서 텍스트 최대 글자(기본 6000)"},
                },
                ["path"],
            ),
            analyze_file,
        ),
        BuiltinTool(
            "read_file_text",
            "문서/표 파일의 텍스트를 구간별로 읽습니다 (analyze_file 결과가 잘렸을 때 start 로 이어 읽기).",
            _obj({"path": {"type": "string"}, "start": {"type": "integer"}, "max_chars": {"type": "integer"}}, ["path"]),
            read_file_text,
        ),
        BuiltinTool(
            "query_table",
            "엑셀/CSV 표 데이터를 필터·정렬·그룹 집계합니다. 큰 파일에서 필요한 부분만 정확히 계산할 때 사용하세요. "
            "filters 예: [{\"column\":\"부서\",\"op\":\"==\",\"value\":\"IT운영팀\"},{\"column\":\"금액\",\"op\":\">\",\"value\":1000000}] "
            "op: == != > >= < <= contains not_contains in startswith empty not_empty. "
            "aggregates 예: [{\"column\":\"금액\",\"func\":\"sum\"}] func: count sum avg min max count_distinct.",
            _obj(
                {
                    "path": {"type": "string"},
                    "sheet": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}, "description": "보여줄 열"},
                    "filters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "op": {"type": "string"},
                                "value": {"description": "비교값 (문자/숫자, in 은 배열 또는 쉼표구분)"},
                            },
                            "required": ["column", "op"],
                        },
                    },
                    "group_by": {"type": "array", "items": {"type": "string"}},
                    "aggregates": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"column": {"type": "string"}, "func": {"type": "string"}}, "required": ["func"]},
                    },
                    "sort_by": {"type": "string"},
                    "descending": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                ["path"],
            ),
            query_table,
        ),
        BuiltinTool(
            "extract_zip",
            "ZIP 파일을 workspace 안에 풉니다 (한글 파일명 지원). 푼 뒤 각 파일을 analyze_file 로 분석하세요.",
            _obj({"path": {"type": "string"}, "dest": {"type": "string", "description": "풀 위치(workspace 기준, 선택)"}}, ["path"]),
            extract_zip,
        ),
        BuiltinTool(
            "save_file",
            "정리한 결과를 workspace/files 아래에 파일로 저장합니다 (md, csv, json, txt 등).",
            _obj(
                {
                    "path": {"type": "string", "description": "files 폴더 기준 상대 경로. 예: 2026-11-01/매출.csv"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean", "description": "true 면 이어쓰기"},
                },
                ["path", "content"],
            ),
            save_file,
        ),
    ]

    if skills is not None:

        async def use_skill(args: dict[str, Any]) -> str:
            sk = skills.get(str(args["name"]))
            ctx = current_run.get()
            tz_name = timezone
            inputs = {}
            try:
                inputs = sk.resolve_inputs(args.get("inputs") or {}, tz_name)
            except Exception as e:  # noqa: BLE001
                inputs_note = f"(입력값 확인 필요: {e} - ask_user 로 물어보거나 기본값 사용)"
            else:
                inputs_note = json.dumps(inputs, ensure_ascii=False)
            if ctx is not None:
                ctx.meta.setdefault("skills_used", []).append(sk.name)
            files = ", ".join(sk.files) or "(없음)"
            return (
                f"# 스킬: {sk.name}\n설명: {sk.description}\n입력값: {inputs_note}\n"
                f"추가 파일: {files} (read_skill_file 로 읽기)\n\n## 지침 (이 절차를 따르세요)\n{sk.render(inputs, tz_name)}"
            )

        async def read_skill_file(args: dict[str, Any]) -> str:
            return skills.read_file(str(args["name"]), str(args["path"]))

        tools += [
            BuiltinTool(
                "use_skill",
                "등록된 스킬의 지침(절차)을 불러옵니다. 요청이 스킬 설명과 맞으면 작업 전에 먼저 호출해서 지침을 따르세요.",
                _obj({"name": {"type": "string"}, "inputs": {"type": "object", "description": "스킬 입력값(선택)"}}, ["name"]),
                use_skill,
            ),
            BuiltinTool(
                "read_skill_file",
                "스킬 폴더 안의 참고 파일(템플릿, 기준표 등)을 읽습니다.",
                _obj({"name": {"type": "string"}, "path": {"type": "string"}}, ["name", "path"]),
                read_skill_file,
            ),
        ]
    return tools
