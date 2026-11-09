"""엑셀 데이터 규칙: 과거의 정상 엑셀에서 열마다 규칙을 배우고, 새 엑셀을 검사합니다.

열 규칙 (자동으로 배움)
  type      number | date | code | text   (95% 이상이 그 형식일 때)
  required  거의 모든 행(98%)에 값이 있으면 필수
  unique    파일마다 값이 겹치지 않으면 중복 금지 (문서번호 같은 키)
  allowed   종류가 적은 글·코드 열이면 허용값 목록 (부서, 상태 등)
  min/max   숫자·날짜 범위 (검사할 때는 관측 범위에서 여유를 둠), nonneg: 음수가 없었으면 음수 금지
  pattern   코드 모양이 일정하면 정규식 (예: AB-1234 → [A-Z]{2}-\\d{4})
사람이 고친 규칙(excel_rules_update)은 overrides 로 따로 보관해 다시 배워도 유지됩니다.
저장: rulebooks/_data_rules/<이름>.json (모든 사용자가 공유)
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import analyzers

_DATE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$")
_CODE = re.compile(r"^(?=.*\d)[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*$")  # 영문·숫자 코드 (한글이 섞이면 글)
NAME_RE = re.compile(r"^[A-Za-z0-9가-힣][\w가-힣.-]{0,63}$")


class DataRuleError(ValueError):
    pass


def _norm_date(v: str) -> str | None:
    m = _DATE.match(v.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2200):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def classify(v: Any) -> str | None:
    """값 하나의 형식. 빈 값이면 None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "text"
    if isinstance(v, (int, float)):
        return "number"
    s = str(v).strip()
    if not s:
        return None
    if _norm_date(s):
        return "date"
    if re.fullmatch(r"0\d+", s):
        return "code"  # 00123 처럼 앞자리 0 이 있는 번호
    if re.fullmatch(r"[-+(]?[\d,]+(\.\d+)?[)%원]?", s) and analyzers.to_number(s) is not None:
        return "number"
    if re.search(r"\d", s) and len(s) <= 30 and _CODE.match(s):
        return "code"  # A-001, 2026-001, AB1234
    return "text"


def shape(s: str) -> str:
    """값의 모양 → 정규식 (글자 종류별로 묶음)."""
    out, prev, n = [], None, 0

    def flush() -> None:
        if prev is None:
            return
        out.append(prev + (f"{{{n}}}" if n > 1 else ""))

    for ch in s:
        k = r"\d" if ch.isdigit() else "[A-Z]" if "A" <= ch <= "Z" else "[a-z]" if "a" <= ch <= "z" else "[가-힣]" if "가" <= ch <= "힣" else re.escape(ch)
        if k == prev:
            n += 1
        else:
            flush()
            prev, n = k, 1
    flush()
    return "^" + "".join(out) + "$"


def _pick_table(path: Path, sheet: str | None) -> Any:
    tables = analyzers.load_tables(path)
    if not tables:
        raise DataRuleError(f"표를 찾지 못했습니다: {path.name}")
    if sheet:
        for t in tables:
            if t.name == sheet:
                return t
        raise DataRuleError(f"{path.name} 에 시트 '{sheet}' 가 없습니다 (있는 시트: {', '.join(t.name for t in tables)})")
    return max(tables, key=lambda t: len(t.rows))


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else analyzers.to_number(v)


