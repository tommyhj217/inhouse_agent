"""사내 문서 도구: 엑셀 양식 채우기 (이후 PPT 수정·레이아웃 검사·영업일 테스트도 이 파일에)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_multiuser_skills import _cfg

from inhouse_agent.config import load_config
from inhouse_agent.runtime import AgentRuntime


def make_form(p: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Border, Font, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "보고서"
    ws.merge_cells("A1:D1")
    ws["A1"] = "{{month}} 영업 실적 보고"
    ws["A1"].font = Font(bold=True, size=16)
    ws["A3"] = "작성자:"
    ws["C3"] = "작성일:"
    ws["D3"].number_format = "yyyy-mm-dd"
    ws.append([])
    ws["A5"], ws["B5"], ws["C5"], ws["D5"] = "품목", "수량", "단가", "금액"
    thin = Side(style="thin")
    for col in "ABCD":
        c = ws[f"{col}6"]
        c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
        if col != "A":
            c.number_format = "#,##0"
    ws["A10"] = "합계"
    ws["D10"] = "=SUM(D6:D9)"
    ch = BarChart()
    ch.add_data(Reference(ws, min_col=4, min_row=5, max_row=9), titles_from_data=True)
    ws.add_chart(ch, "F3")
    s2 = wb.create_sheet("요약")
    s2["A2"] = "총매출"
    s2["B3"] = "부서: ${dept}"
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return p


async def test_inspect_and_fill_excel_form(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        form = make_form(Path(rt.workspace.files) / "양식" / "월보고_양식.xlsx")
        before = form.read_bytes()
        out = await rt.hub.call("inspect_excel_template", {"path": str(form)})
        assert not out.is_error, out.text
        sh = out.data["sheets"][0]
        assert sh["placeholders"][0]["tokens"] == ["month"] and sh["formulas"] == 1
        assert {"label": "작성자:", "label_cell": "A3", "input_cell": "B3"} in sh["inputs"]
        assert sh["tables"][0]["header_row"] == 5 and sh["tables"][0]["first_empty_row"] == 6
        assert out.data["sheets"][1]["placeholders"][0]["tokens"] == ["dept"] and out.data["charts"] == 1

        res = await rt.hub.call("fill_excel", {
            "template": str(form), "output": "reports/10월_보고.xlsx",
            "replace": {"{{month}}": "2026년 10월", "${dept}": "영업1팀", "{{없는것}}": "x"},
            "cells": {"B3": "김영업", "D3": "2026-11-03", "요약!B2": "1,234,000", "A1": "제목 덮어쓰기 테스트"},
            "tables": [{"sheet": "보고서", "header": "품목", "rows": [
                {"품목": "모니터", "수량": 3, "단가": "250,000", "금액": "=B6*C6"},
                {"품목": "키보드", "수량": "10", "단가": 30000, "금액": "=B7*C7"},
                {"품목": "코드", "수량": "00123", "없는열": 1}]}],
        })
        assert not res.is_error, res.text
        assert "찾지 못한 자리표시:" in res.text and "{{없는것}}" in res.text and "머리글에 없는 열: 없는열" in res.text
        dst = Path(res.files[0]["path"])
        assert dst.name == "10월_보고.xlsx" and form.read_bytes() == before  # 원본은 그대로
        wb = load_workbook(dst)
        ws = wb["보고서"]
        assert ws["A1"].value == "제목 덮어쓰기 테스트" and ws["A1"].font.bold  # 병합 셀·서식 유지
        assert ws["B3"].value == "김영업" and ws["D3"].value.year == 2026  # 날짜 칸이면 날짜
        assert ws["C6"].value == 250000 and ws["B7"].value == 10 and ws["B8"].value == "00123"  # 코드는 문자 그대로
        assert ws["D6"].value == "=B6*C6" and ws["D10"].value == "=SUM(D6:D9)"
        assert ws["C7"].number_format == "#,##0" and ws["A7"].border.top.style == "thin"  # 첫 행 서식을 아래 행에 복사
        assert wb["요약"]["B2"].value == 1234000 and wb["요약"]["B3"].value == "부서: 영업1팀"
        assert wb.calculation.fullCalcOnLoad is True
        from inhouse_agent.excel_tools import _chart_count

        assert _chart_count(dst) == 1
        # 원본 덮어쓰기 금지, xls 는 안내
        bad = await rt.hub.call("fill_excel", {"template": str(form), "output": "양식/월보고_양식.xlsx"})
        assert bad.is_error and "덮어쓸 수 없습니다" in bad.text


async def test_fill_excel_insert_rows(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        form = make_form(Path(rt.workspace.files) / "f.xlsx")
        res = await rt.hub.call("fill_excel", {"template": str(form), "output": "g.xlsx", "tables": [
            {"start": "A6", "insert": True, "rows": [[f"품목{i}", i, 1000, None] for i in range(6)]}]})
        assert not res.is_error and "끼워 넣었습니다" in res.text
        ws = load_workbook(Path(res.files[0]["path"]))["보고서"]
        assert ws["A11"].value == "품목5" and ws["A15"].value == "합계"  # 합계 행이 아래로 밀림


def _sales(p: Path, rows: list[list]) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "매출"
    ws.append(["문서번호", "일자", "부서", "금액", "거래처코드", "비고"])
    for r in rows:
        ws.append(r)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return p


async def test_excel_data_rules_learn_validate_update(tmp_path: Path) -> None:
    from datetime import datetime

    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        past = Path(rt.workspace.files) / "과거"
        depts = ["영업1팀", "영업2팀", "기술팀"]
        n = 0
        for m in (8, 9):
            rows = []
            for i in range(12):
                n += 1
                rows.append([f"S-2026-{n:04d}", datetime(2026, m, i + 1), depts[i % 3], 10000 * (i + 1), f"{(i % 4) + 1:05d}", "" if i % 2 else "확인"])
            _sales(past / f"{m}월.xlsx", rows)
        csv = past / "10월.csv"
        lines = ["문서번호,일자,부서,금액,거래처코드,비고"] + [f"S-2026-{n + i + 1:04d},2026-10-{i + 1:02d},{depts[i % 3]},\"{10000 * (i + 1):,}\",{(i % 4) + 1:05d}," for i in range(12)]
        csv.write_bytes("\n".join(lines).encode("cp949"))  # 사내 CSV 는 cp949 가 많음

        out = await rt.hub.call("excel_rules_learn", {"name": "월매출", "paths": [str(past)], "description": "월별 매출 대장"})
        assert not out.is_error, out.text
        c = out.data["columns"]
        assert c["문서번호"]["type"] == "code" and c["문서번호"]["unique"] and c["문서번호"]["pattern"] == r"^[A-Z]\-\d{4}\-\d{4}$"
        assert c["일자"]["type"] == "date" and c["일자"]["required"]
        assert c["부서"]["allowed"] == sorted(depts) and c["부서"]["required"]
        assert c["금액"]["type"] == "number" and c["금액"]["min"] == 10000 and c["금액"]["max"] == 120000 and c["금액"]["nonneg"]
        assert c["거래처코드"]["type"] == "code" and c["거래처코드"]["pattern"] == r"^\d{5}$"  # 00001 같은 코드는 숫자로 보지 않음
        assert not c["비고"].get("required")

        bad = _sales(Path(rt.workspace.files) / "11월.xlsx", [
            ["S-2026-0100", datetime(2026, 11, 1), "영업1팀", 50000, "00001", ""],
            ["S-2026-0100", datetime(2026, 11, 2), "", -5000, "00002", ""],            # 중복 번호, 빈 부서, 음수
            ["X1", "2026/13/40", "마케팅팀", 99000000, "123", "메모"],                    # 모양·날짜·새 부서·범위·코드 모양
        ])
        out = await rt.hub.call("excel_rules_validate", {"name": "월매출", "path": str(bad)})
        r = out.data
        got = {(x["column"], x["rule"]) for x in r["issues"]}
        assert {("문서번호", "unique"), ("부서", "required"), ("금액", "nonneg"), ("일자", "type"), ("부서", "allowed"),
                ("금액", "range"), ("문서번호", "pattern"), ("거래처코드", "pattern")} <= got, got
        dup = next(x for x in r["issues"] if x["rule"] == "unique")
        assert dup["rows"] == [3] and not r["passed"]  # 엑셀 행 번호 (머리글 1행)
        assert "❌ [문서번호] 중복 값 1개 (행 3)" in out.text

        # 마케팅팀은 새로 생긴 정상 부서 → 규칙 고치기 (이유 필수), 다시 배워도 유지
        assert (await rt.hub.call("excel_rules_update", {"name": "월매출", "column": "부서", "changes": {"allowed": depts + ["마케팅팀"]}, "reason": ""})).is_error
        up = await rt.hub.call("excel_rules_update", {"name": "월매출", "column": "부서", "changes": {"allowed": depts + ["마케팅팀"]}, "reason": "11월 신설 부서"})
        assert not up.is_error and "✏️" in up.text
        await rt.hub.call("excel_rules_learn", {"name": "월매출", "paths": [str(past)], "append": True})
        r2 = (await rt.hub.call("excel_rules_validate", {"name": "월매출", "path": str(bad)})).data
        assert ("부서", "allowed") not in {(x["column"], x["rule"]) for x in r2["issues"]} and r2["version"] == 3
        lst = await rt.hub.call("excel_rules_list", {})
        assert "월매출: 월별 매출 대장 (v3" in lst.text
        good = _sales(Path(rt.workspace.files) / "정상.xlsx", [["S-2026-0200", datetime(2026, 11, 3), "기술팀", 30000, "00003", ""]])
        ok = (await rt.hub.call("excel_rules_validate", {"name": "월매출", "path": str(good)})).data
        assert ok["passed"] and ok["score"] == 100


def monthly_deck(p: Path) -> Path:
    from deck_factory import make_deck
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    make_deck(p, [
        {"title": "10월 실적 보고", "cover": True},
        {"title": "매출 요약", "bullets": ["10월 총매출 12.3억 원", "전월 대비 5% 증가"], "notes": "10월 발표 메모"},
        {"title": "부서별 현황", "table": {"columns": ["부서", "금액"], "rows": [["영업", "800"], ["기술", "430"]]}},
        {"title": "월별 추이", "chart": {"categories": ["8월", "9월", "10월"], "values": [10, 11, 12.3], "name": "매출"}},
        {"title": "부록", "bullets": ["삭제할 장"]},
    ])
    prs = Presentation(str(p))  # 서식이 다른 run 에 걸친 글: '10월'(굵게·빨강) + ' 핵심 지표'
    tb = prs.slides[1].shapes.add_textbox(Pt(40), Pt(380), Pt(400), Pt(40)).text_frame
    para = tb.paragraphs[0]
    r1, r2 = para.add_run(), para.add_run()
    r1.text, r2.text = "10", "월 핵심 지표"
    r1.font.bold, r1.font.size, r1.font.color.rgb = True, Pt(20), RGBColor(0xC0, 0, 0)
    prs.save(str(p))
    return p


async def test_edit_pptx_monthly_update(tmp_path: Path) -> None:
    from pptx import Presentation

    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        src = monthly_deck(Path(rt.workspace.files) / "10월_보고.pptx")
        before = src.read_bytes()
        res = await rt.hub.call("edit_pptx", {
            "path": str(src), "output": "11월_보고.pptx",
            "replace": {"10월": "11월", "12.3억": "13.1억", "없는글": "x"},
            "slides": [
                {"no": 2, "replace": {"5%": "7%"}, "notes": "11월 발표 메모"},
                {"no": 3, "title": "부서별 현황 (11월)", "table": {"rows": [["영업", "850"], ["기술", "460"]]}},
                {"no": 4, "chart": {"categories": ["9월", "10월", "11월"], "series": {"매출": [11, 12.3, 13.1]}}},
                {"no": 9, "title": "x"},
            ],
            "delete_slides": [5],
        })
        assert not res.is_error, res.text
        assert "찾지 못한 글: 없는글" in res.text and "9장이 없습니다" in res.text
        assert src.read_bytes() == before  # 원본 그대로
        prs = Presentation(res.files[0]["path"])
        assert len(prs.slides) == 4
        s1, s2, s3, s4 = prs.slides
        assert s1.shapes.title.text_frame.text == "11월 실적 보고"
        texts = [sh.text_frame.text for sh in s2.shapes if sh.has_text_frame]
        assert any("11월 총매출 13.1억 원" in t and "7% 증가" in t for t in texts)
        run = next(sh for sh in s2.shapes if sh.has_text_frame and sh.text_frame.text == "11월 핵심 지표").text_frame.paragraphs[0].runs
        assert len(run) == 1 and run[0].font.bold and run[0].font.color.rgb == (0xC0, 0, 0)  # run 에 걸친 글도 첫 서식 유지
        assert s2.notes_slide.notes_text_frame.text == "11월 발표 메모"
        assert s3.shapes.title.text_frame.text == "부서별 현황 (11월)"
        tbl = next(sh for sh in s3.shapes if sh.has_table).table
        assert [[tbl.cell(r, c).text for c in range(2)] for r in range(3)] == [["부서", "금액"], ["영업", "850"], ["기술", "460"]]
        chart = next(sh for sh in s4.shapes if sh.has_chart).chart
        assert list(chart.plots[0].categories) == ["9월", "10월", "11월"] and list(chart.series[0].values) == [11, 12.3, 13.1]
        assert (await rt.hub.call("edit_pptx", {"path": str(src), "output": "10월_보고.pptx"})).is_error  # 원본 덮어쓰기 금지
