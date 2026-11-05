"""여러 문서(PPT / 엑셀 / Word / PDF)를 비교 분석.

- PPT ↔ PPT (Word/PDF 도 같은 방식): 장(슬라이드)을 제목으로 맞춰 짝짓고
  추가/삭제된 장, 제목 변경, 본문 추가/삭제 문장, 숫자 변화, 표/차트/이미지 수 변화
- 엑셀 ↔ 엑셀(CSV): 시트별 열 추가/삭제, 행 수, 숫자 열 합계 차이, 키 열 기준 행 추가/삭제/값 변경
- PPT ↔ 엑셀: PPT 에 적힌 숫자가 엑셀 값(셀·열 합계)과 맞는지 교차 확인 (단위 만/억/천/백만, 반올림 허용)
- 3개 이상이면 첫 파일(baseline)과 나머지를 각각 비교

채팅에 첨부한 파일을 에이전트가 스스로 비교할 때, LLM 없이(토큰 없이) 첨부만 했을 때 모두 사용.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import analyzers
from .docstruct import doc_structure, outline_text
from .files import file_status
from .mcp_hub import BuiltinTool, ToolOutcome

DECK = ("pptx", "docx", "pdf")
TABLE = ("xlsx", "csv")
_NUM = re.compile(r"(?<![\w.])([-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?)\s*(%|퍼센트|조|억|백만|천만|만|천|원|건|개|명|대|배|p|pt|%p)?")
_UNIT = {"조": 1e12, "억": 1e8, "천만": 1e7, "백만": 1e6, "만": 1e4, "천": 1e3}


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s or "").lower())


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s} if s else set()


def _dice(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return 2 * len(x & y) / (len(x) + len(y))


def numbers(text: str) -> list[tuple[float, str]]:
    """본문에서 의미 있는 숫자 추출 → (값, 원문). 페이지 번호 같은 1~2자리 정수 단독 값은 제외."""
    out = []
    for m in _NUM.finditer(str(text or "")):
        raw, unit = m.group(1), m.group(2) or ""
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if not unit and "." not in raw and "," not in raw and abs(v) < 100:
            continue  # 장 번호, 순번 등
        if re.fullmatch(r"(19|20)\d\d", raw) and not unit:
            continue  # 연도
        out.append((v * _UNIT.get(unit, 1), (raw + unit).strip()))
    return out


def _sec_text(s: dict[str, Any]) -> str:
    parts = [s.get("title") or "", *s.get("texts", [])]
    for t in s.get("tables") or []:
        parts += [" ".join(map(str, t.get("columns") or []))] + [" ".join(map(str, r)) for r in t.get("sample") or []]
    for c in s.get("charts") or []:
        parts.append(c.get("title") or "")
    return "\n".join(p for p in parts if p)


def align_sections(a: list[dict[str, Any]], b: list[dict[str, Any]], threshold: float = 0.5) -> list[tuple[int | None, int | None, float]]:
    """제목(없으면 본문 앞부분) 유사도로 장을 짝지음. 같은 순서를 우선(위치 보너스)."""
    def key(s: dict[str, Any]) -> str:
        return (s.get("title") or "") + " " + " ".join(s.get("texts", [])[:2])[:60]

    cands = []
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            sim = max(_dice(x.get("title") or "", y.get("title") or ""), 0.85 * _dice(key(x), key(y)))
            pos = 1 - abs(i / max(len(a), 1) - j / max(len(b), 1))
            cands.append((sim + 0.05 * pos, sim, i, j))
    cands.sort(reverse=True)
    used_a, used_b, pairs = set(), set(), []
    for _, sim, i, j in cands:
        if sim < threshold or i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j, round(sim, 2)))
    # 남은 장 중 같은 자리(앞뒤 짝 사이)에 하나씩 남은 것은 '내용이 바뀐 같은 장' 으로 봄
    pairs.sort()
    out: list[tuple[int | None, int | None, float]] = []
    for i in range(len(a)):
        if i not in used_a:
            out.append((i, None, 0.0))
    for j in range(len(b)):
        if j not in used_b:
            out.append((None, j, 0.0))
    out += pairs
    orphans_a = [i for i, j, _ in out if j is None]
    orphans_b = [j for i, j, _ in out if i is None]
    if len(orphans_a) == len(orphans_b) == 1:
        i, j = orphans_a[0], orphans_b[0]
        prev = [(pi, pj) for pi, pj, _ in pairs if pi < i]
        if not prev or prev[-1][1] < j:
            out = [x for x in out if x[0] != i and x[1] != j] + [(i, j, round(_dice(_sec_text(a[i]), _sec_text(b[j])), 2))]
    out.sort(key=lambda x: (x[1] if x[1] is not None else (x[0] or 0) + 0.5, x[0] if x[0] is not None else -1))
    return out


def compare_decks(a: dict[str, Any], b: dict[str, Any], max_items: int = 8) -> dict[str, Any]:
    sa, sb = a["sections"], b["sections"]
    pairs = align_sections(sa, sb)
    removed = [sa[i] for i, j, _ in pairs if j is None]
    added = [sb[j] for i, j, _ in pairs if i is None]
    changed, same = [], 0
    matched = [(i, j) for i, j, _ in pairs if i is not None and j is not None]
    # 앞뒤 순서가 뒤집힌 짝만 '이동' (장 추가/삭제로 번호만 밀린 것은 이동 아님)
    moved = {(i, j) for i, j in matched if any((i2 < i and j2 > j) or (i2 > i and j2 < j) for i2, j2 in matched)}
    for i, j, sim in pairs:
        if i is None or j is None:
            continue
        x, y = sa[i], sb[j]
        tx, ty = [t.strip("- ").strip() for t in x["texts"]], [t.strip("- ").strip() for t in y["texts"]]
        plus = [t for t in ty if t not in tx]
        minus = [t for t in tx if t not in ty]
        nx = {r for _, r in numbers(_sec_text(x))}
        ny = {r for _, r in numbers(_sec_text(y))}
        d: dict[str, Any] = {"a": x["no"], "b": y["no"], "title_a": x["title"], "title_b": y["title"], "similarity": sim}
        if _norm(x["title"]) != _norm(y["title"]):
            d["title_changed"] = True
        if plus:
            d["added_text"] = plus[:max_items]
        if minus:
            d["removed_text"] = minus[:max_items]
        if nx != ny:
            d["numbers"] = {"before": sorted(nx - ny)[:max_items], "after": sorted(ny - nx)[:max_items]}
        for k in ("tables", "charts"):
            if len(x[k]) != len(y[k]):
                d[k] = (len(x[k]), len(y[k]))
        if x["images"] != y["images"]:
            d["images"] = (x["images"], y["images"])
        if x.get("notes", "") != y.get("notes", ""):
            d["notes_changed"] = True
        if (i, j) in moved:
            d["moved"] = True
        if len(d) > 5 or d.get("title_changed"):
            changed.append(d)
        else:
            same += 1
    return {"type": "deck", "a": a["name"], "b": b["name"], "count": (len(sa), len(sb)), "added": [{"no": s["no"], "title": s["title"]} for s in added],
            "removed": [{"no": s["no"], "title": s["title"]} for s in removed], "changed": changed, "unchanged": same,
            "reordered": bool(moved),
            "words": (sum(s["words"] for s in sa), sum(s["words"] for s in sb))}


def _sum(t: Any, col: str) -> float | None:
    i = t.columns.index(col)
    vals = [analyzers.to_number(r[i]) for r in t.rows if i < len(r)]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _auto_key(a: Any, b: Any) -> list[str]:
    for c in a.columns:
        if c not in b.columns:
            continue
        ia, ib = a.columns.index(c), b.columns.index(c)
        va = [str(r[ia]) for r in a.rows if ia < len(r)]
        vb = [str(r[ib]) for r in b.rows if ib < len(r)]
        if va and vb and len(set(va)) == len(va) and len(set(vb)) == len(vb) and len(set(va) & set(vb)) >= 0.3 * min(len(va), len(vb)):
            return [c]
    return []


def compare_tables_struct(pa: Path, pb: Path, key_columns: list[str] | None = None, max_items: int = 8) -> dict[str, Any]:
    ta, tb = analyzers.load_tables(pa), analyzers.load_tables(pb)
    by_name = {t.name: t for t in tb}
    sheets = []
    for i, a in enumerate(ta):
        b = by_name.get(a.name) or (tb[i] if len(ta) == len(tb) == 1 or (i < len(tb) and tb[i].name not in {x.name for x in ta}) else None)
        if b is None:
            sheets.append({"sheet": a.name, "only_in": "a"})
            continue
        common = [c for c in b.columns if c in a.columns]
        d: dict[str, Any] = {"sheet": a.name if a.name == b.name else f"{a.name} ↔ {b.name}", "rows": (len(a.rows), len(b.rows))}
        if set(a.columns) != set(b.columns):
            d["columns_added"] = [c for c in b.columns if c not in a.columns]
            d["columns_removed"] = [c for c in a.columns if c not in b.columns]
        sums = []
        for c in common:
            x, y = _sum(a, c), _sum(b, c)
            if x is None or y is None:
                continue
            if abs(x - y) > 1e-9:
                sums.append({"column": c, "a": x, "b": y, "diff": y - x, "pct": round((y - x) / x * 100, 1) if x else None})
        if sums:
            d["sums"] = sums
        keys = [k for k in (key_columns or []) if k in a.columns and k in b.columns] or _auto_key(a, b)
        if keys:
            def k(t: Any, r: list[Any]) -> tuple:
                return tuple(str(r[t.columns.index(c)]) for c in keys)
            ma, mb = {k(a, r): r for r in a.rows}, {k(b, r): r for r in b.rows}
            add, rem = [x for x in mb if x not in ma], [x for x in ma if x not in mb]
            chg = []
            for key in [x for x in mb if x in ma]:
                diffs = [(c, ma[key][a.columns.index(c)], mb[key][b.columns.index(c)]) for c in common
                         if str(ma[key][a.columns.index(c)]) != str(mb[key][b.columns.index(c)])]
                if diffs:
                    chg.append(("/".join(key), diffs))
            d["key"] = keys
            d["added_rows"], d["removed_rows"], d["changed_rows"] = len(add), len(rem), len(chg)
            d["added_sample"] = ["/".join(x) for x in add[:max_items]]
            d["removed_sample"] = ["/".join(x) for x in rem[:max_items]]
            d["changed_sample"] = [f"{key}: " + ", ".join(f"{c} {o} → {v}" for c, o, v in diffs[:4]) for key, diffs in chg[:max_items]]
        else:
            ra, rb = {tuple(map(str, r)) for r in a.rows}, {tuple(map(str, r)) for r in b.rows}
            d["added_rows"], d["removed_rows"] = len(rb - ra), len(ra - rb)
        sheets.append(d)
    for t in tb:
        if t.name not in {x.name for x in ta} and not any(s.get("sheet", "").endswith(f"↔ {t.name}") for s in sheets):
            sheets.append({"sheet": t.name, "only_in": "b"})
    return {"type": "table", "a": pa.name, "b": pb.name, "sheets": sheets}


def cross_check(deck: dict[str, Any], table_path: Path, max_items: int = 15) -> dict[str, Any]:
    """PPT(문서)의 숫자가 엑셀 값과 맞는지 확인."""
    values: list[float] = []
    for t in analyzers.load_tables(table_path):
        cols = list(zip(*[r + [""] * (len(t.columns) - len(r)) for r in t.rows])) if t.rows else []
        for col in cols:
            nums = [v for v in (analyzers.to_number(x) for x in col) if v is not None]
            values += nums
            if len(nums) > 1:
                values.append(sum(nums))
                values.append(sum(nums) / len(nums))
    vs = sorted(set(values))

    import bisect

    def near(t: float, tol: float) -> bool:
        k = bisect.bisect_left(vs, t - tol)
        return k < len(vs) and vs[k] <= t + tol

    def found(v: float, raw: str) -> bool:
        digits = re.match(r"[-+]?[\d,]+(?:\.(\d+))?", raw)
        dec = len(digits.group(1) or "") if digits else 0
        half = 0.5 * 10 ** (-dec)
        if near(v, max(abs(v) * 0.005, half)):  # 그대로(반올림 허용)
            return True
        if raw.endswith("%") and near(v / 100, max(abs(v) * 0.00005, half / 100)):  # 엑셀 0.153 = PPT 15.3%
            return True
        if raw[-1:].isdigit():  # 단위 표기 없이 '(단위: 백만원)' 처럼 줄여 적은 값
            for scale in (1e3, 1e4, 1e6, 1e8):
                if near(v * scale, max(abs(v) * scale * 0.005, half * scale)):
                    return True
        return False

    ok, miss = 0, []
    for s in deck["sections"]:
        for v, raw in numbers(_sec_text(s)):
            if found(v, raw):
                ok += 1
            else:
                ctx = next((t for t in [s["title"], *s["texts"]] if raw in t), "")
                miss.append({"no": s["no"], "value": raw, "context": ctx[:80]})
    return {"type": "cross", "a": deck["name"], "b": table_path.name, "matched": ok, "unmatched": len(miss), "unmatched_sample": miss[:max_items]}


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.2f}"


def format_pair(r: dict[str, Any]) -> str:
    out = []
    if r["type"] == "deck":
        out.append(f"### {r['a']} ↔ {r['b']} (장 {r['count'][0]} → {r['count'][1]}, 단어 {r['words'][0]} → {r['words'][1]})")
        out.append(f"- 같은 장 {r['unchanged']} · 바뀐 장 {len(r['changed'])} · 추가 {len(r['added'])} · 삭제 {len(r['removed'])}" + (" · 순서 변경 있음" if r["reordered"] else ""))
        if r["added"]:
            out.append("- 추가된 장: " + ", ".join(f"#{s['no']} {s['title']}" for s in r["added"]))
        if r["removed"]:
            out.append("- 삭제된 장: " + ", ".join(f"#{s['no']} {s['title']}" for s in r["removed"]))
        for d in r["changed"]:
            head = f"- #{d['a']}→#{d['b']} " + (f"제목 '{d['title_a']}' → '{d['title_b']}'" if d.get("title_changed") else f"[{d['title_b']}]")
            sub = []
            if d.get("numbers"):
                sub.append(f"숫자 {', '.join(d['numbers']['before']) or '-'} → {', '.join(d['numbers']['after']) or '-'}")
            if d.get("added_text"):
                sub.append("추가: " + " / ".join(t[:60] for t in d["added_text"][:3]))
            if d.get("removed_text"):
                sub.append("삭제: " + " / ".join(t[:60] for t in d["removed_text"][:3]))
            for k, lab in (("tables", "표"), ("charts", "차트"), ("images", "이미지")):
                if d.get(k):
                    sub.append(f"{lab} {d[k][0]}→{d[k][1]}")
            if d.get("notes_changed"):
                sub.append("발표자 노트 변경")
            if d.get("moved"):
                sub.append("위치 이동")
            out.append(head + ("\n    " + "\n    ".join(sub) if sub else ""))
    elif r["type"] == "table":
        out.append(f"### {r['a']} ↔ {r['b']} (표 비교)")
        for s in r["sheets"]:
            if s.get("only_in"):
                out.append(f"- 시트 '{s['sheet']}': {'앞' if s['only_in'] == 'a' else '뒤'} 파일에만 있음")
                continue
            line = f"- 시트 '{s['sheet']}': {s['rows'][0]}행 → {s['rows'][1]}행"
            if s.get("key"):
                line += f" (키 {', '.join(s['key'])}: 추가 {s['added_rows']} / 삭제 {s['removed_rows']} / 값 변경 {s['changed_rows']})"
            else:
                line += f" (행 추가 {s['added_rows']} / 삭제 {s['removed_rows']})"
            out.append(line)
            if s.get("columns_added") or s.get("columns_removed"):
                out.append(f"    열 추가 {s.get('columns_added') or '-'} / 삭제 {s.get('columns_removed') or '-'}")
            for x in s.get("sums") or []:
                out.append(f"    합계 '{x['column']}': {_fmt(x['a'])} → {_fmt(x['b'])} ({'+' if x['diff'] >= 0 else ''}{_fmt(x['diff'])}" + (f", {x['pct']:+.1f}%" if x["pct"] is not None else "") + ")")
            for k, lab in (("added_sample", "추가"), ("removed_sample", "삭제"), ("changed_sample", "변경")):
                if s.get(k):
                    out.append(f"    {lab} 예: " + "; ".join(s[k][:5]))
    else:
        tot = r["matched"] + r["unmatched"]
        out.append(f"### {r['a']} 의 숫자 ↔ {r['b']} 값 교차 확인")
        out.append(f"- 숫자 {tot}개 중 엑셀에서 확인 {r['matched']}개, 확인 안 됨 {r['unmatched']}개")
        for m in r["unmatched_sample"]:
            out.append(f"    #{m['no']} {m['value']}" + (f" - {m['context']}" if m["context"] else ""))
    return "\n".join(out)


def compare_documents(paths: list[Path], key_columns: list[str] | None = None, max_items: int = 8) -> tuple[str, dict[str, Any]]:
    if len(paths) < 2:
        raise ValueError("비교하려면 파일이 2개 이상 필요합니다.")
    structs, errors = [], []
    for p in paths:
        try:
            structs.append(doc_structure(p))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.name}: {e}")
            structs.append(None)
    ok = [(p, s) for p, s in zip(paths, structs) if s is not None]
    if len(ok) < 2:
        raise ValueError("비교할 수 있는 파일이 2개 미만입니다. " + "; ".join(errors))
    lines = ["## 문서 비교", "| 파일 | 종류 | 장/시트 | 표 | 차트 | 이미지 | 단어 | 숫자 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for p, s in ok:
        secs = s["sections"]
        lines.append(f"| {s['name']} | {s['kind']} | {len(secs)} | {sum(len(x['tables']) for x in secs)} | {sum(len(x['charts']) for x in secs)} | "
                     f"{sum(x['images'] for x in secs)} | {sum(x['words'] for x in secs)} | {sum(len(numbers(_sec_text(x))) for x in secs)} |")
    base_p, base = ok[0]
    pairs = []
    for p, s in ok[1:]:
        if base["kind"] in TABLE and s["kind"] in TABLE:
            r = compare_tables_struct(base_p, p, key_columns, max_items)
        elif base["kind"] in DECK and s["kind"] in DECK:
            r = compare_decks(base, s, max_items)
        elif base["kind"] in DECK and s["kind"] in TABLE:
            r = cross_check(base, p)
        else:
            r = cross_check(s, base_p)
        pairs.append(r)
        lines += ["", format_pair(r)]
    # 엑셀이 여러 개 섞여 있으면 PPT 와도 교차 확인
    decks = [(p, s) for p, s in ok if s["kind"] in DECK]
    tables = [(p, s) for p, s in ok if s["kind"] in TABLE]
    if decks and tables and len(ok) > 2:
        for dp, ds in decks:
            for tp, _ in tables:
                if any(r["type"] == "cross" and r["a"] == ds["name"] and r["b"] == tp.name for r in pairs):
                    continue
                r = cross_check(ds, tp)
                pairs.append(r)
                lines += ["", format_pair(r)]
    if errors:
        lines += ["", "(비교에서 제외: " + "; ".join(errors) + ")"]
    lines += ["", f"(기준 파일: {base['name']} - 나머지를 이 파일과 비교했습니다)" if len(ok) > 2 else ""]
    return "\n".join(lines).rstrip(), {"files": [s["name"] for _, s in ok], "pairs": pairs}


def attachment_note(paths: list[str | Path], max_chars: int = 1500) -> str:
    """채팅 첨부 문서를 LLM 에 알려주는 안내문 (경로 + 장별 요약)."""
    lines = [f"[첨부 파일 {len(paths)}개 - 사용자가 채팅에 붙여넣은 파일입니다]"]
    for p in paths:
        p = Path(p)
        try:
            d = doc_structure(p)
            o = outline_text(d, max_text=100)
            lines.append(f"- 경로: {p}\n{o[:max_chars]}{'…(생략, doc_structure 로 전체 확인)' if len(o) > max_chars else ''}")
        except Exception:  # noqa: BLE001
            lines.append(f"- 경로: {p} (구조 요약 불가 - analyze_file / read_file_text 로 확인)")
    if len(paths) >= 2:
        lines.append("파일이 2개 이상이니 compare_documents 로 먼저 비교하고, 필요한 장은 doc_structure / query_table / compare_tables 로 자세히 보세요.")
    else:
        lines.append("analyze_file / doc_structure 로 내용을 확인하세요. 규칙집이 있으면 rulebook_validate 로 검증할 수 있습니다.")
    return "\n".join(lines)


def attachment_steps(paths: list[str]) -> list[dict[str, Any]]:
    """LLM 없이 첨부 문서를 분석하는 워크플로 단계: 2개 이상이면 비교 + 각 파일 장별 구조."""
    steps: list[dict[str, Any]] = []
    if len(paths) >= 2:
        steps.append({"id": "cmp", "title": "첨부 파일 비교", "tool": "compare_documents", "args": {"paths": list(paths), "save_report": True}, "on_error": "continue"})
    for i, p in enumerate(paths[:6], 1):
        steps.append({"id": f"doc{i}", "title": f"구조: {Path(p).name}", "tool": "doc_structure", "args": {"path": str(p)}, "on_error": "continue"})
    return steps


def make_compare_tools(ws: Any) -> list[BuiltinTool]:
    async def compare_documents_tool(args: dict[str, Any]) -> ToolOutcome:
        raw = args.get("paths") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in re.split(r"[\n,]", raw) if x.strip()]
        paths = [ws.resolve(str(p)) for p in raw]
        for p in paths:
            if not p.is_file():
                return ToolOutcome(f"오류: 파일이 없습니다: {p}", True)
        try:
            text, data = await asyncio.to_thread(compare_documents, paths, args.get("key_columns"), int(args.get("max_items") or 8))
        except ValueError as e:
            return ToolOutcome(f"오류: {e}", True)
        files = []
        if args.get("save_report"):
            out = Path(ws.files) / "compare" / f"{datetime.now():%Y%m%d_%H%M%S}_비교.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
            files.append(await file_status(out, ws, stable_wait=0))
            text += f"\n\n보고서 저장: {out}"
        return ToolOutcome(text, False, files, data)

    return [
        BuiltinTool(
            "compare_documents",
            "PPT·엑셀·Word·PDF 파일 2개 이상을 비교 분석합니다. PPT↔PPT: 장별로 짝지어 추가/삭제/바뀐 장, 문장·숫자·표·차트 변화. "
            "엑셀↔엑셀: 시트별 열/행/합계/값 변경(키 열 자동 탐지). PPT↔엑셀: PPT 숫자가 엑셀 값과 맞는지 교차 확인. "
            "첫 파일이 기준. 사용자가 첨부한 파일을 비교·검토할 때 먼저 쓰세요.",
            {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "비교할 파일 경로들 (첫 번째가 기준)"},
                    "key_columns": {"type": "array", "items": {"type": "string"}, "description": "엑셀 행을 식별하는 열 (생략 시 자동)"},
                    "max_items": {"type": "integer", "description": "항목별 예시 최대 개수 (기본 8)"},
                    "save_report": {"type": "boolean", "description": "비교 결과를 md 파일로 저장"},
                },
                "required": ["paths"],
            },
            compare_documents_tool,
        )
    ]
