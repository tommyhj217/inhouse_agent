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


def make_pptx_tools(ws: Any, links: Any = None) -> list[BuiltinTool]:
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

    return [
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
