"""제품 개발 규칙 학습: 과거 개발 건(베이스 → 개발 모델)과 BOM 에서 사람들이 한 일을 규칙 후보로 뽑고,
사람이 확인한 답을 쌓아 가며 반복할수록 정확해지는 규칙집을 만든다. LLM 을 부르지 않는다 (토큰 0).

입력
- 모델명 규칙: 이름 붙은 그룹이 있는 정규식. 예) (?P<series>[A-Z]+\\d+)(?P<color>[A-Z])\\.(?P<suffix>\\w+)
- 개발 건 표(엑셀/CSV): 베이스 모델, 개발 모델, (선택) 툴·향지·스펙 열
- BOM 표(엑셀/CSV): 모델, 레벨, 품번, 설명 (레벨로 부모를 찾음). 파일 하나에 여러 모델 또는 모델마다 파일

뽑는 규칙 (모두 지지 건수 / 해당 건수 / 근거 사례를 가짐)
- change  : 모델명·개발 건의 속성(색상 등)이 바뀌면 어느 부품 자리가 바뀌는가
- catalog : 툴(없으면 시리즈)별로 그 부품 자리에 어떤 값(색상 등)의 품번이 개발되어 있는가
- base    : 개발자들이 베이스를 고를 때 무엇을 같게 유지했는가, 어떤 베이스를 많이 썼는가

반복 (iteration)
  learn(자료 추가) → review(사람에게 물을 후보) → answer(맞음/틀림/수정, 이유 기록) → export_skill(확정 규칙을 스킬에)
  → evaluate(과거 건으로 예측 정확도) → 새 개발 건이 생기면 다시 learn. 사람의 답은 다시 학습해도 유지된다.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .analyzers import load_tables

ALIASES = {
    "model": ("model", "모델", "모델명", "model name", "modelname"),
    "level": ("level", "lv", "lvl", "레벨", "수준", "bom level"),
    "part": ("part", "part no", "part_no", "partno", "part number", "p/n", "품번", "부품번호", "자재코드", "자재번호"),
    "desc": ("description", "desc", "설명", "품명", "부품명", "자재명", "part name", "spec", "규격"),
    "base": ("base", "base model", "베이스", "베이스모델", "기준모델", "베이스 모델"),
    "dev": ("dev", "dev model", "development model", "개발모델", "개발 모델", "개발모델명", "신규모델", "model"),
    "tool": ("tool", "툴", "tooling", "금형", "플랫폼", "platform"),
}
MIN_SUPPORT = 2  # 이보다 적게 본 것은 후보로 내지 않음
STATUS_KO = {"candidate": "후보(확인 필요)", "confirmed": "확정", "rejected": "틀림", "edited": "수정해서 확정"}


class ProductRuleError(ValueError):
    pass


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def _col(columns: list[str], kind: str, override: str | None = None) -> int | None:
    low = [_norm(c).lower() for c in columns]
    if override:
        o = _norm(override).lower()
        if o in low:
            return low.index(o)
        raise ProductRuleError(f"'{override}' 열이 없습니다. 있는 열: {', '.join(columns)}")
    for a in ALIASES[kind]:
        if a in low:
            return low.index(a)
    for a in ALIASES[kind]:
        for i, c in enumerate(low):
            if a in c:
                return i
    return None


def _level(v: Any) -> int:
    """BOM 레벨: 1, "2", 3.0, "..3"(점 개수 + 숫자), "1.2.1"(깊이) 모두 받음."""
    t = str(v if v is not None else "").strip()
    if re.fullmatch(r"\d+(\.0+)?", t):
        return int(float(t))
    if re.fullmatch(r"\.*\d+", t):
        return int(t.lstrip("."))
    if re.fullmatch(r"\d+(\.\d+)+", t):
        return t.count(".") + 1
    return 1


def parse_model(name: str, pattern: str | None) -> dict[str, str]:
    """모델명 → 속성. 규칙이 없으면 '이름.서픽스' 로만 나눔."""
    name = _norm(name)
    if pattern:
        m = re.fullmatch(pattern, name) or re.match(pattern, name)
        if m:
            return {k: v for k, v in m.groupdict().items() if v is not None}
    head, _, suffix = name.partition(".")
    return {"name": head, **({"suffix": suffix} if suffix else {})}


def read_boms(paths: list[Path], cols: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """BOM → {모델: {부품 자리: 품번}}. 부품 자리 = '부모 설명 > 설명' (레벨로 부모를 찾음).
    같은 자리에 품번이 여러 개면 정렬해 '+' 로 묶음."""
    cols = cols or {}
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for p in paths:
        for t in load_tables(p):
            i_part, i_desc = _col(t.columns, "part", cols.get("part")), _col(t.columns, "desc", cols.get("desc"))
            if i_part is None or i_desc is None:
                continue  # BOM 이 아닌 시트
            i_level, i_model = _col(t.columns, "level", cols.get("level")), _col(t.columns, "model", cols.get("model"))
            stack: list[tuple[int, str]] = []
            for r in t.rows:
                part, desc = _norm(r[i_part]), _norm(r[i_desc])
                if not part:
                    continue
                model = _norm(r[i_model]) if i_model is not None else p.stem
                level = _level(r[i_level]) if i_level is not None else 1
                while stack and stack[-1][0] >= level:
                    stack.pop()
                parent = stack[-1][1] if stack else ""
                stack.append((level, desc))
                slot = f"{parent} > {desc}" if parent else desc
                out[model][slot].append(part)
    return {m: {s: "+".join(sorted(set(ps))) for s, ps in slots.items()} for m, slots in out.items()}


def read_projects(paths: list[Path], cols: dict[str, str] | None = None) -> list[dict[str, str]]:
    """개발 건 표 → [{base, dev, tool?, 그 밖의 열…}]"""
    cols = cols or {}
    out = []
    for p in paths:
        for t in load_tables(p):
            i_base = _col(t.columns, "base", cols.get("base"))
            if i_base is None:
                continue
            i_dev = _col([c if i != i_base else "" for i, c in enumerate(t.columns)], "dev", cols.get("dev"))
            if i_dev is None:
                continue
            i_tool = _col(t.columns, "tool", cols.get("tool"))
            for r in t.rows:
                base, dev = _norm(r[i_base]), _norm(r[i_dev])
                if not base or not dev:
                    continue
                row = {"base": base, "dev": dev}
                if i_tool is not None and _norm(r[i_tool]):
                    row["tool"] = _norm(r[i_tool])
                for i, c in enumerate(t.columns):
                    if i not in (i_base, i_dev, i_tool) and _norm(r[i]) and _norm(c):
                        row[_norm(c)] = _norm(r[i])
                out.append(row)
    return out


def _rid(kind: str, *parts: str) -> str:
    return f"{kind}-" + hashlib.md5("|".join(parts).encode()).hexdigest()[:8]


def _attrs(model: str, pattern: str | None, row: dict[str, str], side: str) -> dict[str, str]:
    """모델의 속성: 모델명에서 뽑은 것 (+ 개발 모델이면 개발 건 표의 향지·스펙 열)."""
    a = parse_model(model, pattern)
    if side == "dev":
        a.update({k: v for k, v in row.items() if k not in ("base", "dev")})
    return a


def group_of(model: str, row: dict[str, str] | None, pattern: str | None, group_by: str) -> str:
    """모델이 속한 묶음: 개발 건 표의 툴 열 → 없으면 모델명의 group_by 속성 → series → 이름."""
    attrs = parse_model(model, pattern)
    g = (row or {}).get("tool") if group_by == "tool" else None
    return g or attrs.get(group_by) or attrs.get("series") or attrs.get("name") or ""


def tool_map(projects: list[dict[str, str]], pattern: str | None, group_by: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in projects:
        for side in ("base", "dev"):
            out.setdefault(row[side], group_of(row[side], row, pattern, group_by))
    return out


def mine(projects: list[dict[str, str]], boms: dict[str, dict[str, str]], pattern: str | None, group_by: str = "tool") -> list[dict[str, Any]]:
    """규칙 후보를 뽑음."""
    rules: list[dict[str, Any]] = []
    pairs = []
    for row in projects:
        b, d = boms.get(row["base"]), boms.get(row["dev"])
        ab, ad = parse_model(row["base"], pattern), parse_model(row["dev"], pattern)
        changed_attrs = sorted(k for k in set(ab) | set(ad) if ab.get(k) != ad.get(k) and k != "suffix")
        changed_slots = sorted(s for s in set(b) | set(d) if b.get(s) != d.get(s)) if b is not None and d is not None else None
        pairs.append({"row": row, "ab": ab, "ad": ad, "changed_attrs": changed_attrs, "changed_slots": changed_slots})

    # ---- change: 속성이 바뀐 건 중 그 부품 자리가 바뀐 비율이 높고, 그 속성이 안 바뀐 건에서는 잘 안 바뀌는 자리
    with_bom = [p for p in pairs if p["changed_slots"] is not None]
    for attr in sorted({a for p in with_bom for a in p["changed_attrs"]}):
        on = [p for p in with_bom if attr in p["changed_attrs"]]
        off = [p for p in with_bom if attr not in p["changed_attrs"]]
        if len(on) < MIN_SUPPORT:
            continue
        cnt = Counter(s for p in on for s in p["changed_slots"])
        for slot, n in cnt.most_common():
            off_n = sum(slot in p["changed_slots"] for p in off)
            if n < MIN_SUPPORT or n / len(on) < 0.6:
                continue
            lift_ok = not off or off_n / len(off) <= 0.5 * n / len(on)  # 그 속성과 상관없이 늘 바뀌는 자리는 뺌
            if not lift_ok:
                continue
            ex = [p for p in on if slot in p["changed_slots"]][:3]
            rules.append({
                "id": _rid("change", attr, slot), "kind": "change", "attr": attr, "slot": slot, "support": n, "total": len(on),
                "exceptions": [f"{p['row']['base']} → {p['row']['dev']}" for p in on if slot not in p["changed_slots"]][:5],
                "text": f"모델명의 '{attr}' 이(가) 바뀌면 '{slot}' 부품이 바뀐다",
                "question": f"'{attr}' 이(가) 바뀌는 개발에서는 '{slot}' 부품도 바꿔야 합니까? ({n}/{len(on)}건에서 바뀜)",
                "evidence": [f"{p['row']['base']} → {p['row']['dev']} ({attr}: {p['ab'].get(attr, '-')} → {p['ad'].get(attr, '-')}): "
                             f"{boms[p['row']['base']].get(slot, '없음')} → {boms[p['row']['dev']].get(slot, '없음')}" for p in ex],
            })

    # ---- catalog: 그룹(툴/시리즈)별로 속성 값마다 개발된 품번 (change 규칙이 있는 자리만)
    tool_of = tool_map(projects, pattern, group_by)
    for r in [x for x in rules if x["kind"] == "change"]:
        table: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
        for model, slots in boms.items():
            val = parse_model(model, pattern).get(r["attr"])
            if val is None or r["slot"] not in slots or model not in tool_of:
                continue
            table[tool_of[model]][val][slots[r["slot"]]] += 1
        for tool, vals in sorted(table.items()):
            if not tool or sum(sum(c.values()) for c in vals.values()) < MIN_SUPPORT:
                continue
            mapping = {v: c.most_common(1)[0][0] for v, c in sorted(vals.items())}
            clean = sum(c.most_common(1)[0][1] for c in vals.values())
            total = sum(sum(c.values()) for c in vals.values())
            rules.append({
                "id": _rid("catalog", tool, r["attr"], r["slot"]), "kind": "catalog", "tool": tool, "attr": r["attr"], "slot": r["slot"],
                "support": clean, "total": total, "mapping": mapping, "exceptions": [],
                "text": f"'{tool}' 의 '{r['slot']}' 은(는) {r['attr']} {len(mapping)}종이 개발되어 있다: "
                        + ", ".join(f"{v}={pn}" for v, pn in mapping.items()),
                "question": f"'{tool}' 의 '{r['slot']}' 에 개발된 {r['attr']} 은(는) {', '.join(mapping)} 이(가) 전부입니까? (품번: "
                            + ", ".join(f"{v}={pn}" for v, pn in mapping.items()) + ")",
                "evidence": [f"{v}: {pn} ({vals[v][pn]}개 모델)" for v, pn in mapping.items()],
            })

    # ---- base: 베이스를 고를 때 같게 유지한 속성, 많이 쓴 베이스
    if len(pairs) >= MIN_SUPPORT:
        for attr in sorted({k for p in pairs for k in set(p["ab"]) & set(p["ad"])} - {"suffix"}):
            both = [p for p in pairs if attr in p["ab"] and attr in p["ad"]]
            same = [p for p in both if p["ab"][attr] == p["ad"][attr]]
            if len(both) >= MIN_SUPPORT and len(same) / len(both) >= 0.7:
                rules.append({
                    "id": _rid("base", "same", attr), "kind": "base", "attr": attr, "support": len(same), "total": len(both),
                    "exceptions": [f"{p['row']['base']} → {p['row']['dev']}" for p in both if p not in same][:5],
                    "text": f"베이스는 개발 모델과 '{attr}' 이(가) 같은 모델에서 고른다",
                    "question": f"베이스 모델을 고를 때 '{attr}' 이(가) 같은 모델을 우선합니까? ({len(same)}/{len(both)}건)",
                    "evidence": [f"{p['row']['base']} → {p['row']['dev']} ({attr}={p['ab'][attr]})" for p in same[:3]],
                })
        by_group: dict[str, Counter] = defaultdict(Counter)
        for p in pairs:
            by_group[tool_of.get(p["row"]["dev"], "")][p["row"]["base"]] += 1
        for g, c in sorted(by_group.items()):
            base, n = c.most_common(1)[0]
            total = sum(c.values())
            if g and n >= MIN_SUPPORT and n / total >= 0.5:
                rules.append({
                    "id": _rid("base", "popular", g), "kind": "base", "tool": g, "support": n, "total": total, "exceptions": [],
                    "base": base, "text": f"'{g}' 개발에서는 베이스로 '{base}' 을(를) 가장 많이 쓴다",
                    "question": f"'{g}' 의 새 개발은 특별한 이유가 없으면 '{base}' 을(를) 베이스로 합니까? ({n}/{total}건)",
                    "evidence": [f"{b}: {k}건" for b, k in c.most_common(3)],
                })
    return rules


class ProductRuleBook:
    """rulebooks/_product_rules/<이름>.json - 자료 요약 + 규칙(후보·확정·틀림) + 사람의 답 + 회차별 점수."""

    def __init__(self, root: Path, name: str):
        if not re.fullmatch(r"[0-9A-Za-z가-힣_-]{1,60}", name or ""):
            raise ProductRuleError("규칙집 이름은 한글/영문/숫자/_/- 로 60자 이내여야 합니다.")
        self.path = Path(root) / "_product_rules" / f"{name}.json"
        self.name = name
        self.data: dict[str, Any] = {"name": name, "pattern": None, "group_by": "tool", "cols": {}, "sources": {"projects": [], "boms": []},
                                     "rules": {}, "history": [], "rounds": 0}
        if self.path.is_file():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
        cols = self.data.get("cols") or {}
        projects = read_projects([Path(p) for p in self.data["sources"]["projects"]], cols)
        boms = read_boms([Path(p) for p in self.data["sources"]["boms"]], cols)
        if not projects:
            raise ProductRuleError("개발 건 표에서 베이스·개발 모델 열을 찾지 못했습니다. cols 로 열 이름을 알려 주세요 (예: {base: '기준모델', dev: '신규모델'}).")
        return projects, boms

    def learn(self, projects: list[Path], boms: list[Path], pattern: str | None = None, group_by: str | None = None,
              cols: dict[str, str] | None = None, user: str = "") -> dict[str, Any]:
        """자료를 (추가로) 읽고 규칙 후보를 다시 뽑음. 사람이 이미 답한 규칙의 상태·이유는 유지."""
        d = self.data
        if pattern:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ProductRuleError(f"모델명 규칙(정규식)이 올바르지 않습니다: {e}") from e
            d["pattern"] = pattern
        if group_by:
            d["group_by"] = group_by
        if cols:
            d["cols"] = {**(d.get("cols") or {}), **cols}
        for key, new in (("projects", projects), ("boms", boms)):
            d["sources"][key] = list(dict.fromkeys([*d["sources"][key], *[str(p) for p in new]]))
        rows, bom = self._load()
        mined = {r["id"]: r for r in mine(rows, bom, d["pattern"], d["group_by"])}
        old = d["rules"]
        added, gone = [i for i in mined if i not in old], []
        for rid, r in mined.items():
            prev = old.get(rid) or {}
            r["status"] = prev.get("status", "candidate")
            for k in ("answer", "answered_by", "answered_at", "note", "text_override", "first_round"):
                if k in prev:
                    r[k] = prev[k]
            r.setdefault("first_round", d["rounds"] + 1)
            if prev and prev.get("status") != "candidate" and (prev.get("support"), prev.get("total")) != (r["support"], r["total"]):
                r["changed_since_answer"] = True  # 답한 뒤 근거가 달라짐 → 다시 볼 만함
        for rid, prev in old.items():
            if rid not in mined:
                if prev.get("status") in ("confirmed", "edited", "rejected"):
                    mined[rid] = {**prev, "stale": True}  # 사람이 답한 규칙은 근거가 사라져도 지우지 않음
                else:
                    gone.append(rid)
        d["rules"] = mined
        d["tools"] = tool_map(rows, d["pattern"], d["group_by"])  # 모델 → 툴 (새 모델 예측 때 같은 툴의 개발 현황만 쓰려고)
        d["rounds"] += 1
        with_bom = sum(1 for r in rows if r["base"] in bom and r["dev"] in bom)
        d["stats"] = {"projects": len(rows), "with_bom": with_bom, "models_with_bom": len(bom)}
        d["history"].append({"round": d["rounds"], "time": time.time(), "by": user, "event": "learn", "projects": len(rows),
                             "rules": len(mined), "added": len(added), "removed": len(gone)})
        self.save()
        return {"round": d["rounds"], "projects": len(rows), "with_bom": with_bom, "models_with_bom": len(bom), "rules": len(mined),
                "added": added, "removed": gone, "missing_bom": sorted({m for r in rows for m in (r["base"], r["dev"]) if m not in bom})[:10]}

    def review(self, limit: int = 5) -> list[dict[str, Any]]:
        """사람에게 물어볼 후보: 아직 답이 없는 것 중 애매한 것(예외가 있는 것)과 근거가 많은 것 먼저."""
        cands = [r for r in self.data["rules"].values() if r.get("status") == "candidate" or r.get("changed_since_answer")]
        order = {"change": 0, "catalog": 1, "base": 2}  # 부품 변경 규칙부터, 그 안에서는 예외가 있는 애매한 것 먼저
        cands.sort(key=lambda r: (order.get(r["kind"], 9), -(r["support"] < r["total"]), -r["support"], r["id"]))
        return cands[:limit]

    def answer(self, rule_id: str, verdict: str, note: str = "", text: str = "", user: str = "") -> dict[str, Any]:
        r = self.data["rules"].get(rule_id)
        if r is None:
            raise ProductRuleError(f"규칙 '{rule_id}' 이(가) 없습니다.")
        verdict = {"yes": "confirmed", "맞음": "confirmed", "confirm": "confirmed", "no": "rejected", "틀림": "rejected", "reject": "rejected",
                   "edit": "edited", "수정": "edited"}.get(verdict.strip().lower(), verdict)
        if verdict not in ("confirmed", "rejected", "edited"):
            raise ProductRuleError("verdict 는 confirmed(맞음) / rejected(틀림) / edited(수정) 중 하나입니다.")
        if verdict == "edited" and not text.strip():
            raise ProductRuleError("수정(edited)일 때는 text 에 고친 규칙 문장을 적어 주세요.")
        r.update(status=verdict, answer=verdict, answered_by=user, answered_at=time.time(), note=note.strip())
        if text.strip():
            r["text_override"] = text.strip()
        r.pop("changed_since_answer", None)
        self.data["history"].append({"round": self.data["rounds"], "time": time.time(), "by": user, "event": "answer", "rule": rule_id,
                                     "verdict": verdict, "note": note.strip()})
        self.save()
        return r

    def active(self, include_candidates: bool = False) -> list[dict[str, Any]]:
        ok = ("confirmed", "edited", "candidate") if include_candidates else ("confirmed", "edited")
        return [r for r in self.data["rules"].values() if r.get("status") in ok]

    def predict(self, base: str, dev: str, include_candidates: bool = True, rules: list[dict[str, Any]] | None = None,
                tool: str | None = None) -> dict[str, Any]:
        """베이스 → 개발 모델일 때 바뀔 부품 자리와 (그 툴의 개발 현황에 있으면) 쓸 품번.
        툴: 인자 → 이미 아는 개발 모델 → 베이스의 툴 (같은 툴에서 파생한다고 봄) → 모델명 속성."""
        pat = self.data.get("pattern")
        tools = self.data.get("tools") or {}
        gb = self.data.get("group_by") or "tool"
        tool = tool or tools.get(dev) or tools.get(base) or (group_of(dev, None, pat, gb) if gb != "tool" else "")
        ab, ad = parse_model(base, pat), parse_model(dev, pat)
        changed = [k for k in set(ab) | set(ad) if ab.get(k) != ad.get(k) and k != "suffix"]
        rules = rules if rules is not None else self.active(include_candidates)
        out = []
        for r in rules:
            if r["kind"] != "change" or r["attr"] not in changed:
                continue
            part = None
            for c in rules:
                if c["kind"] == "catalog" and c["slot"] == r["slot"] and c["attr"] == r["attr"] and c.get("tool") == tool:
                    part = c["mapping"].get(ad.get(r["attr"], "")) or part
            out.append({"slot": r["slot"], "because": f"{r['attr']}: {ab.get(r['attr'], '-')} → {ad.get(r['attr'], '-')}", "part": part,
                        "rule": r["id"], "status": r["status"]})
        return {"changed_attrs": sorted(changed), "slots": out, "tool": tool}

    def evaluate(self) -> dict[str, Any]:
        """과거 개발 건으로 채점: 그 건을 빼고 뽑은 규칙으로 '바뀔 부품 자리'를 맞히는지 (정밀도·재현율).
        사람이 틀림이라고 한 규칙은 빼고, 확정한 규칙은 넣는다 → 답이 쌓일수록 점수가 달라진다."""
        rows, boms = self._load()
        pat, group_by = self.data.get("pattern"), self.data.get("group_by") or "tool"
        rejected = {i for i, r in self.data["rules"].items() if r.get("status") == "rejected"}
        tp = fp = fn = n = 0
        misses: Counter = Counter()
        for i, row in enumerate(rows):
            if row["base"] not in boms or row["dev"] not in boms:
                continue
            others = [r for r in mine(rows[:i] + rows[i + 1:], boms, pat, group_by) if r["id"] not in rejected]
            pred = {s["slot"] for s in self.predict(row["base"], row["dev"], rules=[{**r, "status": "candidate"} for r in others],
                                                    tool=row.get("tool"))["slots"]}
            b, d = boms[row["base"]], boms[row["dev"]]
            actual = {s for s in set(b) | set(d) if b.get(s) != d.get(s)}
            tp += len(pred & actual)
            fp += len(pred - actual)
            fn += len(actual - pred)
            misses.update(actual - pred)
            n += 1
        res = {"projects": n, "precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3) if tp + fn else None,
               "hit": tp, "wrong": fp, "missed": fn, "often_missed": [f"{s} ({k}건)" for s, k in misses.most_common(5)],
               "confirmed": len(self.active()), "candidates": sum(1 for r in self.data["rules"].values() if r.get("status") == "candidate"),
               "rejected": len(rejected)}
        self.data["history"].append({"round": self.data["rounds"], "time": time.time(), "event": "evaluate",
                                     **{k: res[k] for k in ("projects", "precision", "recall", "confirmed", "rejected")}})
        self.save()
        return res

    def markdown(self, include_candidates: bool = True) -> str:
        """스킬이 읽을 규칙 문서 (확정 규칙 + 선택적으로 후보)."""
        d = self.data
        lines = [f"# 제품 개발 규칙: {self.name}", "",
                 f"> 과거 개발 건 {(d.get('stats') or {}).get('projects', 0)}건과 BOM 에서 뽑아 담당자가 확인한 규칙입니다 (학습 {d['rounds']}회차). "
                 "확정 규칙은 따르고, 후보는 참고만 하되 적용 전에 사용자에게 확인하세요.", ""]
        if d.get("pattern"):
            lines += [f"모델명 규칙: `{d['pattern']}`", ""]
        for kind, title in (("change", "바뀌는 부품"), ("catalog", "개발 현황 (툴·부품별)"), ("base", "베이스 선택")):
            for status, head in ((("confirmed", "edited"), "확정"), (("candidate",), "후보 (확인 전)")):
                if status == ("candidate",) and not include_candidates:
                    continue
                rs = [r for r in d["rules"].values() if r["kind"] == kind and r.get("status") in status]
                if not rs:
                    continue
                lines.append(f"## {title} - {head}")
                for r in sorted(rs, key=lambda r: -r["support"]):
                    lines.append(f"- {r.get('text_override') or r['text']} ({r['support']}/{r['total']}건)"
                                 + (f" - 담당자 메모: {r['note']}" if r.get("note") else ""))
                lines.append("")
        rej = [r for r in d["rules"].values() if r.get("status") == "rejected"]
        if rej:
            lines.append("## 틀린 것으로 확인된 추정 (쓰지 말 것)")
            lines += [f"- {r['text']}" + (f" - 이유: {r['note']}" if r.get("note") else "") for r in rej]
            lines.append("")
        return "\n".join(lines)


def format_rule(r: dict[str, Any]) -> str:
    s = f"[{r['id']}] {STATUS_KO.get(r.get('status', ''), r.get('status'))} · {r.get('text_override') or r['text']} ({r['support']}/{r['total']}건)"
    if r.get("exceptions"):
        s += "\n    예외: " + ", ".join(r["exceptions"])
    if r.get("evidence"):
        s += "\n    근거: " + " / ".join(r["evidence"][:3])
    if r.get("note"):
        s += f"\n    담당자 메모: {r['note']}"
    if r.get("changed_since_answer"):
        s += "\n    ※ 답한 뒤 근거 건수가 달라졌습니다 - 다시 확인할 만합니다"
    if r.get("stale"):
        s += "\n    ※ 지금 자료에서는 근거를 찾지 못했습니다"
    return s


def make_product_rule_tools(ws: Any, rules_path: Path, skills: Any = None, can_edit: Any = None) -> list[Any]:
    import asyncio

    from .mcp_hub import BuiltinTool, ToolOutcome
    from .users import current_user

    exts = (".xlsx", ".xlsm", ".xls", ".csv")

    def paths_of(raw: Any) -> list[Path]:
        if isinstance(raw, str):
            raw = [x.strip() for x in re.split(r"[\n,]", raw) if x.strip()]
        out: list[Path] = []
        for x in raw or []:
            p = ws.resolve(str(x))
            out += sorted(q for q in p.rglob("*") if q.suffix.lower() in exts) if p.is_dir() else [p]
        return out

    def guard(fn: Any) -> Any:
        async def wrapped(args: dict[str, Any]) -> ToolOutcome:
            try:
                return await fn(args)
            except (ProductRuleError, OSError, KeyError) as e:
                return ToolOutcome(f"오류: {e}", True)
        return wrapped

    async def learn(args: dict[str, Any]) -> ToolOutcome:
        book = ProductRuleBook(rules_path, str(args.get("name") or ""))
        res = await asyncio.to_thread(book.learn, paths_of(args.get("projects")), paths_of(args.get("boms")), args.get("pattern"),
                                      args.get("group_by"), args.get("cols"), current_user.get() or "")
        by = Counter(r["kind"] for r in book.data["rules"].values())
        lines = [f"## '{book.name}' 학습 {res['round']}회차",
                 f"- 개발 건 {res['projects']}건 (베이스·개발 BOM 이 모두 있는 건 {res['with_bom']}), BOM 있는 모델 {res['models_with_bom']}개",
                 f"- 규칙 {res['rules']}개: 바뀌는 부품 {by.get('change', 0)}, 개발 현황 {by.get('catalog', 0)}, 베이스 선택 {by.get('base', 0)}"
                 f" (새로 찾음 {len(res['added'])}, 사라짐 {len(res['removed'])})"]
        if res["missing_bom"]:
            lines.append(f"- BOM 이 없는 모델: {', '.join(res['missing_bom'])} → BOM 을 더 주면 규칙이 정확해집니다")
        if not book.data.get("pattern"):
            lines.append("- 모델명 규칙(pattern)이 없어 '이름.서픽스' 로만 나눴습니다. 색상 같은 속성을 보려면 pattern 을 주세요.")
        q = book.review(3)
        if q:
            lines.append("\n### 담당자에게 확인할 것 (product_rules_answer 로 답을 기록)")
            lines += [f"- [{r['id']}] {r['question']}" for r in q]
        return ToolOutcome("\n".join(lines), False, [], res)

    async def show(args: dict[str, Any]) -> ToolOutcome:
        book = ProductRuleBook(rules_path, str(args.get("name") or "_"))
        if not book.path.is_file():
            names = sorted(p.stem for p in (Path(rules_path) / "_product_rules").glob("*.json")) if (Path(rules_path) / "_product_rules").is_dir() else []
            return ToolOutcome("제품 규칙집: " + (", ".join(names) or "(아직 없음 - product_rules_learn 으로 만드세요)"), False, [], {"names": names})
        view = str(args.get("view") or "rules")
        if view == "review":
            q = book.review(int(args.get("limit") or 5))
            text = ("### 담당자에게 확인할 것\n" + "\n".join(f"- [{r['id']}] {r['question']}\n    근거: {' / '.join(r['evidence'][:2])}" for r in q)
                    if q else "확인할 후보가 없습니다 (모두 답했습니다).")
            return ToolOutcome(text, False, [], {"questions": q})
        if view == "history":
            return ToolOutcome("\n".join(json.dumps(h, ensure_ascii=False) for h in book.data["history"][-30:]), False, [], {"history": book.data["history"]})
        kind, status = args.get("kind"), args.get("status")
        rs = [r for r in book.data["rules"].values() if (not kind or r["kind"] == kind) and (not status or r.get("status") == status)]
        rs.sort(key=lambda r: (r["kind"], -r["support"]))
        s = book.data.get("stats") or {}
        head = f"## '{book.name}' 규칙 {len(rs)}개 (학습 {book.data['rounds']}회차, 개발 건 {s.get('projects', 0)}건)"
        return ToolOutcome(head + "\n" + "\n".join(format_rule(r) for r in rs[:60]), False, [], {"rules": rs})

    async def answer(args: dict[str, Any]) -> ToolOutcome:
        book = ProductRuleBook(rules_path, str(args.get("name") or ""))
        r = await asyncio.to_thread(book.answer, str(args.get("rule_id") or ""), str(args.get("verdict") or ""), str(args.get("note") or ""),
                                    str(args.get("text") or ""), current_user.get() or "")
        left = len(book.review(100))
        return ToolOutcome(f"기록했습니다: {format_rule(r)}\n남은 확인 {left}개", False, [], r)

    async def check(args: dict[str, Any]) -> ToolOutcome:
        book = ProductRuleBook(rules_path, str(args.get("name") or ""))
        base, dev = str(args.get("base") or ""), str(args.get("dev") or "")
        res = book.predict(base, dev, args.get("include_candidates", True) is not False, tool=args.get("tool"))
        lines = [f"## {base} → {dev}: 바뀌는 속성 {', '.join(res['changed_attrs']) or '없음'}" + (f" (툴 {res['tool']})" if res.get("tool") else "")]
        lines += [f"- '{s['slot']}' 변경 ({s['because']})" + (f" → 품번 {s['part']}" if s["part"] else " → 개발된 품번 없음(신규 개발 필요할 수 있음)")
                  + f" [{STATUS_KO.get(s['status'], s['status'])}]" for s in res["slots"]]
        if not res["slots"]:
            lines.append("- 규칙으로 예상되는 부품 변경이 없습니다.")
        bases = [r for r in book.active(True) if r["kind"] == "base"]
        if bases:
            lines.append("### 베이스 선택 참고\n" + "\n".join(f"- {r.get('text_override') or r['text']} ({r['support']}/{r['total']}건)" for r in bases))
        return ToolOutcome("\n".join(lines), False, [], res)

    async def evaluate(args: dict[str, Any]) -> ToolOutcome:
        book = ProductRuleBook(rules_path, str(args.get("name") or ""))
        r = await asyncio.to_thread(book.evaluate)
        pct = lambda v: "-" if v is None else f"{v * 100:.0f}%"  # noqa: E731
        runs = [h for h in book.data["history"] if h["event"] == "evaluate"]
        trend = " → ".join(f"{pct(h['precision'])}/{pct(h['recall'])}" for h in runs[-5:])
        text = (f"## '{book.name}' 채점 (과거 {r['projects']}건, 각 건을 빼고 뽑은 규칙으로 바뀔 부품 맞히기)\n"
                f"- 정밀도 {pct(r['precision'])} (맞힘 {r['hit']} / 틀리게 예측 {r['wrong']}), 재현율 {pct(r['recall'])} (놓침 {r['missed']})\n"
                f"- 규칙: 확정 {r['confirmed']}, 후보 {r['candidates']}, 틀림 {r['rejected']}\n"
                + (f"- 자주 놓친 부품: {', '.join(r['often_missed'])}\n" if r["often_missed"] else "")
                + f"- 회차별 정밀도/재현율: {trend}")
        return ToolOutcome(text, False, [], r)

    async def export_skill(args: dict[str, Any]) -> ToolOutcome:
        if can_edit is not None and not can_edit():
            return ToolOutcome("오류: 스킬을 고칠 수 있는 편집자(skills.editors)만 쓸 수 있습니다.", True)
        book = ProductRuleBook(rules_path, str(args.get("name") or ""))
        if not book.path.is_file():
            return ToolOutcome(f"오류: 제품 규칙집 '{book.name}' 이(가) 없습니다.", True)
        skill_name = str(args.get("skill") or "")
        folder = Path(skills.root) / skill_name if skills is not None else None
        if folder is None or not (folder / "SKILL.md").is_file():
            return ToolOutcome(f"오류: 스킬 '{skill_name}' 이(가) 없습니다. 먼저 스킬을 만드세요.", True)
        out = folder / "rules" / f"{book.name}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(book.markdown(args.get("include_candidates", True) is not False), encoding="utf-8")
        book.data["history"].append({"round": book.data["rounds"], "time": time.time(), "by": current_user.get() or "", "event": "export",
                                     "skill": skill_name, "confirmed": len(book.active())})
        book.save()
        return ToolOutcome(f"스킬 '{skill_name}' 에 규칙을 반영했습니다: rules/{book.name}.md (확정 {len(book.active())}개). "
                           f"스킬 지침에서 read_skill_file 로 이 파일을 읽게 하세요.", False, [{"path": str(out), "state": "complete"}])

    name = {"name": {"type": "string", "description": "제품 규칙집 이름 (예: washer_front)"}}

    def obj(props: dict[str, Any], req: list[str]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": req}

    return [
        BuiltinTool("product_rules_learn",
                    "과거 개발 건(베이스 → 개발 모델 표)과 BOM(레벨·품번·설명)에서 규칙 후보를 뽑습니다: 모델명 속성(색상 등)이 바뀌면 바뀌는 부품, "
                    "툴별·부품별 개발 현황, 베이스 선택 경향. 자료를 더 주고 다시 부르면 갱신되고, 담당자가 답한 내용은 유지됩니다.",
                    obj({**name, "projects": {"type": "array", "items": {"type": "string"}, "description": "개발 건 표 파일/폴더"},
                         "boms": {"type": "array", "items": {"type": "string"}, "description": "BOM 파일/폴더"},
                         "pattern": {"type": "string", "description": "모델명 규칙 정규식 (이름 붙은 그룹). 예: (?P<series>[A-Z]+\\d+)(?P<color>[A-Z])\\.(?P<suffix>\\w+)"},
                         "group_by": {"type": "string", "description": "개발 현황을 묶을 기준: tool(개발 건 표의 툴 열, 기본) 또는 모델명 속성 이름"},
                         "cols": {"type": "object", "description": "열 이름을 못 찾을 때: {base, dev, tool, model, level, part, desc: 실제 열 이름}"}},
                        ["name"]), guard(learn)),
        BuiltinTool("product_rules_show", "제품 규칙을 봅니다. view: rules(기본, kind·status 로 거름) | review(담당자에게 물을 질문) | history(회차별 기록). name 생략 시 목록.",
                    obj({**name, "view": {"type": "string", "enum": ["rules", "review", "history"]}, "kind": {"type": "string", "enum": ["change", "catalog", "base"]},
                         "status": {"type": "string", "enum": ["candidate", "confirmed", "edited", "rejected"]}, "limit": {"type": "integer"}}, []),
                    guard(show)),
        BuiltinTool("product_rules_answer", "담당자의 답을 규칙에 기록합니다 (다시 학습해도 유지). verdict: confirmed(맞음) | rejected(틀림) | edited(수정 - text 에 고친 문장). "
                                            "note 에 이유·조건을 적으면 스킬 문서에 함께 들어갑니다.",
                    obj({**name, "rule_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["confirmed", "rejected", "edited"]},
                         "note": {"type": "string"}, "text": {"type": "string"}}, ["name", "rule_id", "verdict"]), guard(answer)),
        BuiltinTool("product_rules_check", "베이스 → 개발 모델일 때 규칙으로 예상되는 부품 변경과 쓸 품번, 베이스 선택 참고를 보여줍니다.",
                    obj({**name, "base": {"type": "string"}, "dev": {"type": "string"}, "tool": {"type": "string", "description": "툴 (생략하면 베이스의 툴)"},
                         "include_candidates": {"type": "boolean"}}, ["name", "base", "dev"]),
                    guard(check)),
        BuiltinTool("product_rules_evaluate", "과거 개발 건으로 규칙을 채점합니다 (각 건을 빼고 뽑은 규칙으로 바뀔 부품을 맞히는지: 정밀도·재현율, 회차별 추이).",
                    obj(name, ["name"]), guard(evaluate)),
        BuiltinTool("product_rules_export_skill", "확정된 제품 규칙을 스킬 폴더의 rules/<이름>.md 로 반영합니다 (편집자만). 스킬이 이 파일을 읽어 규칙을 따릅니다.",
                    obj({**name, "skill": {"type": "string", "description": "반영할 스킬 이름"}, "include_candidates": {"type": "boolean"}}, ["name", "skill"]),
                    guard(export_skill)),
    ]
