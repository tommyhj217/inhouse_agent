"""에이전트 내장 tool (MCP 서버 없이 에이전트가 직접 제공).

- 시간: get_current_time
- 사용자 확인: ask_user (웹/CLI 대화형 실행에서만 실제로 질문, 예약작업은 기본값 진행)
- 파일 상태: file_status, wait_for_download, list_downloads
- 파일 분석: analyze_file, read_file_text, query_table, extract_zip
- 결과 저장: save_file
- 스킬: use_skill, read_skill_file
- 다운로드 URL: download_url
- 동적 도구: create_tool, run_python
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from . import analyzers
from .files import FileWorkspace, file_status, format_status, wait_for_download
from .interaction import current_run
from .mcp_hub import BuiltinTool, ToolOutcome
from .skills import SkillStore

if TYPE_CHECKING:
    from .downloader import Downloader
    from .dynamic_tools import DynamicToolManager


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


def make_builtin_tools(
    ws: FileWorkspace,
    timezone: str,
    skills: SkillStore | None = None,
    downloader: "Downloader | None" = None,
    dyn: "DynamicToolManager | None" = None,
) -> list[BuiltinTool]:
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
    if downloader is not None:

        async def download_url(args: dict[str, Any]) -> ToolOutcome:
            from .downloader import DownloadError

            url = str(args["url"]).strip()
            if args.get("base_url") and not url.lower().startswith(("http://", "https://")):
                import urllib.parse

                url = urllib.parse.urljoin(str(args["base_url"]), url)
            try:
                info = await asyncio.to_thread(
                    downloader.download,
                    url,
                    filename=args.get("filename"),
                    subdir=str(args.get("subdir") or ""),
                    headers=args.get("headers"),
                    cookies=args.get("cookies"),
                    method=str(args.get("method") or "GET"),
                    data=args.get("data"),
                    referer=args.get("referer"),
                    overwrite=bool(args.get("overwrite")),
                )
            except (DownloadError, PermissionError, ValueError) as e:
                return ToolOutcome(f"오류: 다운로드 실패 - {e}\nURL: {url}", True)
            from pathlib import Path

            st = await file_status(Path(info["path"]), ws, stable_wait=0)
            ctype = (info.get("content_type") or "").lower()
            if st["state"] == "complete" and st.get("detected_type") == "html" and "html" in ctype and not str(args.get("filename") or "").lower().endswith((".html", ".htm")):
                # 다운로드 링크인데 웹 페이지가 응답됨 → 대부분 로그인/권한/만료 페이지
                head = Path(info["path"]).read_bytes()[:4000].decode("utf-8", errors="ignore").lower()
                hint = next((h for h in ("로그인", "login", "세션", "session", "권한", "denied", "만료", "expired", "error", "오류") if h in head), None)
                st["state"] = "suspicious"
                st.setdefault("issues", []).append(
                    "다운로드 링크인데 파일이 아닌 HTML 웹 페이지가 응답됨" + (f" ('{hint}' 문구 포함)" if hint else "")
                )
            lines = [
                f"다운로드 완료: {info['path']}",
                f"- HTTP {info['status']} · {info['size']:,} bytes · {info.get('content_type') or '-'} · {info['elapsed']}s"
                + (f" · 리다이렉트 → {info['final_url']}" if info.get("redirected") else ""),
                format_status(st),
            ]
            if st["state"] == "suspicious" and st.get("detected_type") == "html":
                lines.append(
                    "※ 파일 대신 HTML(로그인/오류 페이지)이 내려왔습니다. 인증이 필요한 링크로 보입니다. "
                    "MCP 에서 세션 쿠키를 얻을 수 있으면 cookies 인자로 다시 받거나, MCP 브라우저로 직접 다운로드하세요."
                )
            elif st["state"] == "complete":
                lines.append("※ analyze_file 로 내용을 분석할 수 있습니다.")
            return ToolOutcome("\n".join(lines), st["state"] in ("corrupt", "empty"), [st])

        tools.append(
            BuiltinTool(
                "download_url",
                "http(s) 다운로드 URL 의 파일을 workspace/downloads 에 받고 상태를 점검합니다. "
                "MCP 결과에 다운로드 링크가 있을 때 사용하세요. 한글 파일명, 리다이렉트, 사내 프록시를 지원합니다.",
                _obj(
                    {
                        "url": {"type": "string", "description": "다운로드 URL (상대경로면 base_url 필요)"},
                        "base_url": {"type": "string", "description": "url 이 상대경로일 때 기준 주소"},
                        "filename": {"type": "string", "description": "저장할 파일명(선택, 기본: 서버가 준 이름)"},
                        "subdir": {"type": "string", "description": "downloads 아래 하위 폴더(선택)"},
                        "cookies": {"type": "string", "description": "쿠키 문자열 'A=1; B=2' (로그인이 필요한 링크)"},
                        "headers": {"type": "object", "description": "추가 HTTP 헤더 (예: Authorization)"},
                        "method": {"type": "string", "enum": ["GET", "POST"]},
                        "data": {"type": "object", "description": "POST 폼 데이터"},
                        "referer": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                    ["url"],
                ),
                download_url,
            )
        )

    if dyn is not None and dyn.cfg.mode != "off":

        async def create_tool(args: dict[str, Any]) -> ToolOutcome:
            from .dynamic_tools import ToolCodeError, check_code

            name = str(args.get("name", "")).strip()
            code = str(args.get("code", ""))
            desc = str(args.get("description", "")).strip()
            params = args.get("parameters")
            test_args = args.get("test_args")
            if isinstance(test_args, str):
                try:
                    test_args = json.loads(test_args) if test_args.strip() else None
                except json.JSONDecodeError:
                    return ToolOutcome("오류: test_args 는 JSON 객체여야 합니다.", True)
            exists = (dyn.root / f"{name}.py").exists()
            if exists and not args.get("overwrite"):
                return ToolOutcome(f"오류: 도구 '{name}' 이(가) 이미 있습니다. 그대로 쓰려면 custom__{name} 을 호출하고, 고치려면 overwrite=true 로 다시 만드세요.", True)
            problems = check_code(code, dyn.cfg, require_run=True)
            if not desc:
                problems.append("description 이 비어 있습니다.")
            if problems:
                return ToolOutcome("오류: 코드 검사 실패 - 고쳐서 다시 만드세요:\n- " + "\n- ".join(problems), True)
            ok, why = await dyn.approve(
                f"🛠 에이전트가 새 도구 '{name}' 을(를) {'수정' if exists else '만들어'} 실행하려고 합니다. 승인할까요?",
                f"설명: {desc}\n인자: {json.dumps(params, ensure_ascii=False)}\n테스트 인자: {json.dumps(test_args, ensure_ascii=False)}\n\n--- 코드 ---\n{code[:6000]}",
            )
            if not ok:
                return ToolOutcome(f"도구를 만들지 않았습니다: {why}", True)
            last_test = None
            test_text = "(test_args 가 없어 테스트하지 않음 - 다음에는 test_args 를 넣으세요)"
            if test_args is not None:
                r = await dyn.test_draft(name, code, test_args if isinstance(test_args, dict) else {})
                test_text = dyn.format_result(r)
                last_test = {"ok": r["ok"], "args": test_args, "at": datetime.now().isoformat(timespec="seconds"), "elapsed": r["elapsed"]}
                if not r["ok"]:
                    return ToolOutcome(f"오류: 테스트 실행 실패 - 도구를 등록하지 않았습니다. 오류를 보고 코드를 고쳐 다시 create_tool 하세요.\n\n{test_text}", True)
            try:
                t = dyn.save(name, desc, params, code, created_by="agent", last_test=last_test)
            except ToolCodeError as e:
                return ToolOutcome(f"오류: {e}", True)
            return ToolOutcome(
                f"도구 등록 완료: custom__{t.name} (저장: {t.path})\n이제 custom__{t.name} 을 호출해서 사용하세요.\n\n[테스트 결과]\n{test_text}"
            )

        async def run_python(args: dict[str, Any]) -> ToolOutcome:
            code = str(args.get("code", ""))
            inputs = args.get("args") or {}
            ok, why = await dyn.approve(
                "🐍 에이전트가 파이썬 코드를 실행하려고 합니다. 승인할까요?",
                f"목적: {args.get('purpose', '-')}\n\n--- 코드 ---\n{code[:6000]}",
            )
            if not ok:
                return ToolOutcome(f"실행하지 않았습니다: {why}", True)
            timeout = min(float(args.get("timeout_sec") or dyn.cfg.timeout), 1800)
            r = await dyn.run_script(code, inputs if isinstance(inputs, dict) else {}, timeout)
            return ToolOutcome(dyn.format_result(r), not r["ok"])

        code_help = (
            "코드 규칙: 최상위에 def run(args, ctx): 를 정의하고 결과(dict/list/str)를 return. "
            "ctx.workspace(Path, 작업폴더), ctx.download_dir(Path), ctx.files_dir(Path), ctx.path('downloads/x.csv'), "
            "ctx.call_tool_data('tool이름', {...}) → 결과 dict/list (MCP 결과 파싱에 권장), ctx.call_tool(...) → 결과 텍스트, "
            "ctx.call_tool_files('download_url', {'url': ...}) → 받은 파일 [{path, state}], ctx.log('...'). "
            "from inhouse_agent import analyzers 로 analyzers.load_tables(path) (엑셀/CSV → [Table(name, columns, rows)], CSV 셀은 문자열), "
            "analyzers.to_number('1,234원') → 1234.0 사용 가능. "
            "subprocess/eval/파일삭제 등은 금지."
        )
        tools += [
            BuiltinTool(
                "create_tool",
                "필요한 tool 이 없을 때 파이썬으로 새 tool 을 만들어 등록합니다 (등록 후 custom__<name> 으로 호출, 다음 실행에도 재사용). "
                "test_args 로 바로 테스트하며, 실패하면 오류를 보고 고쳐서 다시 만드세요. " + code_help,
                _obj(
                    {
                        "name": {"type": "string", "description": "영문 소문자/숫자/_ (예: merge_monthly_csv)"},
                        "description": {"type": "string", "description": "무엇을 하는 tool 인지 (LLM 이 보고 고름)"},
                        "parameters": {"type": "object", "description": "인자 JSON 스키마 (type: object, properties, required)"},
                        "code": {"type": "string", "description": "def run(args, ctx): 를 포함한 파이썬 코드"},
                        "test_args": {"type": "object", "description": "등록 전 테스트 실행 인자"},
                        "overwrite": {"type": "boolean", "description": "같은 이름 도구를 고칠 때 true"},
                    },
                    ["name", "description", "parameters", "code", "test_args"],
                ),
                create_tool,
            ),
            BuiltinTool(
                "run_python",
                "일회성 파이썬 코드를 별도 프로세스에서 실행합니다 (데이터 가공, 계산, 파일 변환/병합 등). "
                "결과는 변수 result 에 넣거나 print 하세요. 전역 변수 args, ctx 사용 가능 (ctx 는 create_tool 과 동일). "
                "반복해서 쓸 기능이면 create_tool 을 쓰세요.",
                _obj(
                    {
                        "code": {"type": "string"},
                        "purpose": {"type": "string", "description": "이 코드로 하려는 일 (승인 화면에 표시)"},
                        "args": {"type": "object", "description": "코드에 넘길 값"},
                        "timeout_sec": {"type": "number"},
                    },
                    ["code", "purpose"],
                ),
                run_python,
            ),
        ]
    return tools
