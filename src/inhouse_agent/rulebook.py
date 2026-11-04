"""규칙집(rulebook): 과거 자료에서 '장별 구성'과 '규칙'을 배우고, 새 자료를 검증하며, 결과로 규칙을 고쳐 나감.

흐름
  learn     : 과거 자료(좋은 예시) → 장 구조 추출 → 장별 구성(blueprint) 도출 → 규칙 후보 자동 발굴 → 지지율 계산
  add/update: LLM(또는 사람)이 근거를 보고 규칙 추가·수정 → 즉시 과거 자료로 지지율 재계산
  validate  : 새 자료(제품 분석 보고서 등)를 규칙으로 검증 → 장별 체크리스트·점수 / 판단형 규칙은 LLM 이 판정(judge)
  reflect   : 검증 이력·지지율을 보고 '이상한 규칙'(좋은 예시도 어기는 규칙, 근거 부족, 장별 구성과 안 맞는 규칙)을 찾아 제안
  승인된 결과물을 다시 좋은 예시로 learn → 지지율 변화에 따라 상태(후보/활성/재검토) 자동 변경. 모든 변경은 버전·변경기록으로 남음.

규칙 유형 (판단형 judgment 외에는 기계적으로 검사)
  section_required  {patterns}                         해당 장이 있어야 함
  section_order     {before, after}                    before 장이 after 장보다 앞
  section_element   {patterns, element, min}           장에 표/차트/이미지/노트가 있어야 함
  section_keywords  {patterns?, any?, all?}            장(없으면 전체)에 키워드
  table_columns     {patterns?, columns, min_match?}   (장의) 표에 열이 있어야 함
  section_length    {patterns, min_words?, max_words?} 장 분량
  slide_count       {min?, max?}                       전체 장 수
  text_regex        {pattern, patterns?}               정규식 (예: 출처 표기)
  excel_sheet       {patterns}                         엑셀 시트가 있어야 함
  excel_columns     {sheet?, columns, min_match?}      엑셀 열
  excel_values      {sheet?, column, not_empty?, min?, max?}  엑셀 값
  judgment          {question}                         LLM 이 판단 (예: "시사점이 데이터 근거로 도출되었는가")
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .docstruct import doc_structure

RULE_TYPES = {
    "section_required", "section_order", "section_element", "section_keywords", "table_columns", "section_length",
    "slide_count", "text_regex", "excel_sheet", "excel_columns", "excel_values", "judgment",
}
STATUSES = ("candidate", "active", "needs_review", "deprecated")
SEVERITY_W = {"must": 3, "should": 2, "nice": 1}
NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$", re.UNICODE)
_NUM_PREFIX = re.compile(r"^\s*(?:[0-9]+(?:\.[0-9]+)*|[IVXⅠ-Ⅻ]+|[가-하]|[①-⑳])\s*[.)\-:·]?\s+")
_STOP = {"및", "의", "등", "대한", "통한", "위한", "관련", "현황", "분석", "the", "and", "of", "for"}


class RulebookError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 텍스트 유사도 (한국어 형태소 분석 없이: 공백 제거 문자 bigram Dice)
# ---------------------------------------------------------------------------


def norm(t: str) -> str:
    t = _NUM_PREFIX.sub("", str(t or ""))
    t = re.sub(r"[\[\]()<>【】「」『』\"'“”‘’·:;,!?~/\\|_=+*#&%^-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _bigrams(s: str) -> set[str]:
    s = s.replace(" ", "")
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


_FILLER = {"현황", "분석", "및", "의", "등", "관련", "대한", "검토", "정리"}


def _strip_filler(t: str) -> str:
    words = [w for w in norm(t).split() if w not in _FILLER]
    return " ".join(words) or norm(t)


def _dice(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return 2 * len(A & B) / (len(A) + len(B))


def sim(a: str, b: str) -> float:
    """제목 유사도: 원문 비교와 '현황/분석' 같은 흔한 말을 뺀 비교 중 높은 값."""
    return max(_dice(norm(a), norm(b)), _dice(_strip_filler(a), _strip_filler(b)))


def title_match(title: str, patterns: list[str], thr: float) -> float:
    """제목이 패턴과 얼마나 맞는지 (포함되면 1.0)."""
    nt = norm(title).replace(" ", "")
    best = 0.0
    for p in patterns:
        npat = norm(p).replace(" ", "")
        if not npat:
            continue
        if npat in nt or (len(nt) >= 2 and nt in npat and len(nt) >= 0.6 * len(npat)):
            return 1.0
        best = max(best, sim(title, p))
    return best if best >= thr else 0.0


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9+.#-]{1,}", str(text).lower()) if w not in _STOP and len(w) >= 2}


def col_match(col: str, wanted: str) -> bool:
    a, b = norm(col).replace(" ", ""), norm(wanted).replace(" ", "")
    return bool(a and b) and (a == b or b in a or a in b or sim(a, b) >= 0.6)


# ---------------------------------------------------------------------------
# 규칙 검사
# ---------------------------------------------------------------------------


def _find_sections(doc: dict[str, Any], patterns: list[str], thr: float) -> list[dict[str, Any]]:
    scored = [(title_match(s["title"], patterns, thr), s) for s in doc["sections"]]
    return [s for sc, s in sorted(scored, key=lambda x: -x[0]) if sc > 0]


def _scope(doc: dict[str, Any], params: dict[str, Any], thr: float) -> tuple[list[dict[str, Any]] | None, str]:
    pats = params.get("patterns") or []
    if not pats:
        return doc["sections"], "문서 전체"
    secs = _find_sections(doc, pats, thr)
    return (secs or None), f"'{pats[0]}' 장"


def _sec_text(s: dict[str, Any]) -> str:
    parts = [s["title"], *s["texts"], s.get("notes") or ""]
    for t in s["tables"]:
        parts += [" ".join(map(str, t["columns"]))] + [" ".join(map(str, r)) for r in t.get("sample") or []]
    for c in s["charts"]:
        parts += [c.get("title") or "", " ".join(map(str, c.get("series") or []))]
    return "\n".join(parts)


def check_rule(rule: dict[str, Any], doc: dict[str, Any], thr: float = 0.5) -> dict[str, Any]:
    """→ {"result": pass|fail|na|judge, "detail", "sections": [번호]}"""
    t, p = rule["type"], rule.get("params") or {}
    R = lambda r, d, secs=(): {"result": r, "detail": d, "sections": [s["no"] for s in secs]}  # noqa: E731
    if t == "judgment":
        return R("judge", p.get("question") or rule.get("statement", ""))
    if t == "slide_count":
        n = len(doc["sections"])
        ok = (p.get("min") is None or n >= p["min"]) and (p.get("max") is None or n <= p["max"])
        return R("pass" if ok else "fail", f"{n}장 (기준 {p.get('min', '-')}~{p.get('max', '-')})")
    if t == "section_required":
        secs = _find_sections(doc, p.get("patterns") or [], thr)
        return R("pass", f"#{secs[0]['no']} '{secs[0]['title']}'", secs[:1]) if secs else R("fail", f"'{(p.get('patterns') or ['?'])[0]}' 에 해당하는 장이 없음")
    if t == "section_order":
        a = _find_sections(doc, p.get("before") or [], thr)
        b = _find_sections(doc, p.get("after") or [], thr)
        if not a or not b:
            return R("na", "비교할 장 중 하나가 없음")
        ok = min(s["no"] for s in a[:1]) < min(s["no"] for s in b[:1])
        return R("pass" if ok else "fail", f"#{a[0]['no']} '{a[0]['title']}' → #{b[0]['no']} '{b[0]['title']}'", [a[0], b[0]])
    if t in ("section_element", "section_keywords", "table_columns", "section_length", "text_regex"):
        secs, where = _scope(doc, p, thr)
        if secs is None:
            return R("na", f"{where} 이(가) 없어 검사 생략")
        if t == "section_element":
            el, need = p.get("element", "table"), int(p.get("min") or 1)
            cnt = {"table": lambda s: len(s["tables"]), "chart": lambda s: len(s["charts"]), "image": lambda s: s["images"],
                   "notes": lambda s: 1 if s.get("notes") else 0, "visual": lambda s: len(s["tables"]) + len(s["charts"]) + s["images"]}.get(el)
            if cnt is None:
                return R("na", f"알 수 없는 요소: {el}")
            best = max(secs, key=cnt)
            got = cnt(best)
            return R("pass" if got >= need else "fail", f"{where} #{best['no']}: {el} {got}개 (필요 {need})", [best])
        if t == "section_keywords":
            text = "\n".join(_sec_text(s) for s in secs).lower()
            anyk, allk = [k.lower() for k in p.get("any") or []], [k.lower() for k in p.get("all") or []]
            miss = [k for k in allk if k not in text]
            hit = [k for k in anyk if k in text]
            ok = not miss and (not anyk or bool(hit))
            d = f"{where}: " + (f"있음({', '.join(hit or allk)})" if ok else f"없음 - {'필수 ' + ', '.join(miss) if miss else ''}{' 중 하나: ' + ', '.join(anyk) if anyk and not hit else ''}")
            return R("pass" if ok else "fail", d, secs[:1])
        if t == "table_columns":
            want = p.get("columns") or []
            need = int(p.get("min_match") or len(want))
            best_hit, best_tbl, best_sec = [], None, None
            for s in secs:
                for tb in s["tables"]:
                    hit = [w for w in want if any(col_match(str(c), w) for c in tb["columns"])]
                    if len(hit) > len(best_hit) or best_tbl is None:
                        best_hit, best_tbl, best_sec = hit, tb, s
            if best_tbl is None:
                return R("fail", f"{where}에 표가 없음")
            ok = len(best_hit) >= need
            miss = [w for w in want if w not in best_hit]
            return R("pass" if ok else "fail", f"{where} #{best_sec['no']} 표 열 {len(best_hit)}/{len(want)} 일치" + (f" (빠짐: {', '.join(miss)})" if miss else ""), [best_sec])
        if t == "section_length":
            w = sum(s["words"] for s in secs[:1])
            ok = (p.get("min_words") is None or w >= p["min_words"]) and (p.get("max_words") is None or w <= p["max_words"])
            return R("pass" if ok else "fail", f"{where} 본문 {w}단어 (기준 {p.get('min_words', '-')}~{p.get('max_words', '-')})", secs[:1])
        if t == "text_regex":
            try:
                rx = re.compile(p.get("pattern") or "", re.I)
            except re.error as e:
                return R("na", f"정규식 오류: {e}")
            hit = [s for s in secs if rx.search(_sec_text(s))]
            return R("pass" if hit else "fail", f"{where}: " + (f"#{hit[0]['no']} 에서 발견" if hit else f"'{p.get('pattern')}' 없음"), hit[:1])
    if t.startswith("excel_"):
        sheets = doc.get("sheets") or []
        if not sheets:
            return R("na", "엑셀 문서가 아님")
        pats = p.get("patterns") or ([p["sheet"]] if p.get("sheet") else [])
        cand = [s for s in sheets if title_match(s["name"], pats, thr)] if pats else sheets
        if t == "excel_sheet":
            return R("pass" if cand else "fail", f"시트 '{cand[0]['name']}'" if cand else f"'{pats[0] if pats else '?'}' 시트 없음")
        if not cand:
            return R("na" if pats else "fail", "대상 시트 없음")
        if t == "excel_columns":
            want = p.get("columns") or []
            need = int(p.get("min_match") or len(want))
            best = max(cand, key=lambda s: sum(any(col_match(str(c), w) for c in s["columns"]) for w in want))
            hit = [w for w in want if any(col_match(str(c), w) for c in best["columns"])]
            miss = [w for w in want if w not in hit]
            return R("pass" if len(hit) >= need else "fail", f"시트 '{best['name']}' 열 {len(hit)}/{len(want)}" + (f" (빠짐: {', '.join(miss)})" if miss else ""))
        if t == "excel_values":
            col = p.get("column", "")
            for s in cand:
                cols = [c for c in s["columns"] if col_match(str(c), col)]
                if not cols:
                    continue
                c = cols[0]
                issues = []
                if p.get("not_empty") and s["empty_ratio"].get(c, 0) > 0:
                    issues.append(f"빈 칸 {s['empty_ratio'][c]:.0%}")
                if p.get("min") is not None or p.get("max") is not None:
                    from . import analyzers

                    idx = s["columns"].index(c)
                    vals = [analyzers.to_number(r[idx]) for r in s.get("all_rows") or s.get("sample") or [] if idx < len(r)]
                    vals = [v for v in vals if v is not None]
                    bad = [v for v in vals if (p.get("min") is not None and v < p["min"]) or (p.get("max") is not None and v > p["max"])]
                    if bad:
                        issues.append(f"범위 밖 값 {len(bad)}개 (예: {bad[0]})")
                return R("fail" if issues else "pass", f"시트 '{s['name']}' 열 '{c}': " + (", ".join(issues) if issues else "정상"))
            return R("fail", f"열 '{col}' 이(가) 없음")
    return R("na", f"알 수 없는 규칙 유형: {t}")


# ---------------------------------------------------------------------------
# 장별 구성(blueprint) 도출
# ---------------------------------------------------------------------------


def _common_part(titles: list[str]) -> str:
    """여러 제목의 공통 부분 (예: '뮤즈 기술 분석 보고서', 'AI 비서 기술 분석 보고서' → '기술 분석 보고서')."""
    toks = [t.split() for t in titles if t.strip()]
    if len(toks) < 2:
        return titles[0] if titles else ""
    best: list[str] = []
    first = toks[0]
    for i in range(len(first)):
        for j in range(i + 1, len(first) + 1):
            cand = first[i:j]
            if len(cand) > len(best) and all(" ".join(cand) in " ".join(t) for t in toks[1:]):
                best = cand
    common = " ".join(best)
    return common if len(common.replace(" ", "")) >= 2 else ""


def is_deck(doc: dict[str, Any]) -> bool:
    return doc.get("kind") not in ("xlsx", "csv")


def build_blueprint(docs: list[dict[str, Any]], thr: float = 0.5) -> list[dict[str, Any]]:
    """좋은 예시 문서(PPT/Word/PDF)들의 장 제목을 묶어서 '보통 이런 장이 이 순서로, 이런 내용으로 들어간다' 를 만듦."""
    docs = [d for d in docs if is_deck(d)]
    clusters: list[dict[str, Any]] = []
    total = len(docs)
    global_tok: Counter = Counter()
    for d in docs:
        for s in d["sections"]:
            global_tok.update(tokens(_sec_text(s)))
    n_secs = max(1, sum(len(d["sections"]) for d in docs))
    for di, d in enumerate(docs):
        n = len(d["sections"])
        for s in d["sections"]:
            nt = norm(s["title"])
            if not nt:
                continue
            best, best_sc = None, 0.0
            for c in clusters:
                sc = max((title_match(s["title"], [t], thr) for t, _ in c["titles"].most_common(6)), default=0.0)
                if sc > best_sc:
                    best, best_sc = c, sc
            if best is None:
                best = {"titles": Counter(), "orig": Counter(), "docs": set(), "pos": [], "el": defaultdict(set), "cols": defaultdict(set),
                        "tok": defaultdict(set), "words": [], "examples": []}
                clusters.append(best)
            best["titles"][nt] += 1
            best["orig"][_NUM_PREFIX.sub("", s["title"]).strip()] += 1
            best["docs"].add(di)
            best["pos"].append((s["no"] - 1) / max(1, n - 1))
            best["words"].append(s["words"])
            for el, ok in (("table", s["tables"]), ("chart", s["charts"]), ("image", s["images"]), ("notes", s.get("notes"))):
                if ok:
                    best["el"][el].add(di)
            for tb in s["tables"]:
                for c in tb["columns"]:
                    if norm(str(c)):
                        best["cols"][str(c).strip()].add(di)
            for tk in tokens(" ".join(s["texts"])):
                best["tok"][tk].add(di)
            if len(best["examples"]) < 3:
                best["examples"].append({"doc": d["name"], "no": s["no"], "title": s["title"], "text": " / ".join(s["texts"])[:150]})
    out = []
    for c in clusters:
        k = len(c["docs"])
        docs_tbl = max(1, len(c["el"].get("table", set())))
        key_tokens = [t for t, ds in sorted(c["tok"].items(), key=lambda x: (-len(x[1]), x[0]))  # 동률은 가나다순 (매번 같은 결과)
                      if len(ds) / k >= 0.6 and global_tok[t] / n_secs < 0.5][:8] if k >= 2 else []
        common = _common_part(list(c["orig"]))
        pats = [t for t, _ in c["titles"].most_common(5)]
        if common and len(c["orig"]) >= 2 and norm(common) not in pats:
            pats = [norm(common)] + pats[:4]  # 제품명 등 바뀌는 부분을 뺀 공통 제목을 대표 패턴으로
        out.append({
            "name": (common if common and len(c["orig"]) >= 2 else c["orig"].most_common(1)[0][0]),
            "patterns": pats,
            "support": round(k / total, 3) if total else 0,
            "docs": k,
            "position": round(sum(c["pos"]) / len(c["pos"]), 3),
            "elements": {el: round(len(ds) / k, 2) for el, ds in c["el"].items()},
            "columns": [col for col, ds in sorted(c["cols"].items(), key=lambda x: -len(x[1])) if len(ds) / docs_tbl >= 0.6][:10],  # 동률은 표의 열 순서 유지
            "keywords": key_tokens,
            "avg_words": round(sum(c["words"]) / len(c["words"])),
            "examples": c["examples"],
        })
    out.sort(key=lambda x: (x["position"], -x["support"]))
    return out


def mine_candidates(blueprint: list[dict[str, Any]], docs: list[dict[str, Any]], min_support: float = 0.6) -> list[dict[str, Any]]:
    """장별 구성에서 기계적으로 검사 가능한 규칙 후보를 뽑음 (LLM/사람이 검토 후 활성화)."""
    cands: list[dict[str, Any]] = []
    core = [b for b in blueprint if b["support"] >= min_support and b["docs"] >= 2]
    for b in core:
        pats = b["patterns"]
        cands.append({"type": "section_required", "params": {"patterns": pats}, "severity": "must" if b["support"] >= 0.9 else "should",
                      "statement": f"'{b['name']}' 장이 있어야 한다", "rationale": f"좋은 예시 {b['docs']}건 중 {b['support']:.0%} 에 있음"})
        for el, ratio in b["elements"].items():
            if ratio >= 0.7 and el in ("table", "chart", "image"):
                label = {"table": "표", "chart": "차트", "image": "이미지"}[el]
                cands.append({"type": "section_element", "params": {"patterns": pats, "element": el, "min": 1}, "severity": "should",
                              "statement": f"'{b['name']}' 장에는 {label}가 있어야 한다", "rationale": f"이 장이 있는 자료의 {ratio:.0%} 가 {label} 사용"})
        if len(b["columns"]) >= 2:
            cols = b["columns"][:6]
            cands.append({"type": "table_columns", "params": {"patterns": pats, "columns": cols, "min_match": max(2, math.ceil(len(cols) * 0.7))},
                          "severity": "should", "statement": f"'{b['name']}' 표에는 {', '.join(cols)} 열이 있어야 한다", "rationale": "과거 표에서 공통으로 쓴 열"})
        if b["keywords"]:
            cands.append({"type": "section_keywords", "params": {"patterns": pats, "any": b["keywords"][:3]}, "severity": "nice",
                          "statement": f"'{b['name']}' 장에서 {', '.join(b['keywords'][:3])} 중 하나를 다뤄야 한다", "rationale": "과거 이 장에서 반복된 핵심어"})
    for a, b in zip(core, core[1:]):
        cands.append({"type": "section_order", "params": {"before": a["patterns"], "after": b["patterns"]}, "severity": "nice",
                      "statement": f"'{a['name']}' 장이 '{b['name']}' 장보다 앞에 와야 한다", "rationale": "과거 자료의 평균 순서"})
    ns = [len(d["sections"]) for d in docs if d["sections"] and is_deck(d)]
    if len(ns) >= 2:
        cands.append({"type": "slide_count", "params": {"min": max(1, int(min(ns) * 0.8)), "max": int(math.ceil(max(ns) * 1.25))}, "severity": "nice",
                      "statement": f"전체 분량은 {max(1, int(min(ns) * 0.8))}~{int(math.ceil(max(ns) * 1.25))}장", "rationale": f"과거 자료 {min(ns)}~{max(ns)}장"})
    sheets = [s for d in docs for s in d.get("sheets") or []]
    if sheets:
        by: dict[str, list] = defaultdict(list)
        for d in docs:
            for s in d.get("sheets") or []:
                by[norm(s["name"])].append((d["name"], s))
        n_x = sum(1 for d in docs if d.get("sheets"))
        for key, items in by.items():
            if len({x[0] for x in items}) / max(1, n_x) >= min_support and n_x >= 2:
                cols = Counter(str(c) for _, s in items for c in s["columns"])
                common = [c for c, k in cols.most_common(8) if k / len(items) >= 0.7]
                name = items[0][1]["name"]
                cands.append({"type": "excel_sheet", "params": {"patterns": [key]}, "severity": "should", "statement": f"엑셀에 '{name}' 시트가 있어야 한다", "rationale": f"엑셀 자료 {n_x}건 중 {len(items)}건"})
                if len(common) >= 2:
                    cands.append({"type": "excel_columns", "params": {"sheet": key, "columns": common, "min_match": max(2, math.ceil(len(common) * 0.8))}, "severity": "should",
                                  "statement": f"'{name}' 시트에는 {', '.join(common)} 열이 있어야 한다", "rationale": "과거 엑셀 공통 열"})
    return cands


def _pats(p: dict[str, Any], key: str = "patterns") -> set[str]:
    v = p.get(key) or ([p["sheet"]] if key == "patterns" and p.get("sheet") else [])
    return {norm(x) for x in v if norm(x)}


def same_rule(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """같은 내용의 규칙인지 (장 제목 패턴이 겹치고 핵심 조건이 같으면 같은 규칙으로 봄)."""
    if a["type"] != b["type"]:
        return False
    pa, pb = a.get("params") or {}, b.get("params") or {}
    t = a["type"]
    if t == "slide_count":
        return True
    if t == "judgment":
        return norm(pa.get("question", "")) == norm(pb.get("question", ""))
    if t == "text_regex":
        return pa.get("pattern") == pb.get("pattern") and (_pats(pa) == _pats(pb) or bool(_pats(pa) & _pats(pb)))
    if t == "section_order":
        return bool(_pats(pa, "before") & _pats(pb, "before")) and bool(_pats(pa, "after") & _pats(pb, "after"))
    A, B = _pats(pa), _pats(pb)
    scope_same = (not A and not B) or bool(A & B)
    if not scope_same:
        return False
    if t == "section_element":
        return pa.get("element") == pb.get("element")
    if t in ("table_columns", "excel_columns"):
        ca, cb = {norm(x) for x in pa.get("columns") or []}, {norm(x) for x in pb.get("columns") or []}
        return bool(ca and cb) and len(ca & cb) / min(len(ca), len(cb)) >= 0.5
    if t == "section_keywords":
        ka = {norm(x) for x in (pa.get("any") or []) + (pa.get("all") or [])}
        kb = {norm(x) for x in (pb.get("any") or []) + (pb.get("all") or [])}
        return bool(ka & kb)
    if t == "excel_values":
        return norm(pa.get("column", "")) == norm(pb.get("column", ""))
    return True  # section_required, section_length, excel_sheet


# ---------------------------------------------------------------------------
# 저장소
# ---------------------------------------------------------------------------

_LOCKS: dict[str, threading.RLock] = defaultdict(threading.RLock)


class Rulebook:
    DEFAULT_SETTINGS = {"match_threshold": 0.5, "promote": 0.75, "demote": 0.5, "min_docs": 2, "candidate_support": 0.6}

    def __init__(self, root: Path, name: str):
        if not NAME_RE.match(name):
            raise RulebookError("규칙집 이름은 한글/영문/숫자와 . _ - 만 쓸 수 있습니다.")
        self.name = name
        self.dir = root / name
        self.lock = _LOCKS[str(self.dir.resolve())]

    # ---------------- 파일 ----------------
    @property
    def file(self) -> Path:
        return self.dir / "rulebook.json"

    def exists(self) -> bool:
        return self.file.is_file()

    def load(self) -> dict[str, Any]:
        if not self.exists():
            raise RulebookError(f"규칙집 '{self.name}' 이(가) 없습니다. rulebook_create 로 먼저 만드세요.")
        d = json.loads(self.file.read_text(encoding="utf-8"))
        d.setdefault("settings", {})
        for k, v in self.DEFAULT_SETTINGS.items():
            d["settings"].setdefault(k, v)
        return d

    def save(self, d: dict[str, Any], *, backup: bool = True) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if backup and self.file.exists():
            h = self.dir / ".history"
            h.mkdir(exist_ok=True)
            shutil.copy2(self.file, h / f"rulebook_v{d.get('version', 0)}_{datetime.now():%Y%m%d_%H%M%S_%f}.json")
            for old in sorted(h.glob("rulebook_*.json"))[:-50]:
                old.unlink(missing_ok=True)
        d["version"] = int(d.get("version", 0)) + 1
        d["updated"] = time.time()
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.file)

    def log(self, action: str, user: str | None, **kw: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "changelog.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.time(), "action": action, "user": user, **kw}, ensure_ascii=False, default=str) + "\n")

    def changelog(self, limit: int = 100) -> list[dict[str, Any]]:
        p = self.dir / "changelog.jsonl"
        if not p.is_file():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(x) for x in reversed(lines) if x.strip()]

    def corpus_doc(self, doc_id: str) -> dict[str, Any]:
        return json.loads((self.dir / "corpus" / f"{doc_id}.json").read_text(encoding="utf-8"))

    def _exemplars(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for c in d.get("corpus", []):
            if c.get("label") == "exemplar":
                try:
                    out.append(self.corpus_doc(c["id"]))
                except OSError:
                    continue
        return out

    # ---------------- 생성 ----------------
    def create(self, description: str, domain: str = "", user: str | None = None) -> dict[str, Any]:
        with self.lock:
            if self.exists():
                raise RulebookError(f"규칙집 '{self.name}' 이(가) 이미 있습니다.")
            d = {"name": self.name, "description": description, "domain": domain, "version": 0, "created": time.time(),
                 "created_by": user, "settings": dict(self.DEFAULT_SETTINGS), "rules": [], "blueprint": [], "corpus": []}
            self.save(d, backup=False)
            self.log("create", user, description=description)
            return d

    # ---------------- 학습 ----------------
    def learn(self, paths: list[Path], label: str = "exemplar", user: str | None = None, product: str | None = None,
              mine: bool = True) -> dict[str, Any]:
        """과거 자료를 corpus 에 추가(label: exemplar=좋은 예시, reference=참고만) → 장별 구성·후보·지지율 갱신."""
        if label not in ("exemplar", "reference"):
            raise RulebookError("label 은 exemplar(좋은 예시) 또는 reference(참고) 입니다.")
        with self.lock:
            d = self.load()
            added, updated, errors = [], [], []
            (self.dir / "corpus").mkdir(parents=True, exist_ok=True)
            for p in paths:
                try:
                    st = doc_structure(p)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{Path(p).name}: {e}")
                    continue
                digest = hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
                ex = next((c for c in d["corpus"] if c["hash"] == digest), None)
                if ex:
                    if ex["label"] != label:
                        ex["label"] = label
                        updated.append(ex["name"])
                    continue
                doc_id = uuid.uuid4().hex[:12]
                (self.dir / "corpus" / f"{doc_id}.json").write_text(json.dumps(st, ensure_ascii=False, default=str), encoding="utf-8")
                d["corpus"].append({"id": doc_id, "name": Path(p).name, "path": str(p), "hash": digest, "label": label, "kind": st["kind"],
                                    "sections": len(st["sections"]), "added": time.time(), "added_by": user, "product": product})
                added.append(Path(p).name)
            ex_docs = self._exemplars(d)
            old_bp = {b["name"] for b in d.get("blueprint", [])}
            d["blueprint"] = build_blueprint(ex_docs, d["settings"]["match_threshold"])
            new_cands = []
            if mine and ex_docs:
                for c in mine_candidates(d["blueprint"], ex_docs, d["settings"]["candidate_support"]):
                    if any(same_rule(c, r) for r in d["rules"]):  # 폐기한 규칙도 다시 제안하지 않음
                        continue
                    new_cands.append(self._new_rule(d, c, source="mined", user=user))
            changes = self._recompute(d, ex_docs, user)
            self.save(d)
            self.log("learn", user, added=added, relabeled=updated, label=label, new_candidates=[r["id"] for r in new_cands], status_changes=changes)
            return {"added": added, "relabeled": updated, "errors": errors, "exemplars": len(ex_docs), "blueprint": d["blueprint"],
                    "new_candidates": new_cands, "status_changes": changes, "new_sections": [b["name"] for b in d["blueprint"] if b["name"] not in old_bp]}

    def _new_rule(self, d: dict[str, Any], r: dict[str, Any], *, source: str, user: str | None) -> dict[str, Any]:
        t = r.get("type")
        if t not in RULE_TYPES:
            raise RulebookError(f"규칙 유형은 {', '.join(sorted(RULE_TYPES))} 중 하나입니다.")
        if t == "text_regex":
            try:
                re.compile((r.get("params") or {}).get("pattern") or "")
            except re.error as e:
                raise RulebookError(f"정규식 오류: {e}") from e
        nums = [int(x["id"][1:]) for x in d["rules"] if re.fullmatch(r"R\d+", x["id"])]
        rule = {
            "id": f"R{(max(nums) + 1) if nums else 1:03d}",
            "type": t,
            "params": r.get("params") or {},
            "statement": str(r.get("statement") or "").strip() or t,
            "rationale": str(r.get("rationale") or ""),
            "severity": r.get("severity") if r.get("severity") in SEVERITY_W else "should",
            "status": r.get("status") if r.get("status") in STATUSES else "candidate",
            "locked": bool(r.get("locked")),
            "source": source,
            "tags": list(r.get("tags") or []),
            "created": time.time(), "created_by": user, "updated": time.time(),
            "support": {}, "stats": {"validations": 0, "fails": 0, "targets_failed": 0},
            "version": 1, "history": [],
        }
        d["rules"].append(rule)
        return rule

    def _support(self, d: dict[str, Any], rule: dict[str, Any], ex_docs: list[dict[str, Any]]) -> dict[str, Any]:
        thr = d["settings"]["match_threshold"]
        if rule["type"] == "judgment":
            j = rule.get("judgments") or {}
            ex_ids = {c["id"] for c in d["corpus"] if c["label"] == "exemplar"}
            vals = [v["verdict"] for k, v in j.items() if k in ex_ids]
            p = sum(1 for v in vals if v == "pass")
            return {"pass": p, "fail": len(vals) - p, "na": len(ex_ids) - len(vals), "ratio": round(p / len(vals), 3) if vals else None, "failing_docs": []}
        excel_rule = rule["type"].startswith("excel_")
        res = [(doc, check_rule(rule, doc, thr)) for doc in ex_docs if is_deck(doc) != excel_rule]
        p = sum(1 for _, r in res if r["result"] == "pass")
        f = sum(1 for _, r in res if r["result"] == "fail")
        return {"pass": p, "fail": f, "na": len(res) - p - f, "ratio": round(p / (p + f), 3) if p + f else None,
                "failing_docs": [f"{doc['name']}: {r['detail']}" for doc, r in res if r["result"] == "fail"][:5]}

    def _recompute(self, d: dict[str, Any], ex_docs: list[dict[str, Any]] | None = None, user: str | None = None) -> list[dict[str, Any]]:
        """모든 규칙의 지지율 재계산 + (잠기지 않은 규칙) 상태 자동 변경."""
        ex_docs = self._exemplars(d) if ex_docs is None else ex_docs
        s = d["settings"]
        changes = []
        for r in d["rules"]:
            r["support"] = self._support(d, r, ex_docs)
            if r.get("locked") or r["status"] == "deprecated":
                continue
            ratio, n = r["support"].get("ratio"), r["support"].get("pass", 0) + r["support"].get("fail", 0)
            new = r["status"]
            if ratio is not None and n >= s["min_docs"]:
                if r["status"] in ("candidate", "needs_review") and ratio >= s["promote"]:
                    new = "active"
                elif r["status"] == "active" and ratio <= s["demote"]:  # 좋은 예시 절반 이상이 어기면 재검토
                    new = "needs_review"
            if new != r["status"]:
                changes.append({"rule": r["id"], "from": r["status"], "to": new, "support": ratio, "statement": r["statement"]})
                r["history"].append({"time": time.time(), "change": f"상태 {r['status']} → {new} (지지율 {ratio:.0%}, 자동)", "user": user})
                r["status"] = new
        return changes

    # ---------------- 규칙 편집 ----------------
    def add_rule(self, rule: dict[str, Any], user: str | None = None, source: str = "agent") -> dict[str, Any]:
        with self.lock:
            d = self.load()
            if rule.get("type") not in RULE_TYPES:
                raise RulebookError(f"규칙 유형은 {', '.join(sorted(RULE_TYPES))} 중 하나입니다.")
            dup = next((r for r in d["rules"] if r["status"] != "deprecated" and same_rule(rule, r)), None)
            if dup:
                raise RulebookError(f"같은 내용의 규칙이 이미 있습니다: {dup['id']} {dup['statement']} (수정은 rulebook_update_rule)")
            r = self._new_rule(d, rule, source=source, user=user)
            r["support"] = self._support(d, r, self._exemplars(d))
            changes = self._recompute(d, user=user)
            self.save(d)
            self.log("add_rule", user, rule=r["id"], statement=r["statement"], source=source)
            return {"rule": r, "status_changes": changes}

    def update_rule(self, rule_id: str, changes: dict[str, Any], reason: str, user: str | None = None) -> dict[str, Any]:
        with self.lock:
            d = self.load()
            r = next((x for x in d["rules"] if x["id"] == rule_id), None)
            if r is None:
                raise RulebookError(f"규칙 {rule_id} 이(가) 없습니다.")
            before = {k: copy.deepcopy(r.get(k)) for k in changes}
            for k, v in changes.items():
                if k not in ("params", "statement", "rationale", "severity", "status", "locked", "tags"):
                    raise RulebookError(f"바꿀 수 없는 항목: {k}")
                if k == "status" and v not in STATUSES:
                    raise RulebookError(f"상태는 {', '.join(STATUSES)} 중 하나")
                if k == "severity" and v not in SEVERITY_W:
                    raise RulebookError("중요도는 must / should / nice")
                if k == "params":
                    v = {**r["params"], **v}
                r[k] = v
            if "status" in changes and "locked" not in changes:
                r["locked"] = True  # 사람/에이전트가 직접 정한 상태는 자동 변경하지 않음
            r["version"] += 1
            r["updated"] = time.time()
            r["history"].append({"time": time.time(), "change": json.dumps({k: [before[k], r.get(k)] for k in changes}, ensure_ascii=False, default=str)[:600], "reason": reason, "user": user})
            r["support"] = self._support(d, r, self._exemplars(d))
            self.save(d)
            self.log("update_rule", user, rule=rule_id, before=before, after={k: r.get(k) for k in changes}, reason=reason)
            return r

    # ---------------- 검증 ----------------
    def validate(self, path: Path, *, label: str = "target", product: str | None = None, user: str | None = None,
                 include_candidates: bool = False) -> dict[str, Any]:
        with self.lock:
            d = self.load()
        doc = doc_structure(path)
        thr = d["settings"]["match_threshold"]
        rules = [r for r in d["rules"] if r["status"] == "active" or (include_candidates and r["status"] in ("candidate", "needs_review"))]
        deck = is_deck(doc)
        rules = [r for r in rules if r["type"].startswith("excel_") != deck or r["type"] == "judgment"]  # 문서 종류에 맞는 규칙만
        results = []
        for r in rules:
            res = check_rule(r, doc, thr)
            results.append({"rule": r["id"], "type": r["type"], "statement": r["statement"], "severity": r["severity"], "status": r["status"], **res})
        applicable = [x for x in results if x["result"] in ("pass", "fail") and x["status"] == "active"]
        w = sum(SEVERITY_W[x["severity"]] for x in applicable)
        score = round(100 * sum(SEVERITY_W[x["severity"]] for x in applicable if x["result"] == "pass") / w, 1) if w else None
        # 장별 체크리스트: 장별 구성의 각 장이 있는지, 필요한 요소/규칙을 지켰는지
        checklist = []
        for b in (_key_sections(d) if deck else []):
            secs = _find_sections(doc, b["patterns"], thr)
            need = [k for k, v in b["elements"].items() if v >= 0.6 and k in ("table", "chart", "image")]
            rel = [x for x in results if set(x["sections"]) & {s["no"] for s in secs[:1]}] if secs else []
            checklist.append({
                "section": b["name"], "found": bool(secs), "at": secs[0]["no"] if secs else None, "title": secs[0]["title"] if secs else None,
                "needs": need, "has": [k for k in need if secs and ((k == "table" and secs[0]["tables"]) or (k == "chart" and secs[0]["charts"]) or (k == "image" and secs[0]["images"]))],
                "keywords": b["keywords"][:5], "rules": [{"rule": x["rule"], "result": x["result"]} for x in rel],
            })
        extra = [s["no"] for s in doc["sections"] if deck and not any(title_match(s["title"], b["patterns"], thr) for b in d["blueprint"])]
        vid = f"V{datetime.now():%Y%m%d%H%M%S}{uuid.uuid4().hex[:4]}"
        v = {"id": vid, "time": time.time(), "user": user, "path": str(path), "name": Path(path).name, "label": label, "product": product,
             "rulebook_version": d["version"], "score": score, "results": results, "checklist": checklist, "extra_sections": extra,
             "pending_judgments": [x["rule"] for x in results if x["result"] == "judge"], "sections": len(doc["sections"])}
        with self.lock:
            vdir = self.dir / "validations"
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / f"{vid}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            d = self.load()
            by_id = {r["id"]: r for r in d["rules"]}
            for x in results:
                r = by_id.get(x["rule"])
                if r is not None and x["result"] in ("pass", "fail"):
                    r["stats"]["validations"] += 1
                    if x["result"] == "fail":
                        r["stats"]["fails"] += 1
                        if label == "target":
                            r["stats"]["targets_failed"] += 1
            self.save(d, backup=False)
            self.log("validate", user, validation=vid, file=Path(path).name, score=score, product=product)
        if label == "exemplar":  # 승인된 결과물 → 좋은 예시로 학습
            v["learned"] = self.learn([Path(path)], "exemplar", user, product, mine=False)
        return v

    def validation(self, vid: str) -> dict[str, Any]:
        if not re.fullmatch(r"V[0-9a-f]+", vid):
            raise RulebookError("잘못된 검증 ID")
        p = self.dir / "validations" / f"{vid}.json"
        if not p.is_file():
            raise RulebookError(f"검증 기록이 없습니다: {vid}")
        return json.loads(p.read_text(encoding="utf-8"))

    def validations(self, limit: int = 50) -> list[dict[str, Any]]:
        vdir = self.dir / "validations"
        if not vdir.is_dir():
            return []
        out = []
        for p in sorted(vdir.glob("V*.json"), reverse=True)[:limit]:
            v = json.loads(p.read_text(encoding="utf-8"))
            out.append({k: v.get(k) for k in ("id", "time", "user", "name", "path", "label", "product", "score", "rulebook_version", "pending_judgments")}
                       | {"fails": sum(1 for x in v["results"] if x["result"] == "fail")})
        return out

    def judge(self, vid: str, rule_id: str, verdict: str, reason: str, user: str | None = None) -> dict[str, Any]:
        """판단형 규칙(또는 기계 판정에 대한 이의)의 LLM/사람 판정 기록."""
        if verdict not in ("pass", "fail", "na"):
            raise RulebookError("verdict 는 pass / fail / na")
        with self.lock:
            v = self.validation(vid)
            item = next((x for x in v["results"] if x["rule"] == rule_id), None)
            if item is None:
                raise RulebookError(f"이 검증에 규칙 {rule_id} 가 없습니다.")
            item.update(result=verdict, detail=f"{item.get('detail', '')} → 판정: {reason}", judged_by=user)
            v["pending_judgments"] = [x for x in v.get("pending_judgments", []) if x != rule_id]
            applicable = [x for x in v["results"] if x["result"] in ("pass", "fail") and x["status"] == "active"]
            w = sum(SEVERITY_W[x["severity"]] for x in applicable)
            v["score"] = round(100 * sum(SEVERITY_W[x["severity"]] for x in applicable if x["result"] == "pass") / w, 1) if w else None
            (self.dir / "validations" / f"{vid}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            d = self.load()
            r = next((x for x in d["rules"] if x["id"] == rule_id), None)
            if r is not None and r["type"] == "judgment":
                doc = next((c for c in d["corpus"] if c["path"] == v["path"] or c["name"] == v["name"]), None)
                if doc is not None:
                    r.setdefault("judgments", {})[doc["id"]] = {"verdict": verdict, "reason": reason, "time": time.time()}
                    r["support"] = self._support(d, r, [])
            self.save(d, backup=False)
            self.log("judge", user, validation=vid, rule=rule_id, verdict=verdict, reason=reason)
            return v

    # ---------------- 계획 / 회고 ----------------
    def outline(self, product: str = "", focus: str = "") -> list[dict[str, Any]]:
        """새 제품 분석 보고서의 장별 계획: 각 장의 목적, 필요한 요소, 지켜야 할 규칙, 과거 예시."""
        d = self.load()
        thr = d["settings"]["match_threshold"]
        plan = []
        active = [r for r in d["rules"] if r["status"] == "active"]
        for b in _key_sections(d):
            rel = [r for r in active if any(title_match(t, b["patterns"], thr) or title_match(b["name"], [t], thr)
                                            for t in ((r["params"].get("patterns") or []) + (r["params"].get("before") or []) + (r["params"].get("after") or [])))]
            plan.append({
                "section": b["name"], "support": b["support"], "position": b["position"], "required_by": b["required_by"],
                "needs": [k for k, v in b["elements"].items() if v >= 0.6], "columns": b["columns"], "keywords": b["keywords"],
                "avg_words": b["avg_words"], "rules": [f"{r['id']}[{r['severity']}] {r['statement']}" for r in rel],
                "examples": b["examples"][:2],
            })
        general = [f"{r['id']}[{r['severity']}] {r['statement']}" for r in active if not r["params"].get("patterns") and r["type"] not in ("section_order",)]
        if general:
            plan.append({"section": "(문서 전체)", "rules": general})
        return plan

    def reflect(self) -> dict[str, Any]:
        """스스로 점검: 규칙과 근거(좋은 예시), 검증 이력이 어긋나는 곳을 찾아 제안."""
        d = self.load()
        sug = []
        for r in d["rules"]:
            if r["status"] == "deprecated":
                continue
            sp = r.get("support") or {}
            ratio = sp.get("ratio")
            n = (sp.get("pass") or 0) + (sp.get("fail") or 0)
            st = r["stats"]
            if r["status"] == "active" and ratio is not None and ratio < 0.75:
                sug.append({"rule": r["id"], "kind": "too_strict", "message": f"좋은 예시 {n}건 중 {sp['fail']}건이 어김({ratio:.0%}) → 조건 완화(열/키워드 줄이기, min_match 낮추기)나 중요도 하향 검토",
                            "evidence": sp.get("failing_docs")})
            if r["status"] == "needs_review":
                sug.append({"rule": r["id"], "kind": "needs_review", "message": "지지율이 떨어져 재검토 상태 → 수정하거나 폐기", "evidence": sp.get("failing_docs")})
            if r["status"] == "candidate" and n >= d["settings"]["min_docs"] and ratio is not None and ratio < d["settings"]["promote"]:
                sug.append({"rule": r["id"], "kind": "weak_candidate", "message": f"후보 지지율 {ratio:.0%} - 조건을 바꾸거나 폐기 검토"})
            if r["status"] == "active" and n == 0 and r["type"] != "judgment":
                sug.append({"rule": r["id"], "kind": "no_evidence", "message": "근거가 되는 좋은 예시에서 한 번도 적용되지 않음(해당 장 없음)"})
            if st["validations"] >= 3 and st["fails"] == 0 and r["severity"] == "nice":
                sug.append({"rule": r["id"], "kind": "always_pass", "message": "항상 통과 - 변별력이 낮음(중요도 유지 또는 더 구체적인 규칙으로)"})
            if st.get("targets_failed", 0) >= 3 and ratio is not None and ratio >= 0.9:
                sug.append({"rule": r["id"], "kind": "common_miss", "message": f"새 자료에서 자주 빠짐({st['targets_failed']}회) - 작성 지침/스킬에 강조 필요"})
            if r["type"] == "judgment" and not r.get("judgments"):
                sug.append({"rule": r["id"], "kind": "unjudged", "message": "판단형 규칙인데 판정 기록이 없음 → 좋은 예시로 판정해서 근거를 쌓기"})
        covered = {norm(p) for r in d["rules"] if r["status"] in ("active", "candidate") for p in (r["params"].get("patterns") or [])}
        for b in d["blueprint"]:
            if b["support"] >= 0.5 and not any(norm(p) in covered for p in b["patterns"]):
                sug.append({"section": b["name"], "kind": "uncovered_section", "message": f"자주 나오는 장({b['support']:.0%})인데 관련 규칙이 없음 → 이 장에 필요한 내용 규칙 추가"})
        recent = self.validations(20)
        miss_counter: Counter = Counter()
        for v in recent[:10]:
            try:
                full = self.validation(v["id"])
            except RulebookError:
                continue
            for c in full.get("checklist", []):
                if not c["found"]:
                    miss_counter[c["section"]] += 1
        for sec, k in miss_counter.most_common(5):
            if k >= 2:
                sug.append({"section": sec, "kind": "often_missing", "message": f"최근 검증 {k}건에서 '{sec}' 장이 빠짐"})
        n_ex = sum(1 for c in d["corpus"] if c["label"] == "exemplar")
        if n_ex < 3:
            sug.append({"kind": "few_exemplars", "message": f"좋은 예시가 {n_ex}건뿐입니다 - 규칙 신뢰도를 위해 3건 이상 학습 권장"})
        counts = Counter(r["status"] for r in d["rules"])
        return {"rulebook": d["name"], "version": d["version"], "exemplars": n_ex, "rules": dict(counts), "suggestions": sug,
                "recent_scores": [(v["name"], v["score"]) for v in recent[:10]]}

    # ---------------- 제품 기록 ----------------
    def products(self) -> list[dict[str, Any]]:
        p = self.dir / "products.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []

    def record_product(self, product: str, *, summary: str, facts: dict[str, Any] | None = None, files: list[str] | None = None,
                       validation: str | None = None, tags: list[str] | None = None, user: str | None = None) -> dict[str, Any]:
        with self.lock:
            items = self.products()
            item = next((x for x in items if norm(x["product"]) == norm(product)), None)
            entry = {"time": time.time(), "user": user, "summary": summary, "facts": facts or {}, "files": files or [], "validation": validation}
            if item is None:
                item = {"product": product, "tags": tags or [], "history": []}
                items.append(item)
            item["history"].append(entry)
            item["latest"] = entry
            if tags:
                item["tags"] = sorted(set(item.get("tags", [])) | set(tags))
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "products.json").write_text(json.dumps(items, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            self.log("record_product", user, product=product, validation=validation)
            return item


def _key_sections(d: dict[str, Any]) -> list[dict[str, Any]]:
    """계획/체크리스트에 넣을 장: 자주 나오는 장(50%↑) + 활성 '필수 장' 규칙이 요구하는 장 (사람이 정한 기준이 통계보다 우선)."""
    thr = d["settings"]["match_threshold"]
    req = [r for r in d["rules"] if r["status"] == "active" and r["type"] == "section_required"]
    out = []
    for b in d["blueprint"]:
        by_rule = [r["id"] for r in req if any(title_match(b["name"], [p], thr) or title_match(p, b["patterns"], thr) for p in r["params"].get("patterns") or [])]
        if b["support"] >= 0.5 or by_rule:
            out.append({**b, "required_by": by_rule})
    return out


class RulebookStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, name: str) -> Rulebook:
        return Rulebook(self.root, name)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*/rulebook.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            c = Counter(r["status"] for r in d.get("rules", []))
            out.append({"name": d["name"], "description": d.get("description", ""), "domain": d.get("domain", ""), "version": d.get("version"),
                        "updated": d.get("updated"), "rules": dict(c), "exemplars": sum(1 for x in d.get("corpus", []) if x["label"] == "exemplar"),
                        "sections": len([b for b in d.get("blueprint", []) if b["support"] >= 0.5])})
        return out
