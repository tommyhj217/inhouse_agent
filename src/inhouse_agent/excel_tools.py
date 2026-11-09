"""기존 엑셀 양식 채우기 (서식·수식·병합 유지).

- inspect_excel_template: 양식 구조 파악 - 시트, {{자리표시}}, 라벨 옆 입력칸, 표 머리글과 첫 빈 행, 병합 셀, 수식, 차트 수
- fill_excel: 양식을 복사해 값 채우기
    cells    {"B3": 값, "요약!C5": 값, "이름정의": 값}   셀 주소 / 시트!셀 / 이름 정의
    replace  {"{{month}}": "2026-10"}                  셀 안 자리표시 바꾸기 (셀 전체가 자리표시면 숫자는 숫자로)
    tables   [{"sheet", "start": "A6" | "header": "품목", "rows": [[..]] | [{"품목":..}], "insert": false}]
             표에 행 쓰기. rows 가 dict 면 머리글 이름으로 열을 맞춤. 첫 데이터 행의 서식을 아래 행에 복사.
  저장할 때 엑셀이 수식을 다시 계산하도록 표시합니다 (파일을 열면 합계 등이 새 값으로 갱신).
"""

from __future__ import annotations

import asyncio
import copy
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .files import file_status, format_status
from .mcp_hub import BuiltinTool, ToolOutcome

_TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}|\$\{\s*([^{}]+?)\s*\}")
_CELL = re.compile(r"^(?:(?:'([^']+)'|([^!]+))!)?\$?([A-Za-z]{1,3})\$?(\d+)$")
_NUM = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _chart_count(p: Path) -> int:
    try:
        with zipfile.ZipFile(p) as z:
            return sum(1 for n in z.namelist() if re.match(r"xl/charts/chart\d+\.xml$", n))
    except (zipfile.BadZipFile, OSError):
        return 0


def _load(p: Path):
    from openpyxl import load_workbook

    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError(f"xlsx/xlsm 양식만 채울 수 있습니다 (지금: {p.suffix}). 엑셀에서 xlsx 로 저장한 뒤 쓰세요.")
    return load_workbook(p, keep_vba=p.suffix.lower() == ".xlsm")


def _top_left(ws: Any, coord: str) -> Any:
    """병합된 셀이면 왼쪽 위 셀 (병합 영역 가운데 셀에는 쓸 수 없음)."""
    for rng in ws.merged_cells.ranges:
        if coord in rng:
            return ws.cell(rng.min_row, rng.min_col)
    return ws[coord]


def _coerce(value: Any, cell: Any) -> Any:
    if not isinstance(value, str):
        return value
    v = value.strip()
    if v.startswith("="):
        return v  # 수식
    m = _DATE.match(v)
    if m and (cell.is_date or any(ch in str(cell.number_format).lower() for ch in "yd")):
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if _NUM.match(v) and not (v.lstrip("-").startswith("0") and len(v.lstrip("-").split(".")[0]) > 1):
        n = float(v.replace(",", ""))  # 00123 같은 코드는 문자 그대로
        return int(n) if n.is_integer() and "." not in v else n
    return value


def _resolve(wb: Any, ref: str, default_sheet: Any) -> Any:
    ref = str(ref).strip()
    m = _CELL.match(ref)
    if m:
        sheet = m.group(1) or m.group(2)
        ws = wb[sheet] if sheet else default_sheet
        if sheet and sheet not in wb.sheetnames:
            raise KeyError(f"시트가 없습니다: {sheet}")
        return _top_left(ws, f"{m.group(3).upper()}{m.group(4)}")
    dn = wb.defined_names.get(ref) if hasattr(wb.defined_names, "get") else None
    if dn is not None:
        for title, coord in dn.destinations:
            first = coord.replace("$", "").split(":")[0]
            return _top_left(wb[title], first)
    raise KeyError(f"셀 주소나 이름 정의를 찾을 수 없습니다: {ref}")