def learn_columns(tables: list[Any]) -> dict[str, dict[str, Any]]:
    cols: dict[str, dict[str, Any]] = {}
    names = list(dict.fromkeys(c for t in tables for c in t.columns))
    for name in names:
        present = [t for t in tables if name in t.columns]
        vals_per_file = [[r[t.columns.index(name)] if t.columns.index(name) < len(r) else None for r in t.rows] for t in present]
        allv = [v for vs in vals_per_file for v in vs]
        filled = [v for v in allv if classify(v) is not None]
        rule: dict[str, Any] = {"column": name, "files": len(present), "rows": len(allv), "filled": len(filled)}
        if not filled:
            rule["type"] = "empty"
            cols[name] = rule
            continue
        kinds = Counter(classify(v) for v in filled)
        top, cnt = kinds.most_common(1)[0]
        typ = top if cnt >= 0.95 * len(filled) else "text"
        if typ == "number" and kinds.get("code"):
            typ = "number" if kinds["number"] >= 0.95 * len(filled) else "text"
        rule["type"] = typ
        rule["required"] = len(present) == len(tables) and len(filled) >= 0.98 * len(allv)
        if len(filled) >= 5 and all(len(set(map(str, [v for v in vs if classify(v)]))) == len([v for v in vs if classify(v)]) for vs in vals_per_file):
            rule["unique"] = True
        strs = [str(v).strip() for v in filled]
        if typ == "number":
            nums = [x for x in (_num(v) for v in filled) if x is not None]
            if nums:
                rule["min"], rule["max"] = min(nums), max(nums)
                rule["nonneg"] = min(nums) >= 0
                rule["integer"] = all(float(x).is_integer() for x in nums)
        elif typ == "date":
            ds = sorted(d for d in (_norm_date(s) for s in strs) if d)
            if ds:
                rule["min"], rule["max"] = ds[0], ds[-1]
        else:
            distinct = sorted(set(strs))
            if not rule.get("unique") and len(distinct) <= 15 and len(strs) >= 3 * len(distinct):
                rule["allowed"] = distinct
            shapes = Counter(shape(s) for s in strs)
            sh, n = shapes.most_common(1)[0]
            if typ == "code" and n >= 0.95 * len(strs):
                rule["pattern"] = sh
            rule["max_len"] = max(len(s) for s in strs)
        cols[name] = rule
    return cols


def _store(rules_path: Path) -> Path:
    d = Path(rules_path) / "_data_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(rules_path: Path, name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise DataRuleError(f"규칙 이름은 한글/영문/숫자와 . _ - 로 (최대 64자): {name}")
    return _store(rules_path) / f"{name}.json"


