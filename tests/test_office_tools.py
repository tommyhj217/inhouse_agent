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