def _header_row(ws: Any, header: str | None, start: str | None) -> tuple[int, int, dict[str, int]]:
    """(머리글 행, 첫 데이터 행, {머리글: 열번호})."""
    if start:
        m = _CELL.match(start)
        if not m:
            raise ValueError(f"start 는 셀 주소여야 합니다 (예: A6): {start}")
        from openpyxl.utils import column_index_from_string

        row = int(m.group(4))
        hdr_row = row - 1
        cols = {}
        if hdr_row >= 1:
            for c in ws[hdr_row]:
                if isinstance(c.value, str) and c.value.strip():
                    cols[c.value.strip()] = c.column
        return hdr_row, row, cols or {"_start_col": column_index_from_string(m.group(3).upper())}
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 60)):
        texts = {str(c.value).strip(): c.column for c in row if isinstance(c.value, str) and c.value.strip()}
        if header and header.strip() in texts:
            return row[0].row, row[0].row + 1, texts
    raise ValueError(f"머리글 '{header}' 이(가) 있는 행을 찾지 못했습니다 (위쪽 60행 안).")


def inspect_template(p: Path, max_items: int = 40) -> dict[str, Any]:
    wb = _load(p)
    out: dict[str, Any] = {"path": str(p), "sheets": [], "names": [], "charts": _chart_count(p)}
    try:
        out["names"] = [n for n in wb.defined_names.keys()] if hasattr(wb.defined_names, "keys") else []
    except Exception:  # noqa: BLE001
        out["names"] = []
    for ws in wb.worksheets:
        s: dict[str, Any] = {"name": ws.title, "range": ws.dimensions, "placeholders": [], "inputs": [], "tables": [],
                             "merged": len(ws.merged_cells.ranges), "formulas": 0}
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if isinstance(v, str):
                    if v.startswith("="):
                        s["formulas"] += 1
                        continue
                    toks = [a or b for a, b in _TOKEN.findall(v)]
                    if toks and len(s["placeholders"]) < max_items:
                        s["placeholders"].append({"cell": c.coordinate, "text": v[:80], "tokens": toks})
                    elif v.strip().endswith((":", "：")) or (len(v.strip()) <= 12 and not toks):
                        # 라벨 오른쪽(병합이면 그 끝 다음)이 비어 있으면 입력칸 후보
                        right = ws.cell(c.row, c.column + 1)
                        for rng in ws.merged_cells.ranges:
                            if c.coordinate in rng:
                                right = ws.cell(c.row, rng.max_col + 1)
                        right = _top_left(ws, right.coordinate)
                        if right.value in (None, "") and len(s["inputs"]) < max_items and c.row <= 60:
                            s["inputs"].append({"label": v.strip()[:30], "label_cell": c.coordinate, "input_cell": right.coordinate})
        # 표: 문자열 3개 이상이 이어진 행 → 머리글, 아래 첫 빈 행
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 80)):
            heads = [(c.column_letter, str(c.value).strip()) for c in row if isinstance(c.value, str) and c.value.strip() and not c.value.startswith("=")]
            if len(heads) >= 3 and len(s["tables"]) < 5:
                r = row[0].row + 1
                while r <= ws.max_row and any(ws.cell(r, ws[f"{col}1"].column).value not in (None, "") for col, _ in heads):
                    r += 1
                s["tables"].append({"header_row": row[0].row, "columns": [f"{col}:{h}" for col, h in heads][:20], "first_empty_row": r})
        # 표 머리글로 잡힌 줄의 라벨은 입력칸 후보에서 뺌
        hdr_rows = {t["header_row"] for t in s["tables"]}
        s["inputs"] = [x for x in s["inputs"] if int(re.sub(r"\D", "", x["label_cell"])) not in hdr_rows]
        out["sheets"].append(s)
    return out


def format_inspect(d: dict[str, Any]) -> str:
    lines = [f"## 엑셀 양식: {Path(d['path']).name}" + (f" (차트 {d['charts']}개)" if d["charts"] else "")]
    if d["names"]:
        lines.append("이름 정의: " + ", ".join(d["names"][:20]))
    for s in d["sheets"]:
        lines.append(f"\n### 시트 '{s['name']}' 범위 {s['range']} · 수식 {s['formulas']}개 · 병합 {s['merged']}개")
        if s["placeholders"]:
            lines.append("자리표시: " + "; ".join(f"{p['cell']} {p['tokens']}" for p in s["placeholders"]))
        if s["inputs"]:
            lines.append("입력칸 후보: " + "; ".join(f"{x['label']} → {x['input_cell']}" for x in s["inputs"]))
        for t in s["tables"]:
            lines.append(f"표: 머리글 {t['header_row']}행 [{', '.join(t['columns'])}] → 첫 빈 행 {t['first_empty_row']}")
    lines.append("\nfill_excel 로 채우세요: cells={'시트!셀': 값}, replace={'{{자리표시}}': 값}, tables=[{sheet, header 또는 start, rows}]")
    return "\n".join(lines)


