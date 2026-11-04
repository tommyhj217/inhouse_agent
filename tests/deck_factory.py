"""테스트용 '과거 보고서' PPT/엑셀 생성기."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def make_deck(path: Path, slides: list[dict[str, Any]]) -> Path:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    for s in slides:
        layout = prs.slide_layouts[0 if s.get("cover") else (5 if (s.get("table") or s.get("chart")) else 1)]
        sl = prs.slides.add_slide(layout)
        sl.shapes.title.text = s["title"]
        if s.get("bullets") and layout == prs.slide_layouts[1]:
            tf = sl.placeholders[1].text_frame
            tf.text = s["bullets"][0]
            for b in s["bullets"][1:]:
                tf.add_paragraph().text = b
        elif s.get("bullets"):
            tb = sl.shapes.add_textbox(Inches(0.5), Inches(1.3), Inches(9), Inches(1))
            tb.text_frame.text = " ".join(s["bullets"])
        if s.get("table"):
            cols, rows = s["table"]["columns"], s["table"]["rows"]
            t = sl.shapes.add_table(len(rows) + 1, len(cols), Inches(0.5), Inches(2.3), Inches(9), Inches(3)).table
            for j, c in enumerate(cols):
                t.cell(0, j).text = c
            for i, r in enumerate(rows, 1):
                for j, v in enumerate(r):
                    t.cell(i, j).text = str(v)
        if s.get("chart"):
            cd = CategoryChartData()
            cd.categories = s["chart"]["categories"]
            cd.add_series(s["chart"].get("name", "값"), s["chart"]["values"])
            sl.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2.3), Inches(8), Inches(4), cd)
        if s.get("notes"):
            sl.notes_slide.notes_text_frame.text = s["notes"]
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(path)
    return path


def past_report(product: str, *, competitor_table: bool = True, swot: bool = True, source: bool = True, numbering: bool = True) -> list[dict[str, Any]]:
    n = (lambda i: f"{i}. ") if numbering else (lambda i: "")
    slides = [
        {"title": f"{product} 기술 분석 보고서", "cover": True},
        {"title": "목차", "bullets": ["개요", "시장 현황", "경쟁사 비교", "SWOT", "시사점"]},
        {"title": f"{n(1)}개요", "bullets": [f"{product} 는 생성형 AI 기반 서비스", "핵심 기능 요약", "출시 일정"]},
        {"title": f"{n(2)}시장 현황", "bullets": ["시장 규모 성장률 연 30%", "주요 수요처"], "chart": {"categories": ["2024", "2025", "2026"], "values": [10, 13, 17]}},
    ]
    if competitor_table:
        slides.append({"title": f"{n(3)}경쟁사 비교", "table": {"columns": ["기업", "제품", "핵심 기능", "가격"], "rows": [["A사", "A1", "요약", "무료"], ["B사", "B1", "번역", "유료"]]}})
    if swot:
        slides.append({"title": f"{n(4)}SWOT 분석", "table": {"columns": ["강점", "약점", "기회", "위협"], "rows": [["정확도", "비용", "수요", "규제"]]}})
    slides.append({"title": f"{n(5)}시사점 및 제언", "bullets": ["도입 권고 수준: 시범 적용", "리스크 관리 필요"] + (["출처: 사내 조사, 2026"] if source else [])})
    return slides


def eval_sheet(path: Path, product: str) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "평가표"
    ws.append(["평가항목", "가중치", "점수", "근거"])
    for row in [["기술성", 30, 4, "벤치마크"], ["시장성", 30, 3, "시장 규모"], ["사업성", 40, 4, "원가"]]:
        ws.append(row)
    ws.append(["비고", None, None, product])  # 파일마다 내용이 달라야 같은 파일로 취급되지 않음
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
