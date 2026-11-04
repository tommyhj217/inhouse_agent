"""범용 자동화 도구 모음 (공개 자동화 에이전트들이 공통으로 제공하는 도구를 사내 환경에 맞게 구현).

분석 근거와 선택/제외 이유: docs/agent_tools_analysis.md

| 분류        | tool                                   |
|-------------|----------------------------------------|
| 웹/HTTP     | web_fetch, web_search(설정 시), http_request |
| 데이터      | sql_query, compare_tables              |
| 결과물      | write_excel, make_chart, create_zip, get_download_link |
| 파일 검색   | search_files                           |
| 기억/계획   | remember, recall, forget, update_plan  |
| 알림        | send_email(설정 시), send_message(설정 시) |
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import analyzers
from .files import FileWorkspace, file_status, format_status
from .interaction import current_run
from .mcp_hub import BuiltinTool, ToolOutcome

log = logging.getLogger(__name__)


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


@dataclass
class ToolkitConfig:
    public_url: str = ""
    link_expire_hours: float = 168
    web_search: dict[str, Any] = field(default_factory=dict)  # provider: searxng|tavily, url, api_key
    http_unattended_write: bool = False  # 예약작업에서 POST/PUT/DELETE 허용
    notify: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ToolkitConfig":
        server = raw.get("server") or {}
        http = raw.get("http") or {}
        return cls(
            public_url=str(server.get("public_url") or "").strip(),
            link_expire_hours=float(server.get("link_expire_hours") or 168),
            web_search=dict(raw.get("web_search") or {}),
            http_unattended_write=bool(http.get("allow_unattended_write", False)),
            notify=dict(raw.get("notify") or {}),
        )


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


async def _confirm(title: str, detail: str, *, unattended_ok: bool) -> tuple[bool, str]:
    """외부로 나가거나 데이터를 바꾸는 작업 확인. 대화형이면 사용자에게 묻고, 무인 실행이면 unattended_ok 에 따름."""
    ctx = current_run.get()
    if ctx is not None and ctx.ask is not None:
        ans = await ctx.ask(f"{title}\n\n{detail}", ["승인", "거부"], "거부")
        ok = ans.strip() in ("승인", "예", "yes", "y", "ok", "허용")
        return ok, "사용자 승인" if ok else f"사용자가 거부했습니다 (답변: {ans})"
    if unattended_ok:
        return True, "무인 실행 허용(설정)"
    return False, "사용자 확인이 필요한 작업인데 무인 실행(예약작업)이라 수행하지 않았습니다."


def _md_table(cols: list[str], rows: list[list[Any]], limit: int = 50) -> str:
    def c(v: Any) -> str:
        if v is None:
            s = ""
        elif isinstance(v, bool):
            s = str(v)
        elif isinstance(v, int):
            s = f"{v:,}"
        elif isinstance(v, float):
            s = f"{int(v):,}" if v.is_integer() else f"{v:,.4g}"
        else:
            s = str(v)
        return s.replace("|", "/").replace("\n", " ")[:60]

    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(c(v) for v in r) + " |" for r in rows[:limit]]
    return "\n".join(out)


def _cell_value(v: Any) -> Any:
    """SQL 적재용: 숫자처럼 보이면 숫자로 (단 '00123' 같은 코드는 문자 유지)."""
    if isinstance(v, str):
        s = v.strip()
        if len(s) > 1 and s.startswith("0") and not s.startswith("0.") and s.replace(",", "").isdigit():
            return s
        n = analyzers.to_number(s)
        if n is not None:
            return int(n) if n.is_integer() and "." not in s else n
        return s
    return v


def _safe_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _korean_font() -> str | None:
    try:
        from matplotlib import font_manager
    except ImportError:
        return None
    names = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("Malgun Gothic", "맑은 고딕", "AppleGothic", "Apple SD Gothic Neo", "NanumGothic", "NanumBarunGothic", "Noto Sans CJK KR", "Noto Sans KR", "UnDotum"):
        if cand in names:
            return cand
    return None


# ---------------------------------------------------------------------------
# 도구 생성
# ---------------------------------------------------------------------------


def make_toolkit(ws: FileWorkspace, downloader: Any, signer: Any, cfg: ToolkitConfig, timezone: str) -> list[BuiltinTool]:
    tools: list[BuiltinTool] = []

    async def file_result(text: str, path: Path) -> ToolOutcome:
        st = await file_status(path, ws, stable_wait=0)
        try:
            link = signer.url(path)
            text += f"\n다운로드 링크: {link}"
        except Exception:  # noqa: BLE001
            pass
        return ToolOutcome(text + "\n" + format_status(st), False, [st])

    def out_path(rel: str, default_ext: str) -> Path:
        rel = str(rel).lstrip("/\\")
        if not Path(rel).suffix:
            rel += default_ext
        p = ws.resolve(rel if rel.startswith(("files/", "downloads/", "uploads/")) else f"files/{rel}", write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ======================= 웹 / HTTP =======================
    async def web_fetch(args: dict[str, Any]) -> str:
        r = await asyncio.to_thread(downloader.request, str(args["url"]), headers=args.get("headers"), cookies=args.get("cookies"))
        ctype = (r["headers"].get("Content-Type") or r["headers"].get("content-type") or "").lower()
        body: bytes = r["body"]
        head = f"[{r['status']}] {r['final_url']} ({len(body):,} bytes{', 잘림' if r['truncated'] else ''}, {ctype or '-'})"
        if any(t in ctype for t in ("pdf", "spreadsheet", "excel", "msword", "officedocument", "zip", "octet-stream", "image/")):
            return head + "\n파일 형식 응답입니다. download_url 로 받은 뒤 analyze_file 로 분석하세요."
        text = body.decode(_charset(ctype, body), errors="replace")
        start = int(args.get("start") or 0)
        limit = min(int(args.get("max_chars") or 8000), 40000)
        if "json" in ctype:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
            except ValueError:
                pass
            return head + "\n" + text[start : start + limit]
        if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            title, content, links = _html_to_text(text, r["final_url"])
            out = [head, f"제목: {title}" if title else ""]
            chunk = content[start : start + limit]
            out.append(chunk)
            if start + limit < len(content):
                out.append(f"...(전체 {len(content):,}자, 다음 start={start + limit})")
            if args.get("include_links", True) and links:
                out.append("\n[링크]\n" + "\n".join(f"- {t[:60]} → {u}" for t, u in links[:60]))
            return "\n".join(x for x in out if x)
        return head + "\n" + text[start : start + limit]

    tools.append(
        BuiltinTool(
            "web_fetch",
            "웹 페이지/사내 포털 페이지/REST GET 응답을 읽어 본문 텍스트와 링크 목록을 돌려줍니다 (브라우저 조작 없이 빠르게 읽을 때). "
            "로그인이 필요한 페이지는 cookies 를 넘기세요. 파일이면 download_url 을 쓰세요.",
            _obj(
                {
                    "url": {"type": "string"},
                    "start": {"type": "integer", "description": "본문 시작 위치(이어 읽기)"},
                    "max_chars": {"type": "integer", "description": "최대 글자 수 (기본 8000)"},
                    "include_links": {"type": "boolean"},
                    "cookies": {"type": "string"},
                    "headers": {"type": "object"},
                },
                ["url"],
            ),
            web_fetch,
        )
    )

    async def http_request(args: dict[str, Any]) -> str:
        method = str(args.get("method") or "GET").upper()
        url = str(args["url"])
        if method not in ("GET", "HEAD", "OPTIONS"):
            ok, why = await _confirm(
                f"🌐 에이전트가 {method} 요청을 보내려고 합니다 (데이터가 바뀔 수 있음). 승인할까요?",
                f"URL: {url}\n본문: {json.dumps(args.get('json') or args.get('form') or args.get('body'), ensure_ascii=False)[:2000]}",
                unattended_ok=cfg.http_unattended_write,
            )
            if not ok:
                return f"요청하지 않았습니다: {why}"
        r = await asyncio.to_thread(
            downloader.request, url, method=method, params=args.get("params"), headers=args.get("headers"),
            cookies=args.get("cookies"), json_body=args.get("json"), form=args.get("form"), body=args.get("body"),
            timeout=args.get("timeout_sec"),
        )
        ctype = (r["headers"].get("Content-Type") or r["headers"].get("content-type") or "")
        keep = {k: v for k, v in r["headers"].items() if k.lower() in ("content-type", "content-length", "location", "content-disposition", "x-total-count", "link", "retry-after")}
        text = r["body"].decode(_charset(ctype.lower(), r["body"]), errors="replace")
        if "json" in ctype.lower():
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
            except ValueError:
                pass
        limit = min(int(args.get("max_chars") or 20000), 60000)
        out = f"HTTP {r['status']} {r['reason']} ({r['elapsed']}s) {r['final_url']}\n헤더: {json.dumps(keep, ensure_ascii=False)}\n\n{text[:limit]}"
        if len(text) > limit or r["truncated"]:
            out += f"\n...(응답이 길어 {limit}자까지만 표시)"
        return out

    tools.append(
        BuiltinTool(
            "http_request",
            "사내 REST API 등에 HTTP 요청을 보냅니다 (GET/POST/PUT/PATCH/DELETE). JSON 응답은 보기 좋게 정리됩니다. "
            "GET 이 아닌 요청은 사용자 승인 후 실행됩니다.",
            _obj(
                {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
                    "params": {"type": "object", "description": "쿼리 파라미터"},
                    "headers": {"type": "object"},
                    "json": {"description": "JSON 본문"},
                    "form": {"type": "object", "description": "폼 본문"},
                    "body": {"type": "string", "description": "원문 본문"},
                    "cookies": {"type": "string"},
                    "timeout_sec": {"type": "number"},
                    "max_chars": {"type": "integer"},
                },
                ["url"],
            ),
            http_request,
        )
    )

    ws_cfg = cfg.web_search
    provider = str(ws_cfg.get("provider") or "").lower()
    if provider in ("searxng", "tavily"):

        async def web_search(args: dict[str, Any]) -> str:
            q = str(args["query"])
            n = min(int(args.get("max_results") or 8), 20)
            if provider == "searxng":
                base = str(ws_cfg.get("url") or "").rstrip("/")
                r = await asyncio.to_thread(downloader.request, f"{base}/search", params={"q": q, "format": "json", "language": ws_cfg.get("language", "ko")})
                data = json.loads(r["body"] or b"{}")
                items = [(i.get("title", ""), i.get("url", ""), i.get("content", "")) for i in data.get("results", [])[:n]]
            else:
                r = await asyncio.to_thread(
                    downloader.request, str(ws_cfg.get("url") or "https://api.tavily.com/search"), method="POST",
                    json_body={"api_key": ws_cfg.get("api_key"), "query": q, "max_results": n},
                )
                data = json.loads(r["body"] or b"{}")
                items = [(i.get("title", ""), i.get("url", ""), i.get("content", "")) for i in data.get("results", [])[:n]]
            if r["status"] >= 400:
                return f"오류: 검색 실패 HTTP {r['status']}"
            if not items:
                return "검색 결과가 없습니다."
            return "\n\n".join(f"{i}. {t}\n   {u}\n   {c[:300]}" for i, (t, u, c) in enumerate(items, 1)) + "\n\n※ 내용을 읽으려면 web_fetch 를 사용하세요."

        tools.append(
            BuiltinTool(
                "web_search",
                "웹/사내 검색엔진에서 검색해 제목·URL·요약을 돌려줍니다. 자세한 내용은 web_fetch 로 읽으세요.",
                _obj({"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"]),
                web_search,
            )
        )

    # ======================= 데이터 =======================
    async def sql_query(args: dict[str, Any]) -> ToolOutcome:
        sql = str(args["sql"]).strip().rstrip(";")
        if not re.match(r"^(select|with)\b", sql, re.I):
            return ToolOutcome("오류: SELECT / WITH 조회만 할 수 있습니다.", True)

        def run() -> tuple[list[str], list[list[Any]], list[str]]:
            con = sqlite3.connect(":memory:")
            notes = []
            for t in args.get("tables") or []:
                alias = re.sub(r"\W", "_", str(t["alias"]))
                p = ws.resolve(t["path"])
                tables = analyzers.load_tables(p)
                if not tables:
                    raise ValueError(f"{p.name}: 표 데이터가 없습니다.")
                tb = next((x for x in tables if x.name == t.get("sheet")), tables[0]) if t.get("sheet") else tables[0]
                cols = ", ".join(_safe_ident(c) for c in tb.columns)
                con.execute(f"CREATE TABLE {_safe_ident(alias)} ({cols})")
                ph = ", ".join("?" * len(tb.columns))
                con.executemany(
                    f"INSERT INTO {_safe_ident(alias)} VALUES ({ph})",
                    [[_cell_value(r[i]) if i < len(r) else None for i in range(len(tb.columns))] for r in tb.rows],
                )
                notes.append(f"{alias} ← {p.name}[{tb.name}] {len(tb.rows)}행, 열: {', '.join(tb.columns)}")
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = [list(r) for r in cur.fetchmany(10000)]
            con.close()
            return cols, rows, notes

        try:
            cols, rows, notes = await asyncio.to_thread(run)
        except (sqlite3.Error, ValueError, KeyError) as e:
            return ToolOutcome(f"오류: {type(e).__name__}: {e}", True)
        limit = min(int(args.get("limit") or 100), 1000)
        text = "\n".join(notes) + f"\n\n결과 {len(rows):,}행" + (f" (앞 {limit}행 표시)" if len(rows) > limit else "") + "\n" + _md_table(cols, rows, limit)
        if args.get("save_as"):
            p = out_path(args["save_as"], ".csv")
            await asyncio.to_thread(_save_table, p, cols, rows)
            return await file_result(text + f"\n\n저장: {p}", p)
        return ToolOutcome(text)

    tools.append(
        BuiltinTool(
            "sql_query",
            "엑셀/CSV 파일들을 임시 테이블로 올려 SQL(SQLite 문법)로 조회·조인·집계합니다. 여러 파일 조인이나 복잡한 집계에 사용. "
            "열 이름에 한글/공백이 있으면 \"열 이름\" 처럼 큰따옴표로 감싸세요. save_as 로 결과를 csv/xlsx 저장 가능.",
            _obj(
                {
                    "sql": {"type": "string", "description": 'SELECT 문. 예: SELECT "부서", SUM("금액") AS 합계 FROM s GROUP BY 1 ORDER BY 2 DESC'},
                    "tables": {
                        "type": "array",
                        "items": _obj({"alias": {"type": "string"}, "path": {"type": "string"}, "sheet": {"type": "string"}}, ["alias", "path"]),
                    },
                    "limit": {"type": "integer"},
                    "save_as": {"type": "string", "description": "결과 저장 경로 (files 기준, .csv 또는 .xlsx)"},
                },
                ["sql", "tables"],
            ),
            sql_query,
        )
    )

    async def compare_tables(args: dict[str, Any]) -> str:
        def run() -> str:
            a = _pick(analyzers.load_tables(ws.resolve(args["old_path"])), args.get("old_sheet"))
            b = _pick(analyzers.load_tables(ws.resolve(args["new_path"])), args.get("new_sheet"))
            keys = list(args.get("key_columns") or [])
            for k in keys:
                if k not in a.columns or k not in b.columns:
                    raise ValueError(f"키 열 '{k}' 이(가) 양쪽에 모두 있어야 합니다. 이전: {a.columns} / 이후: {b.columns}")
            common = [c for c in b.columns if c in a.columns]

            def key(t: Any, r: list[Any]) -> tuple:
                return tuple(str(r[t.columns.index(k)]) for k in keys) if keys else tuple(map(str, r))

            ma = {key(a, r): r for r in a.rows}
            mb = {key(b, r): r for r in b.rows}
            added = [k for k in mb if k not in ma]
            removed = [k for k in ma if k not in mb]
            changed = []
            if keys:
                for k in mb.keys() & ma.keys():
                    diffs = [
                        (c, ma[k][a.columns.index(c)], mb[k][b.columns.index(c)])
                        for c in common
                        if str(ma[k][a.columns.index(c)]) != str(mb[k][b.columns.index(c)])
                    ]
                    if diffs:
                        changed.append((k, diffs))
            n = int(args.get("max_rows") or 20)
            out = [
                f"이전 {len(a.rows)}행 → 이후 {len(b.rows)}행 (키: {', '.join(keys) or '행 전체'})",
                f"추가 {len(added)} / 삭제 {len(removed)} / 변경 {len(changed)}",
            ]
            if set(a.columns) != set(b.columns):
                out.append(f"열 변화: 추가 {[c for c in b.columns if c not in a.columns]}, 삭제 {[c for c in a.columns if c not in b.columns]}")
            if added:
                out.append("\n[추가된 행]\n" + _md_table(b.columns, [mb[k] for k in added], n))
            if removed:
                out.append("\n[삭제된 행]\n" + _md_table(a.columns, [ma[k] for k in removed], n))
            if changed:
                out.append("\n[변경된 값]\n" + "\n".join(f"- {'/'.join(k)}: " + ", ".join(f"{c}: {o} → {v}" for c, o, v in d) for k, d in changed[:n]))
            return "\n".join(out)

        return await asyncio.to_thread(run)

    tools.append(
        BuiltinTool(
            "compare_tables",
            "두 엑셀/CSV 파일(예: 어제 자료 vs 오늘 자료)을 비교해 추가/삭제/변경된 행을 알려줍니다. key_columns 를 주면 행 단위 값 변경까지 찾습니다.",
            _obj(
                {
                    "old_path": {"type": "string"},
                    "new_path": {"type": "string"},
                    "key_columns": {"type": "array", "items": {"type": "string"}, "description": "행을 식별하는 열 (예: [\"문서번호\"])"},
                    "old_sheet": {"type": "string"},
                    "new_sheet": {"type": "string"},
                    "max_rows": {"type": "integer"},
                },
                ["old_path", "new_path"],
            ),
            compare_tables,
        )
    )

    # ======================= 결과물 =======================
    async def write_excel(args: dict[str, Any]) -> ToolOutcome:
        p = out_path(args["path"], ".xlsx")
        sheets = args.get("sheets") or []
        if args.get("from_files"):
            for f in args["from_files"]:
                t = _pick(analyzers.load_tables(ws.resolve(f["path"])), f.get("sheet"))
                sheets.append({"name": f.get("name") or Path(f["path"]).stem[:30], "columns": t.columns, "rows": [[_cell_value(v) for v in r] for r in t.rows]})
        if not sheets:
            return ToolOutcome("오류: sheets 또는 from_files 가 필요합니다.", True)
        await asyncio.to_thread(_write_xlsx, p, sheets, args.get("title"))
        total = sum(len(s.get("rows") or []) for s in sheets)
        return await file_result(f"엑셀 저장 완료: {p}\n- 시트 {len(sheets)}개, 총 {total:,}행", p)

    tools.append(
        BuiltinTool(
            "write_excel",
            "표 데이터를 서식(머리글 굵게, 틀 고정, 필터, 열 너비, 숫자 천단위)이 적용된 엑셀(xlsx) 보고서로 저장합니다. "
            "sheets 로 직접 데이터를 주거나 from_files 로 CSV/엑셀을 시트로 합칠 수 있습니다.",
            _obj(
                {
                    "path": {"type": "string", "description": "files 기준 경로. 예: reports/10월_매출.xlsx"},
                    "title": {"type": "string", "description": "첫 행 제목(선택)"},
                    "sheets": {
                        "type": "array",
                        "items": _obj(
                            {"name": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}}, "rows": {"type": "array", "items": {"type": "array"}}},
                            ["name", "columns", "rows"],
                        ),
                    },
                    "from_files": {"type": "array", "items": _obj({"path": {"type": "string"}, "sheet": {"type": "string"}, "name": {"type": "string"}}, ["path"])},
                },
                ["path"],
            ),
            write_excel,
        )
    )

    async def make_chart(args: dict[str, Any]) -> ToolOutcome:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            return ToolOutcome("오류: 차트를 만들려면 matplotlib 이 필요합니다 (pip install matplotlib).", True)
        p = out_path(args["path"], ".png")
        note = await asyncio.to_thread(_draw_chart, p, args)
        return await file_result(f"차트 저장 완료: {p}" + (f"\n{note}" if note else ""), p)

    tools.append(
        BuiltinTool(
            "make_chart",
            "막대/가로막대/선/원 차트를 PNG 이미지로 만듭니다 (보고서·메일 첨부용).",
            _obj(
                {
                    "path": {"type": "string", "description": "files 기준 경로. 예: reports/sales.png"},
                    "chart_type": {"type": "string", "enum": ["bar", "barh", "line", "pie"]},
                    "title": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}, "description": "x축(항목) 이름"},
                    "series": {
                        "type": "array",
                        "items": _obj({"name": {"type": "string"}, "values": {"type": "array", "items": {"type": "number"}}}, ["values"]),
                    },
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                },
                ["path", "chart_type", "labels", "series"],
            ),
            make_chart,
        )
    )

    async def create_zip(args: dict[str, Any]) -> ToolOutcome:
        p = out_path(args["path"], ".zip")
        srcs = [ws.resolve(x) for x in args["paths"]]

        def run() -> int:
            n = 0
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
                for s in srcs:
                    if s.is_dir():
                        for f in s.rglob("*"):
                            if f.is_file() and f != p:
                                z.write(f, f"{s.name}/{f.relative_to(s)}")
                                n += 1
                    elif s.is_file():
                        z.write(s, s.name)
                        n += 1
            return n

        n = await asyncio.to_thread(run)
        return await file_result(f"압축 완료: {p} ({n}개 파일)", p)

    tools.append(
        BuiltinTool(
            "create_zip",
            "여러 파일/폴더를 하나의 ZIP 으로 묶습니다 (결과물을 한 번에 전달할 때).",
            _obj({"path": {"type": "string", "description": "files 기준 zip 경로"}, "paths": {"type": "array", "items": {"type": "string"}}}, ["path", "paths"]),
            create_zip,
        )
    )

    async def get_download_link(args: dict[str, Any]) -> str:
        info = signer.describe(args["path"], args.get("expire_hours"))
        note = "" if info["absolute"] else "\n(server.public_url 이 설정되지 않아 상대 링크입니다. 웹 화면에서는 클릭되지만 메일/메신저 공유에는 public_url 설정이 필요합니다.)"
        return f"다운로드 링크 ({info['expires_in_hours']:.0f}시간 유효): {info['url']}{note}"

    tools.append(
        BuiltinTool(
            "get_download_link",
            "파일(또는 폴더→zip)의 공유용 다운로드 링크를 만듭니다. 메일/메신저로 결과 파일을 보낼 때 사용하세요. "
            "웹 화면 답변의 파일 경로는 자동으로 클릭해서 받을 수 있으므로 따로 만들 필요는 없습니다.",
            _obj({"path": {"type": "string"}, "expire_hours": {"type": "number", "description": "유효 시간 (기본 168시간)"}}, ["path"]),
            get_download_link,
        )
    )

    # ======================= 파일 내용 검색 =======================
    async def search_files(args: dict[str, Any]) -> str:
        query = str(args["query"])
        rx = re.compile(query if args.get("regex") else re.escape(query), re.I)
        pattern = str(args.get("pattern") or "*")
        maxr = min(int(args.get("max_results") or 50), 300)

        def run() -> str:
            hits: list[str] = []
            scanned = 0
            for p in ws.list_files(pattern, args.get("recent_minutes"), str(args.get("where") or "all")):
                if scanned >= 300 or len(hits) >= maxr:
                    break
                try:
                    if p.stat().st_size > 30 * 1024 * 1024:
                        continue
                    text, _ = analyzers.extract_text(p) if p.suffix.lower() not in (".xlsx", ".xlsm", ".xls", ".csv", ".tsv") else (analyzers.read_text_range(p, 0, 2_000_000), None)
                except Exception:  # noqa: BLE001 - 분석 못 하는 파일은 건너뜀
                    continue
                scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{ws.display(p)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= maxr:
                            break
            return (f"{len(hits)}건 (파일 {scanned}개 검색)\n" + "\n".join(hits)) if hits else f"'{query}' 를 찾지 못했습니다 (파일 {scanned}개 검색)."

        return await asyncio.to_thread(run)

    tools.append(
        BuiltinTool(
            "search_files",
            "다운로드/업로드/저장 파일들의 '내용'에서 글자를 찾습니다 (엑셀·CSV·PDF·Word·텍스트). 어떤 파일에 특정 문서번호/이름이 있는지 찾을 때.",
            _obj(
                {
                    "query": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "pattern": {"type": "string", "description": "파일명 패턴 (기본 *)"},
                    "where": {"type": "string", "enum": ["all", "downloads", "uploads", "files"]},
                    "recent_minutes": {"type": "number"},
                    "max_results": {"type": "integer"},
                },
                ["query"],
            ),
            search_files,
        )
    )

    # ======================= 기억 / 계획 =======================
    def mem_path() -> Path:  # 호출 시점의 사용자 작업폴더 기준
        return Path(ws.root) / "memory.json"

    def mem_load() -> dict[str, Any]:
        try:
            return json.loads(mem_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def mem_save(d: dict[str, Any]) -> None:
        tmp = mem_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(mem_path())

    async def remember(args: dict[str, Any]) -> str:
        d = mem_load()
        key = str(args["key"]).strip()[:80]
        d[key] = {"content": str(args["content"])[:4000], "tags": args.get("tags") or [], "updated": datetime.now().isoformat(timespec="seconds")}
        mem_save(d)
        return f"기억함: {key} (전체 {len(d)}개)"

    async def recall(args: dict[str, Any]) -> str:
        d = mem_load()
        q = str(args.get("query") or "").lower()
        items = [(k, v) for k, v in d.items() if not q or q in k.lower() or q in v["content"].lower() or any(q in str(t).lower() for t in v.get("tags", []))]
        if not items:
            return "저장된 기억이 없습니다." if not d else f"'{q}' 관련 기억이 없습니다. 키 목록: {', '.join(list(d)[:50])}"
        return "\n\n".join(f"[{k}] ({v['updated']}) {v['content']}" for k, v in items[:30])

    async def forget(args: dict[str, Any]) -> str:
        d = mem_load()
        if d.pop(str(args["key"]), None) is None:
            return f"없는 키입니다: {args['key']}"
        mem_save(d)
        return f"삭제함: {args['key']}"

    tools += [
        BuiltinTool(
            "remember",
            "다음 실행에서도 기억할 정보를 저장합니다 (사이트별 조회 요령, 사용자 선호 형식, 자주 쓰는 코드값, 이전 작업 결과 위치 등).",
            _obj({"key": {"type": "string"}, "content": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, ["key", "content"]),
            remember,
        ),
        BuiltinTool("recall", "저장된 기억을 찾습니다 (query 생략 시 전체).", _obj({"query": {"type": "string"}}), recall),
        BuiltinTool("forget", "저장된 기억을 지웁니다.", _obj({"key": {"type": "string"}}, ["key"]), forget),
    ]

    async def update_plan(args: dict[str, Any]) -> str:
        steps = [{"step": str(s.get("step", ""))[:200], "status": s.get("status", "pending")} for s in args.get("steps") or []]
        ctx = current_run.get()
        if ctx is not None:
            ctx.meta["plan"] = steps
            emit = ctx.meta.get("emit")
            if emit is not None:
                await emit("plan", {"steps": steps, "note": args.get("note", "")})
        done = sum(1 for s in steps if s["status"] == "done")
        return f"계획 갱신: {done}/{len(steps)} 완료"

    tools.append(
        BuiltinTool(
            "update_plan",
            "여러 단계가 필요한 작업이면 시작할 때 단계 목록을 만들고, 단계가 끝날 때마다 상태를 갱신하세요 (사용자 화면에 진행 상황 표시).",
            _obj(
                {
                    "steps": {
                        "type": "array",
                        "items": _obj({"step": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"]}}, ["step", "status"]),
                    },
                    "note": {"type": "string"},
                },
                ["steps"],
            ),
            update_plan,
        )
    )

    # ======================= 이미지 / 이전 작업 =======================
    async def view_image(args: dict[str, Any]) -> str:
        from .images import ImageError, image_part, is_image

        p = ws.resolve(args["path"])
        if not p.is_file() or not is_image(p):
            return f"오류: 이미지 파일이 아닙니다: {p}"
        try:
            await asyncio.to_thread(image_part, p)  # 열 수 있는지 미리 확인
        except ImageError as e:
            return f"오류: {e}"
        ctx = current_run.get()
        if ctx is None:
            return "오류: 실행 중이 아니라 이미지를 첨부할 수 없습니다."
        ctx.meta.setdefault("pending_images", []).append(str(p))
        return f"이미지를 다음 메시지로 첨부합니다: {p.name} - 바로 이어서 내용을 확인할 수 있습니다."

    tools.append(
        BuiltinTool(
            "view_image",
            "저장된 이미지 파일(MCP 화면 캡처, 다운로드 이미지, 이전 대화 첨부)을 직접 봅니다. 호출하면 다음 단계에서 이미지가 보입니다.",
            _obj({"path": {"type": "string"}}, ["path"]),
            view_image,
        )
    )

    async def list_past_work(args: dict[str, Any]) -> str:
        from .users import SessionStore

        q = str(args.get("query") or "").lower()
        items = SessionStore(ws).list(200)
        if q:
            items = [i for i in items if q in i["title"].lower() or q in i["last_output"].lower() or any(q in f.lower() for f in i["files"])]
        if not items:
            return "이전 작업이 없습니다." if not q else f"'{q}' 관련 이전 작업이 없습니다."
        rows = []
        for i in items[: int(args.get("limit") or 20)]:
            when = datetime.fromtimestamp(i["updated"] or 0).strftime("%Y-%m-%d %H:%M")
            rows.append(f"- [{i['id']}] {when} {i['title']} (대화 {i['turns']}회)" + (f"\n  파일: {', '.join(i['files'][-5:])}" if i["files"] else ""))
        return "\n".join(rows)

    async def read_past_work(args: dict[str, Any]) -> str:
        from .users import SessionStore, UserError

        try:
            d = SessionStore(ws).load(str(args["session_id"]))
        except UserError as e:
            return f"오류: {e}"
        out = [f"# {d.get('title')} [{d.get('id')}]" + (f" (스킬: {d['skill']} {json.dumps(d.get('inputs') or {}, ensure_ascii=False)})" if d.get("skill") else "")]
        for i, t in enumerate(d.get("turns", [])[-int(args.get("last_turns") or 5):], 1):
            out.append(f"\n## 요청 {i}: {t.get('prompt')}")
            if t.get("images"):
                out.append("첨부 이미지: " + ", ".join(t["images"]))
            if t.get("tools"):
                out.append("사용 tool: " + ", ".join(t["tools"]))
            if t.get("files"):
                out.append("파일: " + ", ".join(t["files"]))
            out.append("결과:\n" + str(t.get("output", ""))[:4000])
        return "\n".join(out)

    tools += [
        BuiltinTool(
            "list_past_work",
            "이 사용자가 예전에 했던 작업(대화) 목록을 찾습니다 (제목/결과/파일명 검색).",
            _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}),
            list_past_work,
        ),
        BuiltinTool(
            "read_past_work",
            "이전 작업의 요청·결과·만든 파일을 읽습니다. '지난번 그 보고서 이어서' 같은 요청에 사용.",
            _obj({"session_id": {"type": "string"}, "last_turns": {"type": "integer"}}, ["session_id"]),
            read_past_work,
        ),
    ]

    # ======================= 알림 =======================
    ncfg = cfg.notify
    email = ncfg.get("email") or {}
    if email.get("smtp_host"):
        allowed = [str(x).lower() for x in email.get("allowed_recipients") or []]

        async def send_email(args: dict[str, Any]) -> str:
            to = [str(x).strip() for x in (args["to"] if isinstance(args["to"], list) else str(args["to"]).split(",")) if str(x).strip()]
            bad = [t for t in to if not any(fnmatch.fnmatch(t.lower(), p) for p in allowed)]
            if not allowed or bad:
                return f"오류: 허용되지 않은 수신자입니다: {', '.join(bad or to)} (notify.email.allowed_recipients 설정 확인)"
            attach = [ws.resolve(a) for a in args.get("attachments") or []]
            body = str(args.get("body", ""))
            if args.get("include_links", True) and attach:
                body += "\n\n[첨부 파일 다운로드 링크]\n" + "\n".join(f"- {a.name}: {signer.url(a)}" for a in attach if a.exists())
            ok, why = await _confirm(
                "✉️ 에이전트가 메일을 보내려고 합니다. 승인할까요?",
                f"받는 사람: {', '.join(to)}\n제목: {args['subject']}\n첨부: {', '.join(a.name for a in attach) or '-'}\n\n{body[:3000]}",
                unattended_ok=bool(email.get("allow_unattended", True)),
            )
            if not ok:
                return f"보내지 않았습니다: {why}"
            await asyncio.to_thread(_smtp_send, email, to, str(args["subject"]), body, attach)
            return f"메일 발송 완료: {', '.join(to)} / 제목: {args['subject']} / 첨부 {len(attach)}개"

        tools.append(
            BuiltinTool(
                "send_email",
                "사내 메일(SMTP)로 결과를 보냅니다. 허용된 수신자에게만 보낼 수 있고, 대화형 실행에서는 보내기 전에 사용자 승인을 받습니다.",
                _obj(
                    {
                        "to": {"type": "array", "items": {"type": "string"}},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "attachments": {"type": "array", "items": {"type": "string"}, "description": "첨부 파일 경로"},
                        "include_links": {"type": "boolean", "description": "첨부 파일 다운로드 링크를 본문에 추가 (기본 true)"},
                    },
                    ["to", "subject", "body"],
                ),
                send_email,
            )
        )

    hooks = {str(k): str(v) for k, v in (ncfg.get("webhooks") or {}).items() if v}
    if hooks:

        async def send_message(args: dict[str, Any]) -> str:
            ch = str(args["channel"])
            if ch not in hooks:
                return f"오류: 등록되지 않은 채널입니다: {ch} (사용 가능: {', '.join(hooks)})"
            text = str(args["text"])
            for f in args.get("files") or []:
                try:
                    text += f"\n- {Path(f).name}: {signer.url(f)}"
                except Exception as e:  # noqa: BLE001
                    text += f"\n- {f}: 링크 생성 실패({e})"
            ok, why = await _confirm(
                f"💬 에이전트가 '{ch}' 채널로 메시지를 보내려고 합니다. 승인할까요?", text[:3000], unattended_ok=bool(ncfg.get("allow_unattended", True))
            )
            if not ok:
                return f"보내지 않았습니다: {why}"
            r = await asyncio.to_thread(downloader.request, hooks[ch], method="POST", json_body={"text": text})
            return f"메시지 전송 {'완료' if r['status'] < 300 else '실패 HTTP ' + str(r['status'])}: {ch}"

        tools.append(
            BuiltinTool(
                "send_message",
                f"등록된 메신저 채널(Teams/Slack 웹훅)로 메시지를 보냅니다. 채널: {', '.join(hooks)}. files 를 주면 다운로드 링크가 붙습니다.",
                _obj(
                    {"channel": {"type": "string", "enum": list(hooks)}, "text": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
                    ["channel", "text"],
                ),
                send_message,
            )
        )
    return tools


# ---------------------------------------------------------------------------
# 구현 보조
# ---------------------------------------------------------------------------


def _charset(ctype: str, body: bytes) -> str:
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        cs = m.group(1).lower()
        return "cp949" if cs in ("euc-kr", "ks_c_5601-1987", "ksc5601") else cs
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:3000], re.I)
    if m:
        cs = m.group(1).decode().lower()
        return "cp949" if cs in ("euc-kr", "ks_c_5601-1987") else cs
    try:
        body[:20000].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp949"


def _html_to_text(html: str, base: str) -> tuple[str, str, list[tuple[str, str]]]:
    import html as h
    import urllib.parse

    title = h.unescape(re.sub(r"\s+", " ", (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, ""])[1])).strip()
    links = []
    for m in re.finditer(r"<a\s[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", html, re.S | re.I):
        href = m.group(1).strip()
        if href.lower().startswith(("javascript:", "mailto:")):
            continue
        txt = h.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(2))).strip()
        links.append((txt or href, urllib.parse.urljoin(base, href)))
    p = analyzers._HTMLTables()
    p.feed(re.sub(r"<(nav|header|footer)\b.*?</\1>", " ", html, flags=re.S | re.I))
    text = re.sub(r"[ \t]+", " ", "".join(p.text))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text, links


def _pick(tables: list[Any], sheet: str | None) -> Any:
    if not tables:
        raise ValueError("표 데이터를 찾지 못했습니다.")
    if sheet:
        for t in tables:
            if t.name == sheet:
                return t
        raise ValueError(f"시트 '{sheet}' 없음. 시트: {', '.join(t.name for t in tables)}")
    return tables[0]


def _save_table(p: Path, cols: list[str], rows: list[list[Any]]) -> None:
    if p.suffix.lower() == ".xlsx":
        _write_xlsx(p, [{"name": "결과", "columns": cols, "rows": rows}], None)
        return
    import csv

    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def _write_xlsx(p: Path, sheets: list[dict[str, Any]], title: str | None) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for s in sheets:
        name = re.sub(r"[\[\]:*?/\\]", "_", str(s.get("name") or "Sheet"))[:31] or "Sheet"
        base, n = name, 2
        while name in used:
            name = f"{base[:28]}_{n}"
            n += 1
        used.add(name)
        ws = wb.create_sheet(name)
        r0 = 1
        if title:
            ws.cell(1, 1, title).font = Font(bold=True, size=13)
            r0 = 3
        cols = [str(c) for c in s.get("columns") or []]
        for j, c in enumerate(cols, 1):
            cell = ws.cell(r0, j, c)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2457D6")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        widths = [max(6, min(50, len(c) * 2 + 2)) for c in cols]
        for i, row in enumerate(s.get("rows") or [], r0 + 1):
            for j, v in enumerate(row, 1):
                v = _cell_value(v)
                cell = ws.cell(i, j, v)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cell.number_format = "#,##0" if float(v).is_integer() else "#,##0.00"
                if j <= len(widths):
                    widths[j - 1] = max(widths[j - 1], min(50, len(str(v)) + 2 + sum(1 for ch in str(v) if ord(ch) > 0x3000)))
        for j, wdt in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = wdt
        if cols:
            ws.freeze_panes = ws.cell(r0 + 1, 1)
            ws.auto_filter.ref = f"A{r0}:{get_column_letter(len(cols))}{max(r0, r0 + len(s.get('rows') or []))}"
    wb.save(p)


def _draw_chart(p: Path, args: dict[str, Any]) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    note = ""
    font = _korean_font()
    text_all = " ".join([str(args.get("title", ""))] + [str(x) for x in args.get("labels") or []] + [str(s.get("name", "")) for s in args.get("series") or []])
    if font:
        plt.rcParams["font.family"] = font
    elif re.search(r"[가-힣]", text_all):
        note = "⚠️ 서버에 한글 글꼴이 없어 한글이 깨질 수 있습니다 (맑은 고딕/나눔고딕 설치 권장)."
    plt.rcParams["axes.unicode_minus"] = False
    labels = [str(x) for x in args.get("labels") or []]
    series = args.get("series") or []
    kind = args.get("chart_type", "bar")
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=120)
    if kind == "pie":
        vals = series[0]["values"]
        ax.pie(vals, labels=labels, autopct="%1.1f%%", startangle=90, counterclock=False)
        ax.axis("equal")
    else:
        import numpy as np

        x = np.arange(len(labels))
        k = max(1, len(series))
        width = 0.8 / k
        for i, s in enumerate(series):
            vals = [float(v or 0) for v in s["values"]]
            if kind == "line":
                ax.plot(labels, vals, marker="o", label=s.get("name"))
            elif kind == "barh":
                ax.barh(x + i * width - 0.4 + width / 2, vals, height=width, label=s.get("name"))
            else:
                ax.bar(x + i * width - 0.4 + width / 2, vals, width=width, label=s.get("name"))
        if kind in ("bar", "barh"):
            (ax.set_yticks if kind == "barh" else ax.set_xticks)(x, labels)
        value_axis = ax.xaxis if kind == "barh" else ax.yaxis
        value_axis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.grid(axis="x" if kind == "barh" else "y", alpha=0.3)
        ax.spines[["top", "right"]].set_visible(False)
        if k > 1 or any(s.get("name") for s in series):
            ax.legend(frameon=False)
        if args.get("x_label"):
            ax.set_xlabel(args["x_label"])
        if args.get("y_label"):
            ax.set_ylabel(args["y_label"])
        if len(labels) > 8 and kind != "barh":
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    if args.get("title"):
        ax.set_title(args["title"], fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(p)
    plt.close(fig)
    return note


def _smtp_send(cfg: dict[str, Any], to: list[str], subject: str, body: str, attach: list[Path]) -> None:
    import mimetypes
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = cfg.get("from") or cfg.get("username")
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    limit = int(cfg.get("max_attach_mb") or 20) * 1024 * 1024
    total = 0
    for a in attach:
        if not a.is_file():
            continue
        total += a.stat().st_size
        if total > limit:
            break  # 큰 파일은 본문의 다운로드 링크로 대신
        ctype = mimetypes.guess_type(a.name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(a.read_bytes(), maintype=main, subtype=sub, filename=a.name)
    port = int(cfg.get("port") or 25)
    cls = smtplib.SMTP_SSL if cfg.get("ssl") else smtplib.SMTP
    with cls(cfg["smtp_host"], port, timeout=30) as s:
        if cfg.get("starttls"):
            s.starttls()
        if cfg.get("username") and cfg.get("password"):
            s.login(cfg["username"], cfg["password"])
        s.send_message(msg)