def fill(src: Path, dst: Path, cells: dict[str, Any] | None = None, replace: dict[str, Any] | None = None,
         tables: list[dict[str, Any]] | None = None, sheet: str | None = None) -> dict[str, Any]:
    wb = _load(src)
    charts_before = _chart_count(src)
    default = wb[sheet] if sheet else wb.active
    rep: dict[str, Any] = {"cells": 0, "replaced": 0, "rows": 0, "warnings": []}
    for ref, value in (cells or {}).items():
        try:
            c = _resolve(wb, ref, default)
        except KeyError as e:
            rep["warnings"].append(str(e))
            continue
        c.value = _coerce(value, c)
        rep["cells"] += 1
    if replace:
        keys = {str(k): v for k, v in replace.items()}
        norm = {re.sub(r"\s+", "", k): v for k, v in keys.items()}
        found: set[str] = set()
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    v = c.value
                    if not isinstance(v, str) or v.startswith("="):
                        continue
                    whole = re.sub(r"\s+", "", v)
                    if whole in norm:  # 셀 전체가 자리표시 → 값의 타입 유지 (숫자는 숫자)
                        c.value = _coerce(norm[whole], c) if isinstance(norm[whole], str) else norm[whole]
                        found.add(whole)
                        rep["replaced"] += 1
                        continue
                    new = v
                    for k, val in keys.items():
                        if k in new:
                            new = new.replace(k, "" if val is None else str(val))
                            found.add(re.sub(r"\s+", "", k))
                    if new != v:
                        c.value = new
                        rep["replaced"] += 1
        missing = [k for k in keys if re.sub(r"\s+", "", k) not in found]
        if missing:
            rep["warnings"].append("양식에서 찾지 못한 자리표시: " + ", ".join(missing))
    for t in tables or []:
        ws = wb[t["sheet"]] if t.get("sheet") else default
        try:
            hdr_row, first, cols = _header_row(ws, t.get("header"), t.get("start"))
        except ValueError as e:
            rep["warnings"].append(str(e))
            continue
        rows = list(t.get("rows") or [])
        if not rows:
            continue
        start_col = cols.pop("_start_col", None) or (min(cols.values()) if cols else 1)
        if t.get("start"):
            from openpyxl.utils import column_index_from_string

            start_col = column_index_from_string(_CELL.match(t["start"]).group(3).upper())
        r0 = first
        if t.get("insert") and len(rows) > 1:
            ws.insert_rows(r0 + 1, len(rows) - 1)
            rep["warnings"].append(f"'{ws.title}' 에 {len(rows) - 1}행을 끼워 넣었습니다. 아래쪽 수식의 범위는 엑셀에서 확인하세요.")
        width = max((len(r) if isinstance(r, list) else len(cols)) for r in rows)
        template = [copy.copy(ws.cell(r0, start_col + i)._style) for i in range(max(width, 1))]
        overwrote = 0
        for i, r in enumerate(rows):
            rr = r0 + i
            if isinstance(r, dict):
                items = []
                for k, v in r.items():
                    col = cols.get(str(k).strip())
                    if col is None:
                        rep["warnings"].append(f"'{ws.title}' 머리글에 없는 열: {k}")
                        continue
                    items.append((col, v))
            else:
                items = [(start_col + j, v) for j, v in enumerate(r)]
            for col, v in items:
                c = _top_left(ws, ws.cell(rr, col).coordinate)
                if i and c.has_style is False and col - start_col < len(template):
                    c._style = copy.copy(template[col - start_col])
                if c.value not in (None, "") and not t.get("insert"):
                    overwrote += 1
                c.value = _coerce(v, c)
            rep["rows"] += 1
        if overwrote:
            rep["warnings"].append(f"'{ws.title}' 에서 값이 있던 셀 {overwrote}개를 덮어썼습니다 (insert: true 면 행을 끼워 넣음).")
    try:
        from openpyxl.workbook.properties import CalcProperties

        wb.calculation = wb.calculation or CalcProperties()
        wb.calculation.fullCalcOnLoad = True  # 열면 수식(합계 등)을 다시 계산
    except Exception:  # noqa: BLE001
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    after = _chart_count(dst)
    if after < charts_before:
        rep["warnings"].append(f"양식의 차트 {charts_before}개 중 {charts_before - after}개가 저장 과정에서 빠졌습니다. 차트는 엑셀에서 다시 확인하세요.")
    return rep