def load(rules_path: Path, name: str) -> dict[str, Any]:
    p = _file(rules_path, name)
    if not p.is_file():
        have = ", ".join(sorted(x.stem for x in _store(rules_path).glob("*.json"))) or "(없음)"
        raise DataRuleError(f"데이터 규칙 '{name}' 이(가) 없습니다. 있는 규칙: {have}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(rules_path: Path, rs: dict[str, Any]) -> None:
    _file(rules_path, rs["name"]).write_text(json.dumps(rs, ensure_ascii=False, indent=1), encoding="utf-8")


def effective(rs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cols = {k: dict(v) for k, v in rs["columns"].items()}
    for col, ov in (rs.get("overrides") or {}).items():
        cols.setdefault(col, {"column": col})
        for k, v in ov.items():
            if v is None:
                cols[col].pop(k, None)
            else:
                cols[col][k] = v
    return cols


def learn(rules_path: Path, name: str, paths: list[Path], sheet: str | None = None, description: str = "",
          append: bool = False, user: str | None = None) -> dict[str, Any]:
    try:
        old = load(rules_path, name)
    except DataRuleError:
        old = None
    files = [str(p) for p in paths]
    if append and old:
        files = list(dict.fromkeys([f for f in old.get("learned_from", []) if Path(f).is_file()] + files))
    tables = [_pick_table(Path(f), sheet or (old or {}).get("sheet")) for f in files]
    rs = {
        "name": name, "description": description or (old or {}).get("description", ""), "sheet": sheet or (old or {}).get("sheet"),
        "learned_from": files, "columns": learn_columns(tables), "overrides": (old or {}).get("overrides", {}),
        "version": int((old or {}).get("version", 0)) + 1, "updated": time.time(), "updated_by": user,
        "changelog": ((old or {}).get("changelog", []) + [{"time": time.time(), "user": user, "action": "learn", "files": len(files)}])[-50:],
    }
    _save(rules_path, rs)
    return rs


def update(rules_path: Path, name: str, column: str, changes: dict[str, Any], reason: str, user: str | None = None) -> dict[str, Any]:
    rs = load(rules_path, name)
    allowed_keys = {"type", "required", "unique", "allowed", "min", "max", "nonneg", "integer", "pattern", "max_len", "ignore"}
    bad = set(changes) - allowed_keys
    if bad:
        raise DataRuleError(f"바꿀 수 없는 항목: {', '.join(sorted(bad))} (가능: {', '.join(sorted(allowed_keys))})")
    if not reason.strip():
        raise DataRuleError("reason(바꾸는 이유)을 적어 주세요.")
    if "pattern" in changes and changes["pattern"]:
        re.compile(str(changes["pattern"]))
    ov = rs.setdefault("overrides", {}).setdefault(column, {})
    ov.update(changes)
    rs["version"] = int(rs.get("version", 0)) + 1
    rs["updated"], rs["updated_by"] = time.time(), user
    rs.setdefault("changelog", []).append({"time": time.time(), "user": user, "action": "update", "column": column, "changes": changes, "reason": reason})
    _save(rules_path, rs)
    return rs


def validate(rules_path: Path, name: str, path: Path, sheet: str | None = None, max_examples: int = 5) -> dict[str, Any]:
    rs = load(rules_path, name)
    t = _pick_table(path, sheet or rs.get("sheet"))
    cols = effective(rs)
    issues: list[dict[str, Any]] = []

    def add(level: str, col: str, rule: str, msg: str, rows: list[int] | None = None, values: list[Any] | None = None) -> None:
        issues.append({"level": level, "column": col, "rule": rule, "message": msg, "rows": (rows or [])[:max_examples],
                       "count": len(rows or []) or 1, "values": [str(v)[:40] for v in (values or [])][:max_examples]})

    first = t.header_row + 2  # 엑셀 행 번호 (머리글 다음)
    for col, r in cols.items():
        if r.get("ignore") or r.get("type") == "empty":
            continue
        if col not in t.columns:
            add("error" if r.get("required") else "warn", col, "column", "열이 없습니다")
            continue
        i = t.columns.index(col)
        vals = [(first + k, row[i] if i < len(row) else None) for k, row in enumerate(t.rows)]
        empty = [n for n, v in vals if classify(v) is None]
        if r.get("required") and empty:
            add("error", col, "required", f"빈 칸 {len(empty)}개 (필수 열)", empty)
        filled = [(n, v) for n, v in vals if classify(v) is not None]
        typ = r.get("type")
        if typ in ("number", "date", "code"):
            wrong = [(n, v) for n, v in filled if classify(v) != typ and not (typ == "code" and classify(v) == "number" and r.get("pattern") is None)]
            if wrong:
                label = {"number": "숫자", "date": "날짜", "code": "코드"}[typ]
                add("error", col, "type", f"{label}가 아닌 값 {len(wrong)}개", [n for n, _ in wrong], [v for _, v in wrong])
        if r.get("unique"):
            seen: dict[str, int] = {}
            dup = []
            for n, v in filled:
                k = str(v).strip()
                if k in seen:
                    dup.append((n, v))
                seen.setdefault(k, n)
            if dup:
                add("error", col, "unique", f"중복 값 {len(dup)}개", [n for n, _ in dup], [v for _, v in dup])
        if r.get("allowed"):
            allowed = set(map(str, r["allowed"]))
            new = [(n, v) for n, v in filled if str(v).strip() not in allowed]
            if new:
                add("warn", col, "allowed", f"처음 보는 값 {len(new)}개 (허용값: {', '.join(list(allowed)[:10])})", [n for n, _ in new], [v for _, v in new])
        if typ == "number":
            nums = [(n, _num(v)) for n, v in filled]
            nums = [(n, x) for n, x in nums if x is not None]
            if r.get("nonneg"):
                neg = [(n, x) for n, x in nums if x < 0]
                if neg:
                    add("error", col, "nonneg", f"음수 {len(neg)}개 (과거 자료에는 음수가 없음)", [n for n, _ in neg], [x for _, x in neg])
            if r.get("min") is not None and r.get("max") is not None:
                lo, hi = float(r["min"]), float(r["max"])
                span = (hi - lo) or abs(hi) or 1
                out = [(n, x) for n, x in nums if x < lo - 0.5 * span or x > hi + 0.5 * span]
                if out:
                    add("warn", col, "range", f"평소 범위({lo:,.0f}~{hi:,.0f})를 크게 벗어난 값 {len(out)}개", [n for n, _ in out], [x for _, x in out])
            if r.get("integer"):
                frac = [(n, x) for n, x in nums if not float(x).is_integer()]
                if frac:
                    add("warn", col, "integer", f"소수 {len(frac)}개 (과거 자료는 정수)", [n for n, _ in frac], [x for _, x in frac])
        if typ == "date" and r.get("min"):
            ds = [(n, _norm_date(str(v))) for n, v in filled]
            far = [(n, d) for n, d in ds if d and (d < str(int(r["min"][:4]) - 1) or d > str(int(r["max"][:4]) + 2))]
            if far:
                add("warn", col, "range", f"날짜가 평소와 크게 다른 값 {len(far)}개 ({r['min']}~{r['max']})", [n for n, _ in far], [d for _, d in far])
        if r.get("pattern"):
            pat = re.compile(r["pattern"])
            bad = [(n, v) for n, v in filled if not pat.match(str(v).strip())]
            if bad:
                add("warn", col, "pattern", f"코드 모양이 다른 값 {len(bad)}개 (형식 {r['pattern']})", [n for n, _ in bad], [v for _, v in bad])
        if r.get("max_len"):
            long = [(n, v) for n, v in filled if len(str(v)) > int(r["max_len"]) * 2]
            if long:
                add("warn", col, "max_len", f"평소보다 매우 긴 값 {len(long)}개", [n for n, _ in long], [v for _, v in long])
    extra = [c for c in t.columns if c not in cols]
    errors = sum(1 for x in issues if x["level"] == "error")
    warns = sum(1 for x in issues if x["level"] == "warn")
    score = max(0, 100 - errors * 15 - warns * 5)
    return {"rules": name, "version": rs.get("version"), "file": path.name, "sheet": t.name, "rows": len(t.rows),
            "issues": issues, "errors": errors, "warnings": warns, "extra_columns": extra, "score": score, "passed": errors == 0}


def format_rules(rs: dict[str, Any]) -> str:
    lines = [f"## 데이터 규칙 '{rs['name']}' v{rs.get('version')} · 배운 파일 {len(rs.get('learned_from', []))}개" + (f" · 시트 {rs['sheet']}" if rs.get("sheet") else "")]
    if rs.get("description"):
        lines.append(rs["description"])
    lines.append("| 열 | 형식 | 필수 | 중복금지 | 범위/허용값/모양 |\n|---|---|---|---|---|")
    for col, r in effective(rs).items():
        if r.get("ignore"):
            lines.append(f"| {col} | (검사 안 함) | | | |")
            continue
        extra = []
        if r.get("min") is not None:
            extra.append(f"{r['min']}~{r['max']}" + (" · 음수 금지" if r.get("nonneg") else ""))
        if r.get("allowed"):
            extra.append("허용: " + ", ".join(map(str, r["allowed"][:8])) + ("…" if len(r["allowed"]) > 8 else ""))
        if r.get("pattern"):
            extra.append(f"모양 {r['pattern']}")
        mark = " ✏️" if col in (rs.get("overrides") or {}) else ""
        lines.append(f"| {col}{mark} | {r.get('type')} | {'예' if r.get('required') else ''} | {'예' if r.get('unique') else ''} | {' · '.join(extra)} |")
    if rs.get("overrides"):
        lines.append("\n✏️ = 사람이 고친 규칙 (다시 배워도 유지)")
    return "\n".join(lines)


def format_report(r: dict[str, Any]) -> str:
    head = (f"## 데이터 검사: {r['file']} (시트 {r['sheet']}, {r['rows']}행) ← 규칙 '{r['rules']}' v{r['version']}\n"
            f"- 결과: {'통과' if r['passed'] else '오류 있음'} · 점수 {r['score']} · 오류 {r['errors']} · 경고 {r['warnings']}")
    lines = [head]
    for x in sorted(r["issues"], key=lambda x: (x["level"] != "error", x["column"])):
        rows = f" (행 {', '.join(map(str, x['rows']))}{'…' if x['count'] > len(x['rows']) else ''})" if x["rows"] else ""
        vals = f" 예: {', '.join(x['values'])}" if x["values"] else ""
        lines.append(f"- {'❌' if x['level'] == 'error' else '⚠️'} [{x['column']}] {x['message']}{rows}{vals}")
    if r["extra_columns"]:
        lines.append(f"- ℹ️ 규칙에 없는 새 열: {', '.join(r['extra_columns'])}")
    if not r["issues"]:
        lines.append("- 문제 없음")
    lines.append("\n정상 자료인데 걸린 규칙은 excel_rules_update 로 고치세요 (예: 허용값 추가, 범위 넓히기, ignore).")
    return "\n".join(lines)


def make_data_rule_tools(ws: Any, rules_path: Path) -> list[Any]:
    import asyncio

    from .mcp_hub import BuiltinTool, ToolOutcome
    from .users import current_user

    def paths_of(raw: Any) -> list[Path]:
        if isinstance(raw, str):
            raw = [x.strip() for x in re.split(r"[\n,]", raw) if x.strip()]
        out: list[Path] = []
        for x in raw or []:
            p = ws.resolve(str(x))
            out += sorted(q for q in p.rglob("*") if q.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".csv")) if p.is_dir() else [p]
        return out

    async def run(fn: Any, *a: Any) -> ToolOutcome | Any:
        try:
            return await asyncio.to_thread(fn, *a)
        except (DataRuleError, OSError, re.error) as e:
            return ToolOutcome(f"오류: {e}", True)

    async def excel_rules_learn(args: dict[str, Any]) -> ToolOutcome:
        files = paths_of(args.get("paths"))
        if not files:
            return ToolOutcome("오류: paths 에 정상 자료(엑셀/CSV 파일 또는 폴더)를 주세요.", True)
        rs = await run(learn, rules_path, str(args["name"]), files, args.get("sheet"), str(args.get("description") or ""),
                       bool(args.get("append")), current_user.get())
        return rs if isinstance(rs, ToolOutcome) else ToolOutcome(format_rules(rs), False, [], rs)

    async def excel_rules_validate(args: dict[str, Any]) -> ToolOutcome:
        r = await run(validate, rules_path, str(args["name"]), ws.resolve(str(args["path"])), args.get("sheet"), int(args.get("max_examples") or 5))
        return r if isinstance(r, ToolOutcome) else ToolOutcome(format_report(r), False, [], r)

    async def excel_rules_update(args: dict[str, Any]) -> ToolOutcome:
        rs = await run(update, rules_path, str(args["name"]), str(args["column"]), dict(args.get("changes") or {}),
                       str(args.get("reason") or ""), current_user.get())
        return rs if isinstance(rs, ToolOutcome) else ToolOutcome("규칙을 고쳤습니다.\n" + format_rules(rs), False, [], rs)

    async def excel_rules_list(args: dict[str, Any]) -> ToolOutcome:
        if args.get("name"):
            rs = await run(load, rules_path, str(args["name"]))
            return rs if isinstance(rs, ToolOutcome) else ToolOutcome(format_rules(rs), False, [], rs)
        items = []
        for p in sorted(_store(rules_path).glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(f"- {d['name']}: {d.get('description') or ''} (v{d.get('version')}, 열 {len(d.get('columns', {}))}개, 배운 파일 {len(d.get('learned_from', []))}개)")
        return ToolOutcome("\n".join(items) or "만든 데이터 규칙이 없습니다. excel_rules_learn 으로 과거 정상 자료에서 배우세요.")

    obj = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
    return [
        BuiltinTool("excel_rules_learn",
                    "과거의 정상 엑셀/CSV(파일 여러 개 또는 폴더)에서 열마다 데이터 규칙을 배웁니다: 형식(숫자/날짜/코드/글), 필수, 중복 금지, "
                    "허용값 목록, 숫자·날짜 범위, 음수 금지, 코드 모양. 같은 이름이면 다시 배우고(append=true 면 이전 파일도 포함), 사람이 고친 규칙은 유지.",
                    obj({"name": {"type": "string", "description": "규칙 이름 (예: 월매출)"}, "paths": {"type": "array", "items": {"type": "string"}},
                         "sheet": {"type": "string"}, "description": {"type": "string"}, "append": {"type": "boolean"}}, ["name", "paths"]),
                    excel_rules_learn),
        BuiltinTool("excel_rules_validate",
                    "새 엑셀/CSV 를 데이터 규칙으로 검사합니다: 빈 필수값, 형식이 다른 값, 중복, 처음 보는 값, 범위를 크게 벗어난 값, 음수, 코드 모양. "
                    "문제마다 엑셀 행 번호와 예시를 알려줍니다.",
                    obj({"name": {"type": "string"}, "path": {"type": "string"}, "sheet": {"type": "string"}, "max_examples": {"type": "integer"}}, ["name", "path"]),
                    excel_rules_validate),
        BuiltinTool("excel_rules_update",
                    "데이터 규칙 한 열을 고칩니다 (정상 자료인데 걸렸을 때). changes 예: {\"allowed\": [..]}, {\"max\": 5000000}, {\"required\": false}, "
                    "{\"ignore\": true}, {\"pattern\": null}(값 null 은 규칙 삭제). reason 필수 - 변경 기록에 남습니다.",
                    obj({"name": {"type": "string"}, "column": {"type": "string"}, "changes": {"type": "object"}, "reason": {"type": "string"}},
                        ["name", "column", "changes", "reason"]),
                    excel_rules_update),
        BuiltinTool("excel_rules_list", "데이터 규칙 목록, 또는 name 을 주면 그 규칙의 열별 내용을 봅니다.",
                    obj({"name": {"type": "string"}}, []), excel_rules_list),
    ]
