"""내려받은 파일 분석: 표(xlsx/xls/csv/HTML표), 문서(pdf/docx/pptx/txt), json, zip.

- LLM 에는 파일 전체가 아니라 '구조 요약 + 샘플 + 통계' 를 주고,
  필요하면 query_table / read_file_text 로 원하는 부분만 더 보게 합니다.
- openpyxl / pypdf / xlrd 가 없으면 해당 형식만 안내 메시지를 돌려줍니다.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .files import sniff_type

MAX_ROWS = 200_000
TEXT_LIMIT = 2_000_000


# ---------------------------------------------------------------------------
# 표 데이터
# ---------------------------------------------------------------------------


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[list[Any]]
    header_row: int = 0  # 원본에서 헤더가 있던 행 번호(0부터)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") and (v.hour or v.minute) else v.isoformat()[:10]
    if isinstance(v, str):
        return v.strip()
    return v


def _build_table(name: str, raw_rows: Iterable[list[Any]]) -> Table:
    rows: list[list[Any]] = []
    truncated = False
    for r in raw_rows:
        r = [_cell(c) for c in r]
        while r and r[-1] == "":
            r.pop()
        rows.append(r)
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
    # 앞쪽 제목행(보고서 제목, 출력일 등)을 건너뛰고 헤더 행 추정: 처음 15행 중 채워진 문자열 셀이 가장 많은 행
    hdr = 0
    best = -1
    for i, r in enumerate(rows[:15]):
        score = sum(1 for c in r if isinstance(c, str) and c and _to_num(c) is None)
        if score > best:
            best, hdr = score, i
    header = rows[hdr] if rows else []
    width = max((len(r) for r in rows[hdr:]), default=0)
    cols: list[str] = []
    for i in range(width):
        c = str(header[i]).strip() if i < len(header) and str(header[i]).strip() else f"col{i + 1}"
        base, n = c, 2
        while c in cols:
            c, n = f"{base}_{n}", n + 1
        cols.append(c)
    body = [r + [""] * (width - len(r)) for r in rows[hdr + 1 :] if any(str(c).strip() for c in r)]
    t = Table(name, cols, body, hdr, truncated)
    if hdr:
        t.notes.append(f"상단 {hdr}행은 제목/설명으로 보고 건너뜀 (헤더=원본 {hdr + 1}행)")
    if truncated:
        t.notes.append(f"행이 너무 많아 {MAX_ROWS}행까지만 읽음")
    return t


def _to_num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace(",", "").replace("원", "").replace("₩", "").replace("%", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s or not re.fullmatch(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class _HTMLTables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._cell: list[str] | None = None
        self._span = 1
        self.text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack:
            self._cell = []
            try:
                self._span = max(1, min(50, int(dict(attrs).get("colspan") or 1)))
            except ValueError:
                self._span = 1
        elif tag in ("br", "p", "div", "li") :
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._stack and self._cell is not None:
            if not self._stack[-1]:
                self._stack[-1].append([])
            val = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._stack[-1][-1].extend([val] + [""] * (self._span - 1))
            self._cell = None
        elif tag == "table" and self._stack:
            t = self._stack.pop()
            if any(t):
                self.tables.append([r for r in t if r])
        elif tag in ("tr", "p", "div", "h1", "h2", "h3", "h4", "li", "title"):
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        self.text.append(data)


def _read_text_file(path: Path) -> tuple[str, str]:
    data = path.read_bytes()[:TEXT_LIMIT * 2]
    for enc in ("utf-8-sig", "cp949", "utf-16"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def load_tables(path: Path, kind: str | None = None) -> list[Table]:
    kind = kind or sniff_type(path)
    ext = path.suffix.lower()
    if kind == "xlsx":
        try:
            import openpyxl
        except ImportError as e:
            raise RuntimeError("xlsx 분석에는 openpyxl 이 필요합니다: pip install openpyxl") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return [_build_table(ws.title, ws.iter_rows(values_only=True)) for ws in wb.worksheets]
        finally:
            wb.close()
    if kind == "ole" and ext in (".xls", ""):
        try:
            import xlrd
        except ImportError as e:
            raise RuntimeError("구형 xls 분석에는 xlrd 가 필요합니다: pip install xlrd") from e
        book = xlrd.open_workbook(str(path))
        out = []
        for sh in book.sheets():
            rows = []
            for i in range(sh.nrows):
                row = []
                for c in sh.row(i):
                    if c.ctype == xlrd.XL_CELL_DATE:
                        row.append(xlrd.xldate.xldate_as_datetime(c.value, book.datemode))
                    else:
                        row.append(c.value)
                rows.append(row)
            out.append(_build_table(sh.name, rows))
        return out
    if kind in ("html", "xml") or (kind == "text" and ext in (".xls", ".html", ".htm")):
        text, _ = _read_text_file(path)
        p = _HTMLTables()
        p.feed(text)
        return [_build_table(f"table{i + 1}", t) for i, t in enumerate(p.tables)]
    if kind == "text" or ext in (".csv", ".tsv"):
        text, _ = _read_text_file(path)
        sample = text[:20000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            delim = dialect.delimiter
        except csv.Error:
            delim = "\t" if ext == ".tsv" or sample.count("\t") > sample.count(",") else ","
        return [_build_table(path.stem, csv.reader(io.StringIO(text), delimiter=delim))]
    if kind == "json":
        data = json.loads(_read_text_file(path)[0])
        if isinstance(data, dict):
            # {"data": [...]} 형태
            lst = next((v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)), None)
            data = lst if lst is not None else [data]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            cols = list(dict.fromkeys(k for d in data[:1000] for k in d))
            return [_build_table(path.stem, [cols] + [[_flat(d.get(c)) for c in cols] for d in data])]
        return []
    return []


def _flat(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def summarize_table(t: Table, sample_rows: int = 10) -> str:
    lines = [f"### 시트/표: {t.name}  ({len(t.rows)}행 × {len(t.columns)}열)"]
    lines += [f"- {n}" for n in t.notes]
    if not t.columns:
        return "\n".join(lines + ["(빈 표)"])
    lines.append("")
    lines.append("| 열 | 채움 | 유형 | 통계/주요값 |")
    lines.append("|---|---|---|---|")
    for i, col in enumerate(t.columns[:60]):
        vals = [r[i] for r in t.rows if i < len(r) and str(r[i]).strip() != ""]
        nums = [n for n in (_to_num(v) for v in vals) if n is not None]
        if vals and len(nums) >= 0.8 * len(vals):
            typ = "숫자"
            stat = f"합계={_fmt(sum(nums))}, 평균={_fmt(sum(nums) / len(nums))}, 최소={_fmt(min(nums))}, 최대={_fmt(max(nums))}"
        else:
            typ = "문자"
            top = Counter(str(v) for v in vals).most_common(5)
            uniq = len(set(map(str, vals)))
            stat = f"고유값 {uniq}개; " + ", ".join(f"{_short(k)}({c})" for k, c in top)
        lines.append(f"| {col} | {len(vals)}/{len(t.rows)} | {typ} | {stat} |")
    if len(t.columns) > 60:
        lines.append(f"(열 {len(t.columns)}개 중 60개만 표시)")
    if t.rows:
        lines.append("")
        lines.append(f"샘플 {min(sample_rows, len(t.rows))}행:")
        lines.append(_md_table(t.columns, t.rows[:sample_rows]))
    return "\n".join(lines)


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 or x.is_integer() else f"{x:,.4g}"


def _short(s: str, n: int = 30) -> str:
    s = s.replace("|", "/").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _md_table(cols: list[str], rows: list[list[Any]], max_cols: int = 15) -> str:
    cols_v = cols[:max_cols]
    out = ["| " + " | ".join(_short(c, 20) for c in cols_v) + " |", "|" + "---|" * len(cols_v)]
    for r in rows:
        out.append("| " + " | ".join(_short(str(r[i]) if i < len(r) else "", 40) for i in range(len(cols_v))) + " |")
    if len(cols) > max_cols:
        out.append(f"(열 {len(cols)}개 중 {max_cols}개만 표시)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 문서 텍스트
# ---------------------------------------------------------------------------


def extract_text(path: Path, kind: str | None = None, max_pages: int = 50) -> tuple[str, dict[str, Any]]:
    kind = kind or sniff_type(path)
    meta: dict[str, Any] = {"type": kind}
    if kind == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("PDF 분석에는 pypdf 가 필요합니다: pip install pypdf") from e
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                meta["encrypted"] = True
                return "", meta
        meta["pages"] = len(reader.pages)
        parts = []
        for i, page in enumerate(reader.pages[:max_pages]):
            parts.append(f"\n--- {i + 1}페이지 ---\n" + (page.extract_text() or ""))
        text = "".join(parts)
        if len(re.sub(r"\s|---|\d+페이지", "", text)) < 20:
            meta["no_text"] = True
        if meta["pages"] > max_pages:
            meta["note"] = f"전체 {meta['pages']}페이지 중 {max_pages}페이지만 추출"
        return text, meta
    if kind in ("docx", "pptx"):
        with zipfile.ZipFile(path) as z:
            if kind == "docx":
                names = ["word/document.xml"] + [n for n in z.namelist() if re.match(r"word/(header|footer)\d*\.xml", n)]
                para, tag = r"</w:p>", r"<w:t[^>]*>(.*?)</w:t>"
            else:
                names = sorted(
                    (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml", n)),
                    key=lambda n: int(re.findall(r"\d+", n)[-1]),
                )
                meta["slides"] = len(names)
                para, tag = r"</a:p>", r"<a:t[^>]*>(.*?)</a:t>"
            out = []
            for n in names:
                if n not in z.namelist():
                    continue
                xml = z.read(n).decode("utf-8", errors="replace")
                if kind == "pptx":
                    out.append(f"\n--- 슬라이드 {re.findall(r'[0-9]+', n)[-1]} ---")
                xml = xml.replace("<w:tab/>", "\t").replace("</w:tc>", "\t")
                for p in re.split(para, xml):
                    t = "".join(re.findall(tag, p, flags=re.S))
                    if t.strip():
                        out.append(_unescape(t))
            return "\n".join(out), meta
    if kind == "html":
        text, enc = _read_text_file(path)
        p = _HTMLTables()
        p.feed(text)
        meta["encoding"] = enc
        meta["tables"] = len(p.tables)
        return re.sub(r"\n\s*\n+", "\n\n", "".join(p.text)).strip(), meta
    if kind in ("text", "json", "xml"):
        text, enc = _read_text_file(path)
        meta["encoding"] = enc
        meta["lines"] = text.count("\n") + 1
        return text, meta
    if kind == "hwp":
        raise RuntimeError("HWP 는 직접 분석할 수 없습니다. MCP/한컴 도구로 PDF 나 DOCX 로 변환 후 분석하세요.")
    raise RuntimeError(f"텍스트를 추출할 수 없는 형식입니다: {kind}")


def _unescape(s: str) -> str:
    import html

    return html.unescape(s)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def analyze(path: Path, sheet: str | None = None, sample_rows: int = 10, max_chars: int = 6000) -> str:
    kind = sniff_type(path)
    head = f"## 파일 분석: {path.name}\n- 실제 형식: {kind}\n- 크기: {path.stat().st_size:,} bytes"
    ext = path.suffix.lower()
    is_table = kind in ("xlsx", "ole") or (kind in ("text", "json") and ext in (".csv", ".tsv", ".json", ".xls")) or (
        kind == "html" and ext in (".xls", ".xlsx")
    )
    if is_table:
        tables = load_tables(path, kind)
        if sheet:
            tables = [t for t in tables if t.name == sheet] or tables
        if tables:
            body = [f"- 시트/표 {len(tables)}개: " + ", ".join(f"{t.name}({len(t.rows)}행)" for t in tables)]
            for t in tables[:5]:
                body.append("\n" + summarize_table(t, sample_rows))
            if len(tables) > 5:
                body.append(f"\n(표 {len(tables)}개 중 5개만 요약 - sheet 인자로 지정 가능)")
            body.append("\n※ 전체 데이터 조회/필터/집계는 query_table tool 을 사용하세요.")
            return head + "\n" + "\n".join(body)
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            lst = "\n".join(f"- {i.filename} ({i.file_size:,} bytes)" for i in infos[:100])
        return f"{head}\n- 압축 파일 {len(infos)}개 항목:\n{lst}\n※ extract_zip tool 로 풀어서 각 파일을 분석하세요."
    if kind in ("png", "jpg", "gif"):
        return f"{head}\n- 이미지 파일입니다. 텍스트 분석은 지원하지 않습니다(필요하면 MCP 쪽 OCR 기능 사용)."
    if kind in ("empty", "binary"):
        return f"{head}\n- 분석할 수 없는 내용입니다({kind})."
    text, meta = extract_text(path, kind)
    info = ", ".join(f"{k}={v}" for k, v in meta.items() if k != "type")
    out = [head]
    if info:
        out.append(f"- 정보: {info}")
    if meta.get("no_text"):
        out.append("- ⚠️ 추출된 텍스트가 거의 없습니다. 스캔 이미지 PDF 일 가능성이 높습니다(OCR 필요).")
    if meta.get("encrypted"):
        out.append("- ⚠️ 암호가 걸린 PDF 입니다.")
    if kind == "html" and meta.get("tables"):
        tables = load_tables(path, kind)
        out.append(f"- HTML 표 {len(tables)}개 발견")
        for t in tables[:3]:
            out.append("\n" + summarize_table(t, sample_rows))
    out.append(f"\n전체 {len(text):,}자 중 앞부분:\n```\n{text[:max_chars]}\n```")
    if len(text) > max_chars:
        out.append(f"※ 나머지는 read_file_text tool 의 start 인자로 이어서 읽으세요 (다음 start={max_chars}).")
    return "\n".join(out)


def read_text_range(path: Path, start: int = 0, max_chars: int = 8000) -> str:
    kind = sniff_type(path)
    if kind in ("xlsx", "ole"):
        blocks = []
        for t in load_tables(path, kind):
            lines = ["\t".join(t.columns)] + ["\t".join(map(str, r)) for r in t.rows]
            blocks.append(f"[{t.name}]\n" + "\n".join(lines))
        text = "\n\n".join(blocks)
    else:
        text, _ = extract_text(path, kind, max_pages=500)
    chunk = text[start : start + max_chars]
    more = start + max_chars < len(text)
    return f"[{path.name}] {start}~{start + len(chunk)} / 전체 {len(text):,}자" + (f" (다음 start={start + max_chars})" if more else " (끝)") + f"\n{chunk}"


_OPS = {"==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "in", "startswith", "empty", "not_empty"}


def query_table(
    path: Path,
    sheet: str | None = None,
    columns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    group_by: list[str] | None = None,
    aggregates: list[dict[str, str]] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> str:
    tables = load_tables(path)
    if not tables:
        return "표 형식 데이터를 찾지 못했습니다."
    t = next((x for x in tables if x.name == sheet), None) if sheet else tables[0]
    if t is None:
        return f"시트 '{sheet}' 없음. 시트 목록: {', '.join(x.name for x in tables)}"
    idx = {c: i for i, c in enumerate(t.columns)}

    def col(name: str) -> int:
        if name in idx:
            return idx[name]
        low = {c.lower().replace(" ", ""): i for c, i in idx.items()}
        key = name.lower().replace(" ", "")
        if key in low:
            return low[key]
        raise KeyError(f"열 '{name}' 없음. 열 목록: {', '.join(t.columns)}")

    rows = t.rows
    for f in filters or []:
        op = f.get("op", "==")
        if op not in _OPS:
            raise ValueError(f"지원 연산자: {', '.join(sorted(_OPS))}")
        ci, val = col(f["column"]), f.get("value")
        rows = [r for r in rows if _match(r[ci] if ci < len(r) else "", op, val)]

    if group_by or aggregates:
        gcols = [col(g) for g in group_by or []]
        aggs = aggregates or [{"func": "count"}]
        groups: dict[tuple[Any, ...], list[list[Any]]] = defaultdict(list)
        for r in rows:
            groups[tuple(r[i] for i in gcols)].append(r)
        headers = list(group_by or []) + [f"{a.get('func', 'count')}({a.get('column', '*')})" for a in aggs]
        result = []
        for key, grp in groups.items():
            vals = list(key)
            for a in aggs:
                fn = a.get("func", "count")
                if fn == "count":
                    vals.append(len(grp))
                    continue
                ci = col(a["column"])
                nums = [n for n in (_to_num(r[ci]) for r in grp if ci < len(r)) if n is not None]
                if fn == "count_distinct":
                    vals.append(len({r[ci] for r in grp if ci < len(r)}))
                elif not nums:
                    vals.append("")
                else:
                    vals.append({"sum": sum, "min": min, "max": max, "avg": lambda x: sum(x) / len(x)}[fn](nums))
            result.append(vals)
        sk = headers.index(sort_by) if sort_by in headers else (len(headers) - 1 if aggs else 0)
        result.sort(key=lambda r: _sort_key(r[sk]), reverse=descending or sort_by is None)
        shown = result[offset : offset + limit]
        return f"[{t.name}] 필터 후 {len(rows)}행 → 그룹 {len(result)}개 (표시 {len(shown)})\n" + _md_table(headers, [[_fmt(v) if isinstance(v, float) else v for v in r] for r in shown], 30)

    if sort_by:
        ci = col(sort_by)
        rows = sorted(rows, key=lambda r: _sort_key(r[ci] if ci < len(r) else ""), reverse=descending)
    sel = [col(c) for c in columns] if columns else list(range(len(t.columns)))
    shown = rows[offset : offset + limit]
    out_rows = [[r[i] if i < len(r) else "" for i in sel] for r in shown]
    more = f" (다음 offset={offset + limit})" if offset + limit < len(rows) else ""
    return f"[{t.name}] 조건에 맞는 행 {len(rows)}개 중 {offset + 1}~{offset + len(shown)}{more}\n" + _md_table([t.columns[i] for i in sel], out_rows, 30)


def _sort_key(v: Any) -> tuple[int, Any]:
    n = _to_num(v)
    return (0, n) if n is not None else (1, str(v))


def _match(cell: Any, op: str, val: Any) -> bool:
    s = str(cell)
    if op == "empty":
        return s.strip() == ""
    if op == "not_empty":
        return s.strip() != ""
    if op == "contains":
        return str(val).lower() in s.lower()
    if op == "not_contains":
        return str(val).lower() not in s.lower()
    if op == "startswith":
        return s.startswith(str(val))
    if op == "in":
        opts = val if isinstance(val, list) else [x.strip() for x in str(val).split(",")]
        return s in [str(o) for o in opts]
    a, b = _to_num(cell), _to_num(val)
    if a is not None and b is not None:
        x, y = a, b
    else:
        x, y = s, str(val)  # 날짜(YYYY-MM-DD) 등은 문자열 비교
    try:
        return {"==": x == y, "!=": x != y, ">": x > y, ">=": x >= y, "<": x < y, "<=": x <= y}[op]  # type: ignore[operator]
    except TypeError:
        return False


def extract_zip(path: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename
            # 한글 파일명(cp949로 압축된 zip) 복원
            if not info.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("cp949")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents:  # zip slip 방지
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append(target)
    return out


__all__ = ["analyze", "read_text_range", "query_table", "extract_zip", "load_tables", "extract_text"]
