"""문서 '장(章)' 단위 구조 추출: PPT 는 슬라이드별, 엑셀은 시트별, Word/PDF 는 제목/페이지별.

규칙 학습·검증의 입력이 되는 공통 형식:
{
  "path", "kind": "pptx|xlsx|docx|pdf|csv",
  "sections": [  # PPT 슬라이드 / 엑셀 시트 / 문서 단락
    {"no", "title", "layout", "texts": [..], "tables": [{"columns": [..], "rows": n, "cols": n}],
     "charts": [{"type", "title", "series": [..]}], "images": n, "notes", "words"}
  ],
  "sheets": [{"name", "columns", "rows", "numeric": [..], "empty_ratio": {col: r}}]   # 엑셀
}
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from . import analyzers
from .files import sniff_type

_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _WS.sub(" ", str(s or "")).strip()


def _words(texts: list[str]) -> int:
    return sum(len(t.split()) for t in texts)


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def _pptx_with_lib(path: Path) -> list[dict[str, Any]]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    prs = Presentation(str(path))
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        sec: dict[str, Any] = {"no": i, "title": "", "layout": slide.slide_layout.name, "texts": [], "tables": [], "charts": [], "images": 0, "notes": ""}

        def walk(shapes: Any) -> None:
            for sh in shapes:
                if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                    walk(sh.shapes)
                    continue
                if sh.is_placeholder and sh.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE) and sh.has_text_frame:
                    if not sec["title"]:
                        sec["title"] = _clean(sh.text_frame.text)
                    continue
                if getattr(sh, "has_table", False) and sh.has_table:
                    rows = [[_clean(c.text) for c in r.cells] for r in sh.table.rows]
                    sec["tables"].append({"columns": rows[0] if rows else [], "rows": max(0, len(rows) - 1), "cols": len(rows[0]) if rows else 0, "sample": rows[1:4]})
                    continue
                if getattr(sh, "has_chart", False) and sh.has_chart:
                    ch = sh.chart
                    title = ""
                    try:
                        if ch.has_title:
                            title = _clean(ch.chart_title.text_frame.text)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        series = [s.name for s in ch.plots[0].series] if ch.plots else []
                        cats = [str(c) for c in ch.plots[0].categories] if ch.plots else []
                    except Exception:  # noqa: BLE001
                        series, cats = [], []
                    sec["charts"].append({"type": str(ch.chart_type).split(".")[-1].split(" ")[0], "title": title, "series": series, "categories": cats[:12]})
                    continue
                if sh.shape_type == MSO_SHAPE_TYPE.PICTURE or (sh.is_placeholder and sh.shape_type == MSO_SHAPE_TYPE.PLACEHOLDER and hasattr(sh, "image")):
                    sec["images"] += 1
                    continue
                if sh.has_text_frame:
                    for p in sh.text_frame.paragraphs:
                        t = _clean(p.text)
                        if t:
                            sec["texts"].append(("  " * p.level + "- " if p.level else "") + t)

        walk(slide.shapes)
        if slide.has_notes_slide:
            sec["notes"] = _clean(slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else "")
        if not sec["title"] and sec["texts"]:
            sec["title"] = sec["texts"][0][:60]  # 제목 칸이 없으면 첫 줄
            sec["title_guessed"] = True
        sec["words"] = _words(sec["texts"])
        slides.append(sec)
    return slides


def _pptx_fallback(path: Path) -> list[dict[str, Any]]:
    """python-pptx 가 없을 때: XML 직접 파싱 (제목/본문/표/차트/그림 개수)."""
    import html

    slides = []
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, n in enumerate(names, 1):
            xml = z.read(n).decode("utf-8", "replace")
            sec: dict[str, Any] = {"no": i, "title": "", "layout": "", "texts": [], "tables": [], "charts": [], "images": xml.count("<p:pic>") + xml.count("<p:pic "), "notes": ""}
            for sp in re.findall(r"<p:sp>.*?</p:sp>", xml, re.S):
                paras = [html.unescape("".join(re.findall(r"<a:t>(.*?)</a:t>", p, re.S))) for p in re.split(r"</a:p>", sp)]
                paras = [_clean(p) for p in paras if _clean(p)]
                if re.search(r'<p:ph[^>]*type="(title|ctrTitle)"', sp) and not sec["title"]:
                    sec["title"] = " ".join(paras)
                else:
                    sec["texts"] += paras
            for tbl in re.findall(r"<a:tbl>.*?</a:tbl>", xml, re.S):
                rows = [[html.unescape(_clean("".join(re.findall(r"<a:t>(.*?)</a:t>", c, re.S)))) for c in re.findall(r"<a:tc>.*?</a:tc>", tr, re.S)] for tr in re.findall(r"<a:tr .*?</a:tr>", tbl, re.S)]
                if rows:
                    sec["tables"].append({"columns": rows[0], "rows": len(rows) - 1, "cols": len(rows[0]), "sample": rows[1:4]})
            sec["charts"] = [{"type": "chart", "title": "", "series": [], "categories": []} for _ in re.findall(r'uri="http://schemas.openxmlformats.org/drawingml/2006/chart"', xml)]
            if not sec["title"] and sec["texts"]:
                sec["title"] = sec["texts"][0][:60]
                sec["title_guessed"] = True
            sec["words"] = _words(sec["texts"])
            slides.append(sec)
    return slides


def pptx_structure(path: Path) -> list[dict[str, Any]]:
    try:
        import pptx  # noqa: F401
    except ImportError:
        return _pptx_fallback(path)
    try:
        return _pptx_with_lib(path)
    except Exception:  # noqa: BLE001 - 깨진/특이한 파일은 단순 파싱으로
        return _pptx_fallback(path)


# ---------------------------------------------------------------------------
# 엑셀 / 기타
# ---------------------------------------------------------------------------


def sheet_profiles(path: Path) -> list[dict[str, Any]]:
    out = []
    for t in analyzers.load_tables(path):
        n = len(t.rows)
        numeric, empty = [], {}
        for i, c in enumerate(t.columns):
            vals = [r[i] for r in t.rows if i < len(r)]
            filled = [v for v in vals if str(v).strip() != ""]
            empty[c] = round(1 - len(filled) / n, 3) if n else 1.0
            nums = [x for x in (analyzers.to_number(v) for v in filled) if x is not None]
            if filled and len(nums) >= 0.8 * len(filled):
                numeric.append(c)
        out.append({"name": t.name, "columns": t.columns, "rows": n, "numeric": numeric, "empty_ratio": empty, "sample": t.rows[:3],
                    "all_rows": t.rows[:2000], "notes": t.notes})
    return out


def doc_structure(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    kind = sniff_type(path)
    ext = path.suffix.lower()
    d: dict[str, Any] = {"path": str(path), "name": path.name, "kind": kind, "sections": [], "sheets": []}
    if kind == "pptx":
        d["sections"] = pptx_structure(path)
    elif kind in ("xlsx", "ole") or ext in (".csv", ".tsv", ".xls"):
        d["kind"] = "xlsx" if kind in ("xlsx", "ole") else "csv"
        d["sheets"] = sheet_profiles(path)
        # 엑셀도 시트를 '장' 으로 보고 같은 규칙(섹션 필수 등)을 적용할 수 있게
        d["sections"] = [
            {"no": i, "title": s["name"], "layout": "sheet", "texts": [], "tables": [{"columns": s["columns"], "rows": s["rows"], "cols": len(s["columns"]), "sample": s["sample"]}],
             "charts": [], "images": 0, "notes": "", "words": 0}
            for i, s in enumerate(d["sheets"], 1)
        ]
    elif kind == "docx":
        text, _ = analyzers.extract_text(path)
        d["sections"] = _split_headings(text)
    elif kind == "pdf":
        text, _ = analyzers.extract_text(path, max_pages=200)
        pages = re.split(r"\n--- (\d+)페이지 ---\n", text)
        secs = []
        for j in range(1, len(pages) - 1, 2):
            lines = [_clean(x) for x in pages[j + 1].splitlines() if _clean(x)]
            secs.append({"no": int(pages[j]), "title": lines[0][:60] if lines else "", "layout": "page", "texts": lines[1:], "tables": [], "charts": [], "images": 0, "notes": "", "words": _words(lines)})
        d["sections"] = secs
    else:
        raise ValueError(f"구조 분석을 지원하지 않는 형식입니다: {kind} ({path.name})")
    return d


def _split_headings(text: str) -> list[dict[str, Any]]:
    """Word: 짧은 줄(번호/제목 형태)을 장 제목으로 보고 나눔."""
    secs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = _clean(raw)
        if not line:
            continue
        is_head = len(line) <= 40 and (re.match(r"^([0-9]+[.)]|[IVX]+\.|[가-하]\.|제\s*\d+\s*[장절])", line) or (cur is None))
        if is_head:
            cur = {"no": len(secs) + 1, "title": line, "layout": "heading", "texts": [], "tables": [], "charts": [], "images": 0, "notes": "", "words": 0}
            secs.append(cur)
        elif cur is not None:
            cur["texts"].append(line)
            cur["words"] += len(line.split())
    return secs


def outline_text(d: dict[str, Any], max_text: int = 160, sections: list[int] | None = None) -> str:
    """LLM 용 요약: 장마다 제목 / 표(열) / 차트 / 이미지 / 본문 요지."""
    lines = [f"## {d['name']} ({d['kind']}, {len(d['sections'])}장)"]
    for s in d["sections"]:
        if sections and s["no"] not in sections:
            continue
        parts = [f"#{s['no']} [{s['title'] or '(제목 없음)'}]" + (" (제목 추정)" if s.get("title_guessed") else "")]
        for t in s["tables"]:
            parts.append(f"표 {t['rows']}행×{t['cols']}열(열: {', '.join(map(str, t['columns'][:10]))})")
        for c in s["charts"]:
            parts.append(f"차트 {c['type']}" + (f" '{c['title']}'" if c["title"] else "") + (f" 계열: {', '.join(map(str, c['series'][:5]))}" if c["series"] else ""))
        if s["images"]:
            parts.append(f"이미지 {s['images']}")
        line = " | ".join(parts)
        body = " / ".join(s["texts"])
        if body:
            line += f"\n    본문({s['words']}단어): {body[:max_text]}{'…' if len(body) > max_text else ''}"
        if s.get("notes"):
            line += f"\n    노트: {s['notes'][:100]}"
        lines.append(line)
    for sh in d.get("sheets") or []:
        lines.append(f"- 시트 '{sh['name']}' {sh['rows']}행, 열: {', '.join(map(str, sh['columns'][:15]))}" + (f" (숫자열: {', '.join(sh['numeric'][:8])})" if sh["numeric"] else ""))
    return "\n".join(lines)
