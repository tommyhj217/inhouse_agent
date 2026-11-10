"""파일 작성 후 자동 검증 (opencode 가 코드 수정 뒤 LSP 진단을 받아 바로 고치는 것과 같은 목적).

에이전트가 결과 파일(작업폴더 files/ 아래 PPT·엑셀)을 만들거나 고치면, 그 도구 결과 끝에 검사 결과를 붙입니다.
문제가 있으면 모델이 같은 차례에 바로 고칠 수 있고, 사용자에게 깨진 파일을 넘기지 않습니다.
LLM 호출이 없고(토큰 0), 문제가 없으면 한 줄만 붙여 입력 토큰을 거의 늘리지 않습니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

EXCEL_ERRORS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!")
MAX_CELLS = 200_000
SKIP_TOOLS = {"check_pptx_layout", "render_preview", "file_versions", "excel_rules_validate", "rulebook_validate"}


def check_excel(path: Path) -> dict[str, Any]:
    """엑셀 기본 점검: 열리는지, 빈 시트, 오류값(#REF! 등)·깨진 참조가 든 셀."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(str(path), read_only=True)
    except Exception as e:  # noqa: BLE001 - 열리지 않으면 그것이 결과
        return {"ok": False, "issues": [f"파일이 열리지 않습니다: {type(e).__name__}: {str(e)[:160]}"]}
    issues: list[str] = []
    seen = 0
    try:
        for ws in wb.worksheets:
            bad: list[str] = []
            filled = 0
            for row in ws.iter_rows():
                for c in row:
                    seen += 1
                    v = c.value
                    if v is None or v == "":
                        continue
                    filled += 1
                    if isinstance(v, str) and any(err in v for err in EXCEL_ERRORS) and len(bad) < 5:
                        bad.append(f"{getattr(c, 'coordinate', '?')}={v[:40]}")
                if seen > MAX_CELLS:
                    break
            if filled == 0:
                issues.append(f"'{ws.title}' 시트가 비어 있습니다")
            if bad:
                issues.append(f"'{ws.title}' 시트에 오류값·깨진 참조: {', '.join(bad)}")
            if seen > MAX_CELLS:
                break
    finally:
        wb.close()
    return {"ok": not issues, "issues": issues}


def auto_check(path: Path) -> str | None:
    """PPT·엑셀 결과 파일 검사 → 도구 결과에 붙일 짧은 글 (검사 대상이 아니면 None)."""
    suf = path.suffix.lower()
    name = path.name
    try:
        if suf == ".pptx":
            from .pptx_tools import check_layout

            r = check_layout(path)
            if r["passed"]:
                return f"[자동 검사] {name}: 레이아웃 이상 없음"
            lines = [f"[자동 검사] {name}: 레이아웃 문제 {r['issues']}건 - 사용자에게 넘기기 전에 고치세요"]
            for s in r["slides"]:
                for x in s["issues"][:2]:
                    lines.append(f"- #{s['no']} {x['message']}")
                if len(lines) > 6:
                    break
            lines.append("(정확히 보려면 render_preview → view_image)")
            return "\n".join(lines)
        if suf in (".xlsx", ".xlsm"):
            r = check_excel(path)
            if r["ok"]:
                return f"[자동 검사] {name}: 이상 없음"
            return f"[자동 검사] {name}: 문제 {len(r['issues'])}건 - 사용자에게 넘기기 전에 고치세요\n" + "\n".join(f"- {x}" for x in r["issues"][:5])
    except Exception as e:  # noqa: BLE001 - 검사 실패가 작업을 막지 않게
        return f"[자동 검사] {name}: 검사하지 못했습니다 ({type(e).__name__})"
    return None


def verify_outputs(files: list[dict[str, Any]], outputs_dir: Path | None) -> str:
    """도구가 돌려준 파일 중 결과 폴더(files/) 안의 PPT·엑셀만 검사 (내려받은 원본은 검사하지 않음)."""
    notes = []
    for f in files:
        p = Path(str(f.get("path") or ""))
        if not p.is_file():
            continue
        if outputs_dir is not None:
            try:
                p.resolve().relative_to(outputs_dir.resolve())
            except ValueError:
                continue
            if "_preview" in p.parts:
                continue
        note = auto_check(p)
        if note:
            notes.append(note)
    return "\n".join(notes)
