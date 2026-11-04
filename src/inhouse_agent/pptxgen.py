"""PPT 보고서 생성 (python-pptx). 과거 보고서를 template 으로 주면 그 디자인(마스터/레이아웃)을 그대로 씁니다."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _layout(prs: Any, kind: str) -> Any:
    """kind: cover | content | title_only — 템플릿의 레이아웃 중 알맞은 것."""
    from pptx.enum.shapes import PP_PLACEHOLDER

    def types(lo: Any) -> set:
        return {ph.placeholder_format.type for ph in lo.placeholders}

    los = list(prs.slide_layouts)
    if kind == "cover":
        for lo in los:
            if PP_PLACEHOLDER.CENTER_TITLE in types(lo):
                return lo
    if kind == "content":
        for lo in los:
            t = types(lo)
            if (PP_PLACEHOLDER.TITLE in t) and (PP_PLACEHOLDER.BODY in t or PP_PLACEHOLDER.OBJECT in t):
                return lo
    for lo in los:
        t = types(lo)
        if PP_PLACEHOLDER.TITLE in t and len(t - {PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER}) == 1:
            return lo
    return los[min(5, len(los) - 1)]


def write_pptx(path: Path, slides: list[dict[str, Any]], template: Path | None = None) -> dict[str, Any]:
    try:
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
        from pptx.enum.shapes import PP_PLACEHOLDER
        from pptx.util import Emu, Pt
    except ImportError as e:
        raise RuntimeError("PPT 생성에는 python-pptx 가 필요합니다: pip install python-pptx") from e

    prs = Presentation(str(template)) if template else Presentation()
    if template:  # 템플릿의 기존 슬라이드는 지우고 디자인만 사용
        sld = prs.slides._sldIdLst  # noqa: SLF001
        for sid in list(sld):
            prs.part.drop_rel(sid.rId)
            sld.remove(sid)
    W, H = prs.slide_width, prs.slide_height
    left, top, width, height = Emu(int(W * 0.06)), Emu(int(H * 0.22)), Emu(int(W * 0.88)), Emu(int(H * 0.68))
    counts = {"slides": 0, "tables": 0, "charts": 0, "images": 0}
    for i, s in enumerate(slides):
        visual = s.get("table") or s.get("chart") or s.get("image")
        kind = "cover" if (s.get("cover") or (i == 0 and not s.get("bullets") and not visual)) else ("title_only" if visual else "content")
        sl = prs.slides.add_slide(_layout(prs, kind))
        counts["slides"] += 1
        if sl.shapes.title is not None:
            sl.shapes.title.text = str(s.get("title") or "")
        body = next((ph for ph in sl.placeholders if ph.placeholder_format.type in (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE)), None)
        bullets = s.get("bullets") or ([s["subtitle"]] if s.get("subtitle") else [])
        if bullets:
            if body is not None and not visual:
                tf = body.text_frame
                tf.clear()
            else:
                box_h = Emu(int(H * 0.12)) if visual else height
                tf = sl.shapes.add_textbox(left, top, width, box_h).text_frame
                tf.word_wrap = True
            for j, b in enumerate(bullets):
                text, level = (b.get("text", ""), int(b.get("level", 0))) if isinstance(b, dict) else (str(b), 0)
                p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                p.text = text
                p.level = min(level, 4)
                if visual:
                    for r in p.runs:
                        r.font.size = Pt(14)
            if visual:
                top_v = Emu(int(top + H * 0.13))
            else:
                top_v = top
        else:
            top_v = top
            if body is not None and visual:
                body._element.getparent().remove(body._element)  # noqa: SLF001 - 빈 본문 칸 제거
        vis_h = Emu(int(H * 0.9) - top_v)
        if s.get("table"):
            cols = [str(c) for c in s["table"].get("columns") or []]
            rows = s["table"].get("rows") or []
            if cols:
                gt = sl.shapes.add_table(len(rows) + 1, len(cols), left, top_v, width, Emu(min(vis_h, Emu(int(H * 0.07) * (len(rows) + 1))))).table
                for j, c in enumerate(cols):
                    gt.cell(0, j).text = c
                for r_i, r in enumerate(rows, 1):
                    for j in range(len(cols)):
                        gt.cell(r_i, j).text = "" if j >= len(r) or r[j] is None else str(r[j])
                for row in gt.rows:
                    for cell in row.cells:
                        for p in cell.text_frame.paragraphs:
                            for run in p.runs:
                                run.font.size = Pt(12)
                counts["tables"] += 1
        elif s.get("chart"):
            ch = s["chart"]
            cd = CategoryChartData()
            cd.categories = [str(c) for c in ch.get("categories") or []]
            for se in ch.get("series") or []:
                cd.add_series(str(se.get("name") or ""), [float(v or 0) for v in se.get("values") or []])
            ctype = {"bar": XL_CHART_TYPE.COLUMN_CLUSTERED, "barh": XL_CHART_TYPE.BAR_CLUSTERED, "line": XL_CHART_TYPE.LINE_MARKERS,
                     "pie": XL_CHART_TYPE.PIE}.get(str(ch.get("type") or "bar"), XL_CHART_TYPE.COLUMN_CLUSTERED)
            gf = sl.shapes.add_chart(ctype, left, top_v, width, vis_h, cd)
            if len(ch.get("series") or []) > 1 or ctype == XL_CHART_TYPE.PIE:
                gf.chart.has_legend = True
                gf.chart.legend.position = XL_LEGEND_POSITION.BOTTOM
                gf.chart.legend.include_in_layout = False
            if ch.get("title"):
                gf.chart.has_title = True
                gf.chart.chart_title.text_frame.text = str(ch["title"])
            counts["charts"] += 1
        elif s.get("image"):
            sl.shapes.add_picture(str(s["image"]), left, top_v, height=vis_h)
            counts["images"] += 1
        if s.get("notes"):
            sl.notes_slide.notes_text_frame.text = str(s["notes"])
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return counts
