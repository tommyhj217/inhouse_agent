"""기존 PPT 수정 (지난달 보고서 → 이번 달 보고서).

edit_pptx: 원본을 복사해 바꿈 (원본은 그대로)
  replace        {"10월": "11월", "12.3억": "13.1억"}  모든 장의 글·표·노트에서 바꾸기 (글꼴·크기·색 유지)
  slides         [{"no": 3, "replace": {..}, "title": "..", "notes": "..",
                   "table": {"index": 0, "rows": [[..]], "start_row": 1, "start_col": 0},
                   "chart": {"index": 0, "categories": [..], "series": {"매출": [..]}}}]
  delete_slides  [5, 7]
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .files import file_status, format_status
from .mcp_hub import BuiltinTool, ToolOutcome


def _shapes(shapes: Any):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for sh in shapes:
        if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(sh.shapes)
        else:
            yield sh


def _text_frames(slide: Any, notes: bool = True):
    for sh in _shapes(slide.shapes):
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            yield sh.text_frame
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                for cell in row.cells:
                    yield cell.text_frame
    if notes and slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
        yield slide.notes_slide.notes_text_frame


def _replace_in_paragraph(p: Any, pairs: list[tuple[str, str]]) -> dict[str, int]:
    """문단 안에서 바꾸기. 한 run 안에 있으면 그 run 만, 여러 run 에 걸치면 첫 run 서식으로 합침."""
    hits: dict[str, int] = {}
    runs = list(p.runs)
    if not runs:
        return hits
    full = "".join(r.text for r in runs)
    if not any(old in full for old, _ in pairs):
        return hits
    new_full = full
    for old, new in pairs:
        n = new_full.count(old)
        if n:
            hits[old] = n
            new_full = new_full.replace(old, new)
    simple = True
    for old, new in pairs:
        if old in full and not any(old in r.text for r in runs):
            simple = False
    if simple:
        for r in runs:
            t = r.text
            for old, new in pairs:
                t = t.replace(old, new)
            if t != r.text:
                r.text = t
    else:
        runs[0].text = new_full
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
    return hits


def _replace(frames: Any, mapping: dict[str, Any]) -> dict[str, int]:
    pairs = sorted(((str(k), "" if v is None else str(v)) for k, v in mapping.items() if str(k)), key=lambda kv: -len(kv[0]))
    total: dict[str, int] = {}
    for tf in frames:
        for p in tf.paragraphs:
            for k, n in _replace_in_paragraph(p, pairs).items():
                total[k] = total.get(k, 0) + n
    return total


def _set_text(tf: Any, text: str) -> None:
    """첫 문단·첫 run 서식을 유지하며 글 바꾸기."""
    paras = list(tf.paragraphs)
    p0 = paras[0]
    for p in paras[1:]:
        p._p.getparent().remove(p._p)
    runs = list(p0.runs)
    if runs:
        runs[0].text = str(text)
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
    else:
        p0.text = str(text)


def edit(src: Path, dst: Path, replace: dict[str, Any] | None = None, slides: list[dict[str, Any]] | None = None,
         delete_slides: list[int] | None = None) -> dict[str, Any]:
    from pptx import Presentation

    prs = Presentation(str(src))
    rep: dict[str, Any] = {"replaced": {}, "titles": 0, "notes": 0, "cells": 0, "charts": 0, "deleted": 0, "warnings": []}
    n_slides = len(prs.slides)
    if replace:
        hits: dict[str, int] = {}
        for s in prs.slides:
            for k, n in _replace(_text_frames(s), replace).items():
                hits[k] = hits.get(k, 0) + n
        rep["replaced"] = hits
        miss = [k for k in replace if str(k) not in hits]
        if miss:
            rep["warnings"].append("PPT 에서 찾지 못한 글: " + ", ".join(map(str, miss)))
    for spec in slides or []:
        no = int(spec.get("no") or 0)
        if not 1 <= no <= n_slides:
            rep["warnings"].append(f"{no}장이 없습니다 (전체 {n_slides}장)")
            continue
        s = prs.slides[no - 1]
        if spec.get("replace"):
            hits = _replace(_text_frames(s), spec["replace"])
            for k, n in hits.items():
                rep["replaced"][f"{no}장:{k}"] = n
            miss = [k for k in spec["replace"] if str(k) not in hits]
            if miss:
                rep["warnings"].append(f"{no}장에서 찾지 못한 글: " + ", ".join(map(str, miss)))
        if spec.get("title") is not None:
            if s.shapes.title is not None:
                _set_text(s.shapes.title.text_frame, spec["title"])
                rep["titles"] += 1
            else:
                rep["warnings"].append(f"{no}장에 제목 칸이 없습니다")
        if spec.get("notes") is not None:
            _set_text(s.notes_slide.notes_text_frame, spec["notes"])
            rep["notes"] += 1
        tables = [sh for sh in _shapes(s.shapes) if getattr(sh, "has_table", False) and sh.has_table]
        t = spec.get("table")
        if t:
            idx = int(t.get("index") or 0)
            if idx >= len(tables):
                rep["warnings"].append(f"{no}장에 표 {idx + 1}번이 없습니다 (표 {len(tables)}개)")
            else:
                tbl = tables[idx].table
                r0, c0 = int(t.get("start_row", 1)), int(t.get("start_col", 0))
                for i, row in enumerate(t.get("rows") or []):
                    if r0 + i >= len(tbl.rows):
                        rep["warnings"].append(f"{no}장 표의 행이 부족해 {len(t['rows']) - i}행을 쓰지 못했습니다 (표 {len(tbl.rows)}행). 표에 행을 먼저 추가하세요.")
                        break
                    for j, v in enumerate(row):
                        if c0 + j >= len(tbl.columns) or v is None:
                            continue
                        _set_text(tbl.cell(r0 + i, c0 + j).text_frame, v)
                        rep["cells"] += 1
        ch = spec.get("chart")
        if ch:
            charts = [sh for sh in _shapes(s.shapes) if getattr(sh, "has_chart", False) and sh.has_chart]
            idx = int(ch.get("index") or 0)
            if idx >= len(charts):
                rep["warnings"].append(f"{no}장에 차트 {idx + 1}번이 없습니다 (차트 {len(charts)}개)")
            else:
                from pptx.chart.data import CategoryChartData

                chart = charts[idx].chart
                cd = CategoryChartData()
                cats = ch.get("categories") or [str(c) for c in chart.plots[0].categories]
                cd.categories = cats
                for name, vals in (ch.get("series") or {}).items():
                    if len(vals) != len(cats):
                        raise ValueError(f"{no}장 차트 계열 '{name}' 값 {len(vals)}개가 항목 {len(cats)}개와 다릅니다.")
                    cd.add_series(str(name), [float(v) if v is not None else None for v in vals])
                chart.replace_data(cd)
                rep["charts"] += 1
    for no in sorted({int(n) for n in delete_slides or []}, reverse=True):
        if not 1 <= no <= n_slides:
            rep["warnings"].append(f"삭제할 {no}장이 없습니다")
            continue
        ids = prs.slides._sldIdLst
        sld = list(ids)[no - 1]
        prs.part.drop_rel(sld.rId)
        ids.remove(sld)
        rep["deleted"] += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(dst))
    rep["slides"] = len(prs.slides)
    return rep


def make_pptx_tools(ws: Any, links: Any = None, soffice_path: str | None = None) -> list[BuiltinTool]:
    async def done(text: str, p: Path) -> ToolOutcome:
        st = await file_status(p, ws, stable_wait=0)
        if links is not None:
            try:
                text += f"\n다운로드 링크: {links.url(p)}"
            except Exception:  # noqa: BLE001
                pass
        return ToolOutcome(text + "\n" + format_status(st), False, [st])

    def out_path(src: Path, rel: str | None, suffix: str) -> Path:
        rel = str(rel or f"{src.stem}{suffix}{src.suffix}").lstrip("/\\")
        if not Path(rel).suffix:
            rel += src.suffix
        return ws.resolve(rel if rel.startswith(("files/", "downloads/", "uploads/")) else f"files/{rel}", write=True)

    async def edit_pptx(args: dict[str, Any]) -> ToolOutcome:
        src = ws.resolve(str(args["path"]))
        if src.suffix.lower() != ".pptx":
            return ToolOutcome("오류: pptx 파일만 수정할 수 있습니다 (ppt 는 PowerPoint 에서 pptx 로 저장).", True)
        dst = out_path(src, args.get("output"), "_수정")
        if dst.resolve() == src.resolve():
            return ToolOutcome("오류: 원본에 덮어쓸 수 없습니다. output 에 다른 이름을 주세요.", True)
        try:
            rep = await asyncio.to_thread(edit, src, dst, args.get("replace"), args.get("slides"), args.get("delete_slides"))
        except (ValueError, KeyError, OSError) as e:
            return ToolOutcome(f"오류: {e}", True)
        hits = rep["replaced"]
        text = (f"PPT 수정 완료: {dst} ({rep['slides']}장)\n"
                f"- 글 바꾸기 {sum(hits.values())}곳" + (f" ({', '.join(f'{k} {v}곳' for k, v in list(hits.items())[:12])})" if hits else "")
                + f" · 제목 {rep['titles']} · 노트 {rep['notes']} · 표 칸 {rep['cells']} · 차트 {rep['charts']} · 삭제 {rep['deleted']}장"
                + "".join(f"\n⚠ {w}" for w in rep["warnings"])
                + "\n바뀐 내용은 doc_structure 로 확인하고, 글이 칸을 넘치는지는 check_pptx_layout 으로 검사하세요.")
        return await done(text, dst)

    async def check_pptx_layout(args: dict[str, Any]) -> ToolOutcome:
        p = ws.resolve(str(args["path"]))
        if p.suffix.lower() != ".pptx":
            return ToolOutcome("오류: pptx 파일만 검사할 수 있습니다.", True)
        try:
            r = await asyncio.to_thread(check_layout, p, [int(x) for x in args.get("slides") or []] or None)
        except (ValueError, OSError, KeyError) as e:
            return ToolOutcome(f"오류: {e}", True)
        return ToolOutcome(format_layout(r), False, [], r)

    async def render_preview(args: dict[str, Any]) -> ToolOutcome:
        p = ws.resolve(str(args["path"]))
        soffice = find_soffice(soffice_path)
        if soffice is None:
            return ToolOutcome("오류: LibreOffice 가 설치되어 있지 않아 미리보기 이미지를 만들 수 없습니다. "
                               "서버 PC 에 LibreOffice 를 설치하거나 config 의 office.soffice 에 경로를 적으세요. "
                               "설치 없이 할 수 있는 검사는 check_pptx_layout 입니다.", True)
        out_dir = ws.resolve(f"files/_preview/{p.stem}", write=True)
        try:
            r = await render(p, out_dir, soffice, [int(x) for x in args.get("pages") or []] or None, int(args.get("dpi") or 80))
        except ValueError as e:
            return ToolOutcome(f"오류: {e}", True)
        sts = [await file_status(x, ws, stable_wait=0) for x in [r["pdf"], *r["images"]]]
        text = (f"미리보기: {p.name} → PDF {r['pages']}쪽, 이미지 {len(r['images'])}장\n- PDF: {r['pdf']}\n"
                + "\n".join(f"- {x}" for x in r["images"])
                + ("\n장 이미지를 view_image 로 열어 글 넘침·겹침·디자인을 직접 확인하세요." if r["images"] else
                   "\n(이미지 변환 도구 pdftoppm/PyMuPDF 가 없어 PDF 만 만들었습니다)"))
        return ToolOutcome(text, False, sts, {"pdf": str(r["pdf"]), "images": [str(x) for x in r["images"]]})

    return [
        BuiltinTool(
            "check_pptx_layout",
            "PPT 레이아웃을 검사합니다 (설치 필요 없음): 글이 칸을 넘칠 가능성, 슬라이드 밖으로 나간 개체, 겹친 글상자, 10pt 미만 글씨, "
            "빈 입력칸, 글이 너무 많은 장. 보고서를 만들거나 고친 뒤 확인용.",
            {"type": "object", "properties": {"path": {"type": "string"}, "slides": {"type": "array", "items": {"type": "integer"}}}, "required": ["path"]},
            check_pptx_layout,
        ),
        BuiltinTool(
            "render_preview",
            "PPT·엑셀·Word 를 장별 PNG 이미지(+PDF)로 만듭니다 (서버에 LibreOffice 필요). 만든 이미지를 view_image 로 보면 "
            "디자인·글 넘침·겹침을 눈으로 확인할 수 있습니다.",
            {"type": "object", "properties": {"path": {"type": "string"}, "pages": {"type": "array", "items": {"type": "integer"}},
                                              "dpi": {"type": "integer", "description": "해상도 (기본 80)"}}, "required": ["path"]},
            render_preview,
        ),
        BuiltinTool(
            "edit_pptx",
            "기존 PPT 를 복사해 고칩니다 (원본은 그대로, 글꼴·크기·색 유지). 지난달 보고서로 이번 달 보고서를 만들 때 사용. "
            "replace={'10월': '11월'} (모든 장), slides=[{no, replace, title, notes, table: {index, rows, start_row, start_col}, "
            "chart: {index, categories, series: {'계열': [값..]}}}], delete_slides=[번호]. 장 번호는 1부터 (doc_structure 로 확인).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "원본 pptx"},
                    "output": {"type": "string", "description": "저장할 경로 (files 기준, 기본: <원본>_수정.pptx)"},
                    "replace": {"type": "object", "description": "{찾을 글: 바꿀 글} - 모든 장 (표·노트 포함)"},
                    "slides": {"type": "array", "items": {"type": "object"}, "description": "장별 수정"},
                    "delete_slides": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["path"],
            },
            edit_pptx,
        ),
    ]


# ---------------------------------------------------------------------------
# 레이아웃 검사 (설치 없이) / 미리보기 이미지 (LibreOffice)
# ---------------------------------------------------------------------------

EMU_PER_PT = 12700
DEFAULT_PT = 18.0


def _char_em(ch: str) -> float:
    o = ord(ch)
    if ch == " ":
        return 0.3
    if o >= 0x1100 and (0x1100 <= o <= 0x11FF or 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF):
        return 1.0  # 한글·한자·전각
    if ch.isupper() or ch in "mwMW@%":
        return 0.68
    return 0.55


def _para_size(p: Any, fallback: float) -> float:
    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
    if p.font.size is not None:
        sizes.append(p.font.size.pt)
    return max(sizes) if sizes else fallback


def _placeholder_default(sh: Any) -> float:
    try:
        from pptx.enum.shapes import PP_PLACEHOLDER

        t = sh.placeholder_format.type
        if t in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
            return 40.0
        if t == PP_PLACEHOLDER.SUBTITLE:
            return 24.0
        return 24.0
    except Exception:  # noqa: BLE001
        return DEFAULT_PT


def _needed(tf: Any, width_emu: int, fallback_pt: float) -> tuple[float, float, list[float]]:
    """(필요한 높이 pt, 가장 긴 줄 너비 pt, 글자 크기들) - 줄바꿈을 어림해서 계산."""
    avail = max(1.0, (width_emu - (tf.margin_left or 91440) - (tf.margin_right or 91440)) / EMU_PER_PT)
    height, widest, sizes = 0.0, 0.0, []
    for p in tf.paragraphs:
        size = _para_size(p, fallback_pt)
        sizes.append(size)
        text = "".join(r.text for r in p.runs) or p.text
        w = sum(_char_em(c) for c in text) * size
        widest = max(widest, w)
        lines = 1 if not text else (max(1, -(-int(w) // int(avail))) if tf.word_wrap is not False else 1)
        height += lines * size * 1.2
    return height, widest, sizes


def check_layout(path: Path, only: list[int] | None = None) -> dict[str, Any]:
    from pptx import Presentation
    from pptx.enum.text import MSO_AUTO_SIZE

    prs = Presentation(str(path))
    sw, sh_ = prs.slide_width, prs.slide_height
    report: list[dict[str, Any]] = []
    for no, slide in enumerate(prs.slides, 1):
        if only and no not in only:
            continue
        issues: list[dict[str, Any]] = []
        boxes: list[tuple[str, int, int, int, int]] = []
        words = 0
        for sh in _shapes(slide.shapes):
            name = sh.name
            if sh.width is None or sh.height is None:
                continue
            l, t, w, h = sh.left or 0, sh.top or 0, sh.width, sh.height
            tol = int(0.02 * sw)
            if l < -tol or t < -tol or l + w > sw + tol or t + h > sh_ + tol:
                issues.append({"kind": "off_slide", "shape": name, "message": f"'{name}' 이(가) 슬라이드 밖으로 나감"})
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                tf = sh.text_frame
                text = tf.text.strip()
                if sh.is_placeholder and not text:
                    issues.append({"kind": "empty_placeholder", "shape": name, "message": f"빈 입력칸 '{name}' (내용을 넣거나 지우세요)"})
                    continue
                if not text:
                    continue
                words += len(text.split())
                boxes.append((name, l, t, w, h))
                fallback = _placeholder_default(sh) if sh.is_placeholder else DEFAULT_PT
                need_h, widest, sizes = _needed(tf, w, fallback)
                small = [s for s in sizes if s < 10]
                if small:
                    issues.append({"kind": "small_font", "shape": name, "message": f"'{name}' 글씨가 {min(small):.0f}pt (10pt 미만은 읽기 어려움)"})
                avail_h = (h - (tf.margin_top or 45720) - (tf.margin_bottom or 45720)) / EMU_PER_PT
                shrink = tf.auto_size == MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE or tf._txBody.find(
                    "{http://schemas.openxmlformats.org/drawingml/2006/main}bodyPr/{http://schemas.openxmlformats.org/drawingml/2006/main}normAutofit") is not None
                grows = tf.auto_size == MSO_AUTO_SIZE.SHAPE_TO_FIT_TEXT
                if grows:  # 글에 맞춰 상자가 늘어나는 설정 → 늘어난 상자가 슬라이드 아래로 나가는지 봄
                    grown = need_h * EMU_PER_PT + (tf.margin_top or 45720) + (tf.margin_bottom or 45720)
                    if grown > h:
                        boxes[-1] = (name, l, t, w, int(grown))
                    if t + grown > sh_ * 1.02:
                        issues.append({"kind": "overflow", "shape": name, "ratio": round(need_h / max(avail_h, 1), 2),
                                       "message": f"'{name}' 글이 많아 상자가 늘어나면 슬라이드 아래로 넘칠 가능성"})
                elif need_h > avail_h * 1.15:
                    ratio = need_h / max(avail_h, 1)
                    if not shrink or ratio > 1.6:
                        issues.append({"kind": "overflow", "shape": name, "ratio": round(ratio, 2),
                                       "message": f"'{name}' 글이 칸을 넘칠 가능성 (필요 높이 약 {ratio:.1f}배"
                                                  + (", 자동 줄임으로도 부족" if shrink else "") + ")"})
                if tf.word_wrap is False and widest > (w / EMU_PER_PT) * 1.05:
                    issues.append({"kind": "overflow", "shape": name, "message": f"'{name}' 줄바꿈이 꺼져 있어 글이 옆으로 넘침"})
            if getattr(sh, "has_table", False) and sh.has_table:
                for r_i, row in enumerate(sh.table.rows):
                    for c_i, cell in enumerate(row.cells):
                        txt = cell.text_frame.text.strip()
                        if not txt:
                            continue
                        cw = sh.table.columns[c_i].width
                        need_h, _, _ = _needed(cell.text_frame, cw, 14.0)
                        if need_h > (row.height / EMU_PER_PT) * 2.2:
                            issues.append({"kind": "table_overflow", "shape": name,
                                           "message": f"'{name}' 표 {r_i + 1}행 {c_i + 1}열 글이 길어 표가 슬라이드 밖으로 늘어날 수 있음"})
                            break
        for i in range(len(boxes)):  # 글상자 겹침
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                ix = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
                iy = max(0, min(a[2] + a[4], b[2] + b[4]) - max(a[2], b[2]))
                small_area = min(a[3] * a[4], b[3] * b[4]) or 1
                if ix * iy > 0.2 * small_area:
                    issues.append({"kind": "overlap", "shape": f"{a[0]} / {b[0]}", "message": f"'{a[0]}' 와 '{b[0]}' 가 겹침"})
        if words > 160:
            issues.append({"kind": "too_much_text", "shape": "", "message": f"글이 많음 ({words}단어) - 장을 나누거나 줄이세요"})
        title = slide.shapes.title.text_frame.text if slide.shapes.title is not None else ""
        report.append({"no": no, "title": title, "issues": issues})
    total = sum(len(s["issues"]) for s in report)
    return {"path": str(path), "slides": report, "issues": total, "passed": total == 0}


def format_layout(r: dict[str, Any]) -> str:
    lines = [f"## 레이아웃 검사: {Path(r['path']).name} - " + ("문제 없음" if r["passed"] else f"{r['issues']}건")]
    for s in r["slides"]:
        if s["issues"]:
            lines.append(f"- #{s['no']} {s['title'][:30]}")
            lines += [f"    · {x['message']}" for x in s["issues"]]
    lines.append("※ 글 넘침은 글꼴 크기와 칸 크기로 어림한 결과입니다. 정확히 보려면 render_preview 로 장 이미지를 만들어 view_image 로 확인하세요.")
    return "\n".join(lines)


def find_soffice(cfg_path: str | None = None) -> str | None:
    import os
    import shutil

    cands = [cfg_path] if cfg_path else []
    cands += [shutil.which("soffice"), shutil.which("libreoffice")]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        cands.append(str(Path(base) / "LibreOffice" / "program" / "soffice.exe"))
    cands.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return next((c for c in cands if c and Path(c).is_file()), None)


async def render(src: Path, out_dir: Path, soffice: str, pages: list[int] | None = None, dpi: int = 80, timeout: float = 180) -> dict[str, Any]:
    """LibreOffice 로 PDF 를 만든 뒤 장별 PNG 로 (pdftoppm, 없으면 PyMuPDF). 이미지 도구가 없으면 PDF 만."""
    import shutil
    import tempfile
    import uuid

    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.gettempdir()) / f"agent_lo_{uuid.uuid4().hex[:8]}"  # 동시에 여러 개 변환해도 충돌 없게
    try:
        proc = await asyncio.create_subprocess_exec(
            soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ValueError(f"LibreOffice 변환이 {timeout:.0f}초 안에 끝나지 않았습니다.")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    pdf = out_dir / (src.stem + ".pdf")
    if not pdf.is_file():
        raise ValueError(f"PDF 로 바꾸지 못했습니다: {err.decode('utf-8', 'replace')[-300:]}")
    from pypdf import PdfReader

    n = len(PdfReader(str(pdf)).pages)
    want = [p for p in (pages or range(1, n + 1)) if 1 <= p <= n][:30]
    images: list[Path] = []
    tool = shutil.which("pdftoppm")
    if tool:
        for p in want:
            prefix = out_dir / f"{src.stem}_{p:02d}"
            proc = await asyncio.create_subprocess_exec(tool, "-png", "-r", str(dpi), "-f", str(p), "-l", str(p), "-singlefile", str(pdf), str(prefix),
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), 60)
            if prefix.with_suffix(".png").is_file():
                images.append(prefix.with_suffix(".png"))
    else:
        try:
            import fitz  # PyMuPDF

            doc = fitz.open(str(pdf))
            for p in want:
                img = out_dir / f"{src.stem}_{p:02d}.png"
                doc[p - 1].get_pixmap(dpi=dpi).save(str(img))
                images.append(img)
        except ImportError:
            pass
    return {"pdf": pdf, "pages": n, "images": images}