def make_excel_tools(ws: Any, links: Any = None) -> list[BuiltinTool]:
    async def done(text: str, p: Path) -> ToolOutcome:
        st = await file_status(p, ws, stable_wait=0)
        if links is not None:
            try:
                text += f"\n다운로드 링크: {links.url(p)}"
            except Exception:  # noqa: BLE001
                pass
        return ToolOutcome(text + "\n" + format_status(st), False, [st])

    async def inspect_excel_template(args: dict[str, Any]) -> ToolOutcome:
        p = ws.resolve(str(args["path"]))
        try:
            d = await asyncio.to_thread(inspect_template, p)
        except (ValueError, OSError, KeyError) as e:
            return ToolOutcome(f"오류: {e}", True)
        return ToolOutcome(format_inspect(d), False, [], d)

    async def fill_excel(args: dict[str, Any]) -> ToolOutcome:
        src = ws.resolve(str(args["template"]))
        rel = str(args.get("output") or f"{src.stem}_작성{src.suffix}").lstrip("/\\")
        if not Path(rel).suffix:
            rel += src.suffix
        dst = ws.resolve(rel if rel.startswith(("files/", "downloads/", "uploads/")) else f"files/{rel}", write=True)
        if dst.resolve() == src.resolve():
            return ToolOutcome("오류: 원본 양식에 덮어쓸 수 없습니다. output 에 다른 이름을 주세요.", True)
        try:
            rep = await asyncio.to_thread(fill, src, dst, args.get("cells"), args.get("replace"), args.get("tables"), args.get("sheet"))
        except (ValueError, OSError, KeyError) as e:
            return ToolOutcome(f"오류: {e}", True)
        text = (f"엑셀 양식 채우기 완료: {dst}\n- 셀 {rep['cells']}개, 자리표시 {rep['replaced']}곳, 표 {rep['rows']}행 (원본 서식·수식 유지, 열면 수식 다시 계산)"
                + ("".join(f"\n⚠ {w}" for w in rep["warnings"])))
        return await done(text, dst)

    return [
        BuiltinTool(
            "inspect_excel_template",
            "회사 엑셀 양식(보고서 서식)의 구조를 봅니다: 시트별 {{자리표시}}, 라벨 옆 입력칸, 표 머리글과 첫 빈 행, 병합 셀, 수식 수, 이름 정의. "
            "fill_excel 로 채우기 전에 먼저 호출하세요.",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            inspect_excel_template,
        ),
        BuiltinTool(
            "fill_excel",
            "기존 엑셀 양식을 복사해 값을 채웁니다 (서식·수식·병합·열 너비 유지, 원본은 그대로). "
            "cells={'B3': 값, '요약!C5': 값}, replace={'{{month}}': '2026-10'}, "
            "tables=[{sheet, header: '품목' 또는 start: 'A6', rows: [[..]] 또는 [{'품목': .., '금액': ..}], insert: false}]. "
            "숫자는 숫자로, 'YYYY-MM-DD' 는 날짜 칸이면 날짜로, '='로 시작하면 수식으로 넣습니다.",
            {
                "type": "object",
                "properties": {
                    "template": {"type": "string", "description": "양식 파일 경로 (xlsx/xlsm)"},
                    "output": {"type": "string", "description": "저장할 경로 (files 기준, 기본: <양식>_작성.xlsx)"},
                    "sheet": {"type": "string", "description": "cells 의 시트 이름을 생략했을 때 쓸 시트 (기본: 활성 시트)"},
                    "cells": {"type": "object", "description": "{셀주소|시트!셀|이름정의: 값}"},
                    "replace": {"type": "object", "description": "{자리표시: 값} - 예: {'{{month}}': '10월'}"},
                    "tables": {"type": "array", "items": {"type": "object"}, "description": "[{sheet, header|start, rows, insert}]"},
                },
                "required": ["template"],
            },
            fill_excel,
        ),
    ]
