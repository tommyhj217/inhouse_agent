"""업무 지식 그래프(온톨로지): 사람들이 실제로 한 업무 기록에서 에이전트의 지식을 만든다.

근거
- Agent Workflow Memory (Wang et al., ICML 2025, arXiv 2409.07429): 성공한 실행에서 되풀이되는 작업 순서(workflow)를
  뽑아 기억에 넣고 다음 작업에 다시 쓰면 성공률이 오름 → 업무 유형별 '검증된 도구 순서'
- 프로세스 마이닝(직접 이어짐 그래프 DFG, pm4py 등): 이벤트 로그에서 '무엇 다음에 무엇을 하는지' 를 셈 → 도구 → 도구 관계
- Zep/Graphiti (arXiv 2501.13956): 사실마다 처음·마지막으로 본 시각을 둔 시간 지식 그래프 → 최근에 통한 방법을 우선
- AutoSchemaKG (arXiv 2505.23628): 정해 둔 스키마 없이 데이터에서 개념을 묶어 유형을 만듦 → 요청 문장의 개념으로 업무 유형을 묶음

LLM 을 부르지 않습니다 (토큰 0). 입력은 웹·CLI 실행이 남긴 대화 기록(sessions/*.json)뿐이고, 결과는 knowledge/graph.json.

온톨로지 (클래스 → 관계)
  TaskType(업무 유형)  ─hasConcept→ Concept(개념: 매출, 공지, 부서 …)
                       ─solvedBy→ Workflow(검증된 작업 순서) ─uses→ Tool(도구) ─partOf→ Site(사이트, MCP 서버)
                       ─produces→ ArtifactType(파일 종류: xlsx, pptx …)
                       ─implementedBy→ Skill(스킬)
  Tool ─followedBy→ Tool (DFG, 횟수)        Tool ─hasPitfall→ Pitfall(자주 난 오류)
모든 노드·관계에 count(본 횟수), first/last(처음·마지막 본 시각)를 둡니다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

VERSION = 1

# 업무 흐름에서 의미가 적은 보조 도구 (순서에서 뺌)
UTILITY_TOOLS = {"get_current_time", "update_plan", "ask_user", "list_downloads", "file_status", "wait_for_download",
                 "recall", "remember", "use_skill", "read_skill_file", "knowledge_search", "knowledge_build", "knowledge_export",
                 "knowledge_suggest_skills", "file_versions",
                 "list_past_work", "read_past_work", "agent_usage", "list_models", "business_days", "view_image",
                 "run_tool_batch"}  # 묶음은 상자일 뿐 - 안쪽 호출이 따로 기록됨

_TOKEN = re.compile(r"[0-9가-힣A-Za-z][가-힣A-Za-z0-9_.+-]*")
_JOSA = sorted(["에서는", "으로는", "에서도", "에게서", "이라도", "까지는", "부터는", "에서", "으로", "부터", "까지", "에게", "한테", "처럼",
                "보다", "하고", "이랑", "이나", "라도", "마다", "별로", "별", "랑", "나", "은", "는", "이", "가", "을", "를", "의", "에", "로",
                "와", "과", "도", "만", "요"], key=len, reverse=True)
_ENDINGS = sorted(["해주세요", "해줘요", "해줘", "해주고", "해서", "하고", "하는", "했던", "했어", "합니다", "하세요", "하기", "한", "할",
                   "주세요", "줘", "해", "하자", "아서", "어서", "아줘", "어줘", "고서"], key=len, reverse=True)
_STOP = {"좀", "그리고", "그거", "이거", "저거", "그", "이", "저", "것", "거", "수", "등", "및", "또", "다시", "같이", "모두", "전부", "각",
         "알려", "알려줘", "보여", "보여줘", "주세요", "해줘", "해주세요", "내줘", "내주세요", "봐줘", "줘요", "해봐", "있는", "없는", "있어", "없어", "어떻게", "뭐야", "뭐",
         "please", "the", "and", "for", "with", "to", "of", "in", "a", "an"}
_PERIOD = [(re.compile(r"^(오늘|어제|그제|내일|금일|전일|당일)$"), "기간:일"), (re.compile(r"^(이번주|지난주|다음주|주간|금주|전주)$"), "기간:주"),
           (re.compile(r"^(\d{1,2}월|이번달|지난달|다음달|월간|월별|전월|당월|\d{4}-\d{2})$"), "기간:월"),
           (re.compile(r"^(분기|\d분기|상반기|하반기|연간|올해|작년|\d{4}년)$"), "기간:분기·연")]
_ALIAS = {"엑셀": "xlsx", "excel": "xlsx", "xlsx": "xlsx", "xls": "xlsx", "csv": "csv", "피피티": "pptx", "ppt": "pptx", "pptx": "pptx",
          "슬라이드": "pptx", "pdf": "pdf", "워드": "docx", "docx": "docx"}


def concepts(text: str, limit: int = 12) -> list[str]:
    """요청 문장 → 개념 (조사·어미·불용어 제거, 기간 표현은 '기간:월' 처럼 일반화, 파일 종류는 확장자로)."""
    out: list[str] = []
    for raw in _TOKEN.findall(text or ""):
        w = raw.lower().strip(".-+")
        for p, label in _PERIOD:
            if p.match(w.replace(" ", "")):
                w = label
                break
        else:
            for e in _ENDINGS:
                if len(w) > len(e) + 1 and w.endswith(e):
                    w = w[: -len(e)]
                    break
            for j in _JOSA:
                if len(w) > len(j) + 1 and w.endswith(j):
                    w = w[: -len(j)]
                    break
            w = _ALIAS.get(w, w)
        if len(w) < 2 or w in _STOP or w.isdigit():
            continue
        if w not in out:
            out.append(w)
    return out[:limit]


def _function_name(server: str, tool: str) -> str:
    return tool if server in ("builtin", "?", "") else f"{server}__{tool}"


def _value_kind(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return "<숫자>"
    if isinstance(v, (list, dict)):
        return "<목록>" if isinstance(v, list) else "<객체>"
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return "<날짜>"
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return "<월>"
    if re.search(r"[/\\]|\.(xlsx|xls|csv|pptx|pdf|docx|txt|json)$", s, re.I):
        return "<파일>"
    if s.startswith("http"):
        return "<URL>"
    return "<글>"


def _pitfall_text(s: str) -> str:
    """오류 메시지 → 묶을 수 있게 일반화 (숫자·경로·따옴표 안 값 제거)."""
    s = s.removeprefix("오류:").strip().splitlines()[0] if s.strip() else ""
    s = re.sub(r"(/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+)", "<경로>", s)
    s = re.sub(r"'[^']{1,60}'|\"[^\"]{1,60}\"", "'…'", s)
    s = re.sub(r"\([^)]{0,160}\)", "(…)", s)  # 괄호 안 세부(있는 열 목록 등)가 달라도 같은 오류로
    s = re.sub(r"\d[\d,.:-]*", "N", s)
    return s[:140]


@dataclass
class Run:
    """업무 기록 하나 (대화 한 차례)."""
    prompt: str
    time: float
    success: bool
    tools: list[str]  # 함수 이름 순서 (보조 도구 뺌, 연속 중복 합침)
    errors: list[str]
    sites: set[str]
    artifacts: list[str]
    skill: str | None
    user: str
    concepts: list[str] = field(default_factory=list)
    kind: str = "chat"  # chat | skill | tool(채팅창에서 도구를 직접 실행 - 업무 유형 묶기에서는 뺌)


def _sequence(names: Iterable[str]) -> list[str]:
    seq: list[str] = []
    for n in names:
        if n.split("__")[-1] in UTILITY_TOOLS or n in UTILITY_TOOLS:
            continue
        if not seq or seq[-1] != n:
            seq.append(n)
    return seq


def read_runs(session_dirs: list[tuple[str, Path]]) -> tuple[list[Run], dict[str, Any]]:
    """대화 기록 → Run 목록 + 도구 인자 형태·오류 문구 (메시지에서)."""
    runs: list[Run] = []
    arg_kinds: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    pitfalls: dict[str, Counter] = defaultdict(Counter)
    for user, d in session_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            calls: dict[str, str] = {}
            for m in data.get("messages") or []:
                for tc in m.get("tool_calls") or []:
                    fn = (tc.get("function") or {}).get("name") or ""
                    calls[tc.get("id", "")] = fn
                    try:
                        args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if isinstance(args, dict):
                        for k, v in list(args.items())[:12]:
                            kind = _value_kind(v)
                            if kind in ("<글>", "<목록>", "<객체>"):  # 매번 같은 값이면 그 값 그대로 남기려고 원문 기록
                                lit = json.dumps(v, ensure_ascii=False)
                                kind = lit if len(lit) <= 300 else kind
                            arg_kinds[fn][k][kind] += 1
                if m.get("role") == "tool" and str(m.get("content") or "").startswith("오류"):
                    fn = calls.get(m.get("tool_call_id", ""), "")
                    if fn and fn not in UTILITY_TOOLS:
                        pitfalls[fn][_pitfall_text(str(m["content"]))] += 1
            for t in data.get("turns") or []:
                prompt = str(t.get("prompt") or "")
                if not prompt.strip():
                    continue
                pairs = [x.replace(" (오류)", "(오류)") for x in t.get("tools") or []]
                names = [_function_name(*x.removesuffix("(오류)").split(".", 1)) if "." in x else x.removesuffix("(오류)") for x in pairs]
                errors = [n for n, x in zip(names, pairs) if x.endswith("(오류)")]
                exts = [Path(f).suffix.lower().lstrip(".") for f in t.get("files") or [] if Path(f).suffix]
                sites = {x.split(".", 1)[0] for x in pairs if "." in x and x.split(".", 1)[0] not in ("builtin", "custom")}
                r = Run(prompt=prompt, time=float(t.get("time") or 0), success=t.get("status") == "completed",
                        tools=_sequence(names), errors=errors, sites=sites, artifacts=[_ALIAS.get(e, e) for e in exts],
                        skill=t.get("skill") or (data.get("skill") if t.get("kind") == "skill" else None), user=user,
                        kind=str(t.get("kind") or "chat"))
                r.concepts = concepts(prompt if not prompt.startswith("/") else prompt.split(" ", 1)[0][1:].replace("_", " "))
                runs.append(r)
    extra = {"arg_kinds": {fn: {k: dict(c) for k, c in ks.items()} for fn, ks in arg_kinds.items()},
             "pitfalls": {fn: dict(c) for fn, c in pitfalls.items()}}
    return runs, extra


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Cluster:
    key: str
    runs: list[Run] = field(default_factory=list)
    concept_count: Counter = field(default_factory=Counter)
    tool_set: set[str] = field(default_factory=set)
    skill: str | None = None

    def add(self, r: Run) -> None:
        self.runs.append(r)
        self.concept_count.update(r.concepts)
        self.tool_set |= set(r.tools)

    def top_concepts(self, n: int = 8) -> set[str]:
        return {c for c, _ in self.concept_count.most_common(n)}


def cluster_runs(runs: list[Run], threshold: float = 0.34) -> list[Cluster]:
    """업무 유형 묶기 (스키마를 미리 정하지 않음): 스킬 실행은 스킬별로, 나머지는 개념·도구가 비슷한 것끼리."""
    clusters: list[Cluster] = []
    by_skill: dict[str, Cluster] = {}
    for r in sorted(runs, key=lambda x: x.time):
        if r.skill:
            c = by_skill.get(r.skill)
            if c is None:
                c = by_skill[r.skill] = Cluster(f"skill:{r.skill}", skill=r.skill)
                clusters.append(c)
            c.add(r)
            continue
        best, score = None, 0.0
        for c in clusters:
            if c.skill:
                continue
            s = 0.65 * _jaccard(set(r.concepts), c.top_concepts()) + 0.35 * _jaccard(set(r.tools), c.tool_set)
            if s > score:
                best, score = c, s
        if best is not None and score >= threshold:
            best.add(r)
        else:
            c = Cluster(f"t{len(clusters) + 1}")
            c.add(r)
            clusters.append(c)
    return clusters


def _step_args(fn: str, arg_kinds: dict[str, dict[str, dict[str, int]]], limit: int | None = 4) -> dict[str, str]:
    """도구의 대표 인자 형태 {키: 형태}. 자주 쓴 키만 (가장 많이 쓴 키의 절반 이상)."""
    ks = arg_kinds.get(fn) or {}
    if not ks:
        return {}
    top = max(sum(v.values()) for v in ks.values())
    out: dict[str, str] = {}
    for k, kinds in sorted(ks.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(kinds.values())
        if total < 0.5 * top:
            continue
        v, n = max(kinds.items(), key=lambda kv: kv[1])
        if v[:1] in ('"', "[", "{") and (n < 2 or n < 0.5 * total):
            v = {"[": "<목록>", "{": "<객체>"}.get(v[:1], "<글>")  # 그때그때 다른 값 → 형태만
        out[k] = v
        if limit and len(out) >= limit:
            break
    return out


def _step_text(fn: str, arg_kinds: dict[str, dict[str, dict[str, int]]]) -> str:
    """도구 + 자주 쓴 인자 형태: download_sales_excel(month=<월>, wait=true)"""
    return f"{fn}({', '.join(f'{k}={v}' for k, v in _step_args(fn, arg_kinds).items())})"


class KnowledgeGraph:
    """graph.json 을 읽고 쓰고, 만들고, 찾는다."""

    def __init__(self, path: Path, rebuild_minutes: float = 60, min_runs: int = 2):
        self.path = Path(path)
        self.rebuild_minutes = rebuild_minutes
        self.min_runs = min_runs
        self.sources: Any = None  # () -> list[(user, sessions_dir)] (runtime 이 넣음)
        self.data: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 만들기
    def build(self, session_dirs: list[tuple[str, Path]] | None = None) -> dict[str, Any]:
        dirs = session_dirs if session_dirs is not None else (self.sources() if self.sources else [])
        runs, extra = read_runs(dirs)
        # 업무 유형은 사람이 말로 요청한 업무(대화·스킬)로만 묶음. 도구 직접 실행은 도구 흐름(DFG)·인자 형태에만 씀
        clusters = cluster_runs([r for r in runs if r.kind != "tool"])
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}

        def node(nid: str, typ: str, label: str, t: float | None = None, **props: Any) -> str:
            n = nodes.setdefault(nid, {"type": typ, "label": label, "count": 0, "first": t, "last": t})
            n["count"] += 1
            if t:
                n["first"] = min(n["first"] or t, t)
                n["last"] = max(n["last"] or t, t)
            n.update({k: v for k, v in props.items() if v is not None})
            return nid

        def edge(s: str, p: str, o: str, t: float | None = None, w: int = 1) -> None:
            e = edges.setdefault((s, p, o), {"s": s, "p": p, "o": o, "count": 0, "first": t, "last": t})
            e["count"] += w
            if t:
                e["first"] = min(e["first"] or t, t)
                e["last"] = max(e["last"] or t, t)

        # 도구·사이트·직접 이어짐(DFG)·주의할 오류
        for r in runs:
            prev = "◉시작"
            for fn in r.tools:
                tid = node(f"tool:{fn}", "Tool", fn, r.time)
                if "__" in fn and not fn.startswith("custom__"):
                    edge(tid, "partOf", node(f"site:{fn.split('__', 1)[0]}", "Site", fn.split("__", 1)[0], r.time), r.time)
                if prev != "◉시작":
                    edge(f"tool:{prev}", "followedBy", tid, r.time)
                prev = fn
        for fn, texts in extra["pitfalls"].items():
            for text, n in sorted(texts.items(), key=lambda kv: -kv[1])[:5]:
                pid = f"pitfall:{fn}:{hashlib.md5(text.encode()).hexdigest()[:8]}"
                nodes[pid] = {"type": "Pitfall", "label": text, "count": n, "first": None, "last": None, "tool": fn}
                edge(node(f"tool:{fn}", "Tool", fn), "hasPitfall", pid, w=n)

        # 업무 유형 · 검증된 작업 순서
        n_runs = max(1, len(runs))
        df = Counter(c for r in runs for c in set(r.concepts))
        for c in clusters:
            ok = [r for r in c.runs if r.success]
            if len(c.runs) < self.min_runs and not c.skill:
                continue  # 한 번 해 본 일은 아직 '유형'으로 보지 않음
            ranked = [w for w, _ in sorted(c.concept_count.items(), key=lambda kv: (-kv[1] * math.log(1 + n_runs / df[kv[0]]), kv[0]))
                      if not w.startswith("기간:")]
            top: list[str] = []
            for w in ranked:  # 이름: 기간 표현은 빼고, '공지'/'공지사항' 처럼 겹치면 짧은 쪽만
                if any(w.startswith(t) or t.startswith(w) for t in top):
                    continue
                top.append(w)
                if len(top) == 3:
                    break
            label = c.skill or " · ".join(top) or c.key
            tt = f"task:{c.key}"
            last = max(r.time for r in c.runs)
            nodes[tt] = {"type": "TaskType", "label": label, "count": len(c.runs), "success": len(ok),
                         "users": len({r.user for r in c.runs}), "first": min(r.time for r in c.runs), "last": last,
                         "examples": [r.prompt[:90] for r in sorted(ok or c.runs, key=lambda r: -r.time)[:3]],
                         "sites": sorted({s for r in c.runs for s in r.sites})}
            for w, cnt in c.concept_count.most_common(10):
                edge(tt, "hasConcept", node(f"concept:{w}", "Concept", w, last), last, cnt)
            for a, cnt in Counter(a for r in c.runs for a in set(r.artifacts)).items():
                edge(tt, "produces", node(f"artifact:{a}", "ArtifactType", a, last), last, cnt)
            if c.skill:
                edge(tt, "implementedBy", node(f"skill:{c.skill}", "Skill", c.skill, last), last, len(c.runs))
            variants = Counter(tuple(r.tools) for r in ok if r.tools)
            for i, (seq, cnt) in enumerate(sorted(variants.items(), key=lambda kv: (-kv[1], len(kv[0])))[:2]):
                wid = f"workflow:{c.key}:{i + 1}"
                when = [r.time for r in ok if tuple(r.tools) == seq]
                nodes[wid] = {"type": "Workflow", "label": " → ".join(s.split("__")[-1] for s in seq), "count": cnt,
                              "first": min(when), "last": max(when), "steps": [_step_text(s, extra["arg_kinds"]) for s in seq],
                              "step_args": [_step_args(s, extra["arg_kinds"], None) for s in seq],
                              "tools": list(seq)}
                edge(tt, "solvedBy", wid, max(when), cnt)
                for s in seq:
                    edge(wid, "uses", f"tool:{s}", max(when))
            fails = Counter(fn for r in c.runs for fn in r.errors)
            if fails:
                nodes[tt]["error_tools"] = dict(fails.most_common(3))

        for nid in [e["o"] for e in edges.values()] + [e["s"] for e in edges.values()]:
            nodes.setdefault(nid, {"type": nid.split(":", 1)[0].title(), "label": nid.split(":", 1)[-1], "count": 0, "first": None, "last": None})
        self.data = {"version": VERSION, "built_at": time.time(), "schema": SCHEMA,
                     "source": {"runs": len(runs), "success": sum(r.success for r in runs), "users": len({r.user for r in runs}),
                                "task_types": sum(1 for n in nodes.values() if n["type"] == "TaskType")},
                     "df": dict(df), "nodes": nodes, "edges": list(edges.values())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        return self.data

    def load(self) -> dict[str, Any] | None:
        if self.data is None and self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = None
        return self.data

    def fresh(self) -> dict[str, Any] | None:
        """오래됐으면(rebuild_minutes) 다시 만듦 - 기록을 읽기만 하므로 토큰 비용 없음."""
        d = self.load()
        if self.sources is not None and (d is None or time.time() - d.get("built_at", 0) > self.rebuild_minutes * 60):
            d = self.build()
        return d

    # ------------------------------------------------------------------ 찾기
    def search(self, query: str, top: int = 3) -> list[dict[str, Any]]:
        """질문의 개념으로 업무 유형을 찾고(로컬 그래프 검색), 그 유형의 검증된 순서·파일·주의할 오류를 모아 돌려줌."""
        d = self.fresh()
        if not d:
            return []
        nodes, edges = d["nodes"], d["edges"]
        q = set(concepts(query, 20))
        if not q:
            return []
        n_runs = max(1, d["source"]["runs"])
        out_e: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in edges:
            out_e[e["s"]].append(e)
        now = time.time()
        scored = []
        for nid, n in nodes.items():
            if n["type"] != "TaskType":
                continue
            cs = {e["o"].split(":", 1)[1]: e["count"] for e in out_e[nid] if e["p"] == "hasConcept"}
            hit = q & set(cs)
            skill = next((e["o"].split(":", 1)[1] for e in out_e[nid] if e["p"] == "implementedBy"), None)
            if skill and skill.lower() in query.lower():
                hit = hit | {skill}
            if not hit:
                continue
            idf = sum(math.log(1 + n_runs / max(1, d["df"].get(c, 1))) for c in hit)
            norm = sum(math.log(1 + n_runs / max(1, d["df"].get(c, 1))) for c in q) or 1
            rate = (n.get("success", 0) + 1) / (n["count"] + 2)  # 성공률 (적은 표본은 0.5 쪽으로)
            recency = 0.5 ** ((now - (n.get("last") or now)) / (86400 * 90))  # 90일마다 절반
            score = (idf / norm) * (0.6 + 0.4 * rate) * (0.7 + 0.3 * recency)
            scored.append((score, nid, sorted(hit)))
        results = []
        ranked = sorted(scored, reverse=True)
        best = ranked[0][0] if ranked else 0
        ranked = [x for x in ranked if x[0] >= 0.35 * best]  # '정리' 같은 흔한 말 하나로만 걸린 약한 결과는 뺌
        for score, nid, hit in ranked[:top]:
            n = nodes[nid]
            wfs = sorted((nodes[e["o"]] for e in out_e[nid] if e["p"] == "solvedBy"), key=lambda w: -w["count"])
            tools = {t for w in wfs for t in w.get("tools", [])}
            pits = []
            for t in sorted(tools):
                for e in out_e.get(f"tool:{t}", []):
                    if e["p"] == "hasPitfall":
                        pits.append((e["count"], t, nodes[e["o"]]["label"]))
            results.append({
                "task_type": n["label"], "score": round(score, 3), "matched": hit, "runs": n["count"], "success": n.get("success", 0),
                "users": n.get("users", 0), "last": n.get("last"), "sites": n.get("sites", []),
                "workflows": [{"steps": w.get("steps", []), "count": w["count"], "label": w["label"]} for w in wfs],
                "artifacts": {e["o"].split(":", 1)[1]: e["count"] for e in out_e[nid] if e["p"] == "produces"},
                "skill": next((e["o"].split(":", 1)[1] for e in out_e[nid] if e["p"] == "implementedBy"), None),
                "pitfalls": [{"tool": t, "text": txt, "count": c} for c, t, txt in sorted(pits, reverse=True)[:3]],
                "examples": n.get("examples", []),
            })
        return results

    def hint(self, prompt: str, min_score: float = 0.45, min_success: int = 2) -> str:
        """새 요청에 붙일 짧은 참고 (검증된 순서가 있을 때만). 요청 끝에 붙으므로 프롬프트 캐시를 깨지 않음."""
        info = self.hint_info(prompt, min_score, min_success)
        return info["text"] if info else ""

    def hint_info(self, prompt: str, min_score: float = 0.45, min_success: int = 2) -> dict[str, Any] | None:
        """참고 글 + 어느 업무 유형인지 (효과 측정용으로 실행 기록에 남김)."""
        res = [r for r in self.search(prompt, 1) if r["score"] >= min_score and r["success"] >= min_success and r["workflows"]]
        if not res:
            return None
        r = res[0]
        w = r["workflows"][0]
        lines = [f"[참고: 지식 그래프 - 비슷한 과거 업무 「{r['task_type']}」 성공 {r['success']}/{r['runs']}회]",
                 "검증된 순서: " + " → ".join(w["steps"])]
        if r["pitfalls"]:
            p = r["pitfalls"][0]
            lines.append(f"주의: {p['tool']} 에서 '{p['text']}' 오류가 {p['count']}번 있었음")
        lines.append("요청과 맞으면 이 순서를 따르고(여러 단계면 run_tool_batch 로 묶기), 맞지 않으면 무시하세요.")
        return {"text": "\n".join(lines), "task_type": r["task_type"], "score": r["score"]}

    def classify(self, prompt: str, min_score: float = 0.45) -> str | None:
        """요청이 어느 업무 유형인지 (없으면 None) - 효과 측정에서 '같은 유형인데 참고 없이 한 실행'을 찾을 때."""
        res = self.search(prompt, 1)
        return res[0]["task_type"] if res and res[0]["score"] >= min_score else None

    def suggest_skills(self, min_success: int = 3, existing: set[str] | None = None, out_dir: Path | None = None) -> list[dict[str, Any]]:
        """스킬로 만들 만한 업무: 스킬이 아직 없고, 같은 순서로 min_success 번 이상 성공(성공의 절반 이상이 그 순서)했고 2단계 이상.
        초안(SKILL.md)을 knowledge/skill_drafts/<이름>/ 에 쓰고 목록을 돌려줌 (설치는 편집자가 스킬 가져오기로)."""
        d = self.fresh()
        if not d:
            return []
        nodes, edges = d["nodes"], d["edges"]
        out_e: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in edges:
            out_e[e["s"]].append(e)
        base = Path(out_dir or self.path.parent / "skill_drafts")
        existing = {x.lower() for x in (existing or set())}
        drafts = []
        for nid, n in sorted(nodes.items(), key=lambda kv: -kv[1].get("success", 0)):
            if n["type"] != "TaskType" or any(e["p"] == "implementedBy" for e in out_e[nid]):
                continue
            wfs = sorted((nodes[e["o"]] for e in out_e[nid] if e["p"] == "solvedBy"), key=lambda w: -w["count"])
            if not wfs:
                continue
            wf = wfs[0]
            if wf["count"] < min_success or wf["count"] < 0.5 * n.get("success", 0) or len(wf.get("tools") or []) < 2:
                continue
            slug = skill_slug(n["label"], wf.get("tools"))
            if slug.lower() in existing:
                continue
            pits = [f"{nodes[e['o']].get('tool', '')}: {nodes[e['o']]['label']} ({e['count']}회)"
                    for t in wf.get("tools") or [] for e in out_e.get(f"tool:{t}", []) if e["p"] == "hasPitfall"][:5]
            path = base / slug / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(skill_draft(n, wf, pits, slug), encoding="utf-8")
            drafts.append({"name": slug, "task_type": n["label"], "runs": n["count"], "success": n.get("success", 0),
                           "workflow": wf["label"], "path": str(path)})
        return drafts

    # ------------------------------------------------------------------ 보여주기·내보내기
    def overview(self) -> str:
        d = self.fresh()
        if not d:
            return "지식 그래프가 아직 없습니다. 에이전트로 업무를 몇 번 실행한 뒤 knowledge_build 를 하세요."
        nodes = d["nodes"]
        by = Counter(n["type"] for n in nodes.values())
        s = d["source"]
        lines = [f"## 업무 지식 그래프 ({datetime.fromtimestamp(d['built_at']).strftime('%m-%d %H:%M')} 기준)",
                 f"- 학습한 기록: 실행 {s['runs']}건 (성공 {s['success']}), 사용자 {s['users']}명",
                 "- 노드: " + ", ".join(f"{TYPE_KO.get(t, t)} {n}" for t, n in by.most_common()),
                 f"- 관계: {len(d['edges'])}개"]
        tts = sorted((n for n in nodes.values() if n["type"] == "TaskType"), key=lambda n: -n["count"])[:10]
        if tts:
            lines.append("\n### 업무 유형 (많이 한 순)\n| 유형 | 실행 | 성공 | 사용자 | 최근 |\n|---|---:|---:|---:|---|")
            lines += [f"| {n['label']} | {n['count']} | {n.get('success', 0)} | {n.get('users', 0)} | "
                      f"{datetime.fromtimestamp(n['last']).strftime('%m-%d') if n.get('last') else '-'} |" for n in tts]
        dfg = sorted((e for e in d["edges"] if e["p"] == "followedBy"), key=lambda e: -e["count"])[:8]
        if dfg:
            lines.append("\n### 자주 이어지는 도구 (직접 이어짐 그래프)\n" + "\n".join(
                f"- {e['s'].split(':', 1)[1]} → {e['o'].split(':', 1)[1]} ({e['count']}회)" for e in dfg))
        return "\n".join(lines)

    def export(self, fmt: str, out_dir: Path | None = None) -> Path:
        d = self.fresh()
        if not d:
            raise ValueError("지식 그래프가 아직 없습니다.")
        out_dir = Path(out_dir or self.path.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "ttl":
            p = out_dir / "knowledge.ttl"
            p.write_text(to_turtle(d), encoding="utf-8")
        elif fmt == "graphml":
            p = out_dir / "knowledge.graphml"
            p.write_text(to_graphml(d), encoding="utf-8")
        elif fmt == "json":
            p = out_dir / "knowledge.json"
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            raise ValueError("형식은 ttl / graphml / json 중 하나입니다.")
        return p


# ---------------------------------------------------------------------- 스킬 초안 (AWM·SkillWeaver: 검증된 순서 → 재사용 절차)
_INPUT_FOR_KIND = {"<월>": ("month", "월 (YYYY-MM)", "{last_month}"), "<날짜>": ("date", "날짜 (YYYY-MM-DD)", "{yesterday}"),
                   "<URL>": ("url", "주소", None)}


def _is_output(fn: str, key: str) -> bool:
    name = fn.split("__")[-1]
    return key in ("output", "out", "dest", "save_to", "output_path") or (
        key == "path" and name.startswith(("write_", "save_", "export_", "make_", "create_")))


def skill_slug(label: str, tools: list[str] | None = None) -> str:
    """스킬 이름은 영문·숫자·_ 만 됨 → 처음·마지막 도구 이름 + 업무 이름 해시 (한글 업무 이름은 설명·제목에)."""
    parts = []
    for t in (tools or [])[:1] + (tools or [])[-1:]:
        short = re.sub(r"[^0-9A-Za-z_]+", "_", t.split("__")[-1]).strip("_")
        if short and short not in parts:
            parts.append(short)
    ascii_label = re.sub(r"[^0-9A-Za-z]+", "_", label).strip("_")
    base = "_".join(parts) or ascii_label or "task"
    return f"auto_{base[:40]}_{hashlib.md5(label.encode()).hexdigest()[:4]}"


def skill_draft(task: dict[str, Any], wf: dict[str, Any], pitfalls: list[str], slug: str) -> str:
    """업무 유형 + 검증된 작업 순서 → SKILL.md 초안 (steps 로 LLM 없이도 실행 가능, 본문은 LLM 이 따를 지침)."""
    import yaml

    inputs: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    todo: list[str] = []
    for i, (fn, args) in enumerate(zip(wf.get("tools") or [], wf.get("step_args") or []), 1):
        a: dict[str, Any] = {}
        for k, kind in args.items():
            if kind in _INPUT_FOR_KIND:
                name, label, default = _INPUT_FOR_KIND[kind]
                inputs.setdefault(name, {"name": name, "label": label, **({"default": default} if default else {"required": True})})
                a[k] = "{" + name + "}"
            elif kind == "<파일>" and _is_output(fn, k):  # 새로 저장할 파일 이름
                ext = ".xlsx" if "excel" in fn else ".pptx" if "pptx" in fn else ".csv" if "csv" in fn else ""
                inputs.setdefault(k, {"name": k, "label": "저장할 파일 이름", "default": f"reports/{slug}_{{today}}{ext}"})
                a[k] = "{" + k + "}"
            elif kind == "<파일>" and i > 1:
                a[k] = "{steps.s%d.file}" % (i - 1)  # 앞 단계가 만든(받은) 파일
                todo.append(f"{i}단계 {k}: 앞 단계 파일을 쓰도록 했습니다. 다른 파일이면 고치세요.")
            elif kind in ("<파일>", "<글>", "<숫자>", "<목록>", "<객체>"):
                spec: dict[str, Any] = {"name": k, "label": k, "required": True}
                if kind == "<숫자>":
                    spec["type"] = "number"
                if kind == "<목록>":
                    spec.update(type="text", description="여러 개는 줄바꿈으로")
                if kind == "<객체>":
                    spec.update(type="text", description="JSON 객체")
                    todo.append(f"{i}단계 {k}: JSON 객체 값이라 입력 형식을 확인하세요.")
                inputs.setdefault(k, spec)
                a[k] = "{" + k + ("|lines}" if kind == "<목록>" else "}")
            elif kind in ("true", "false"):
                a[k] = kind == "true"
            else:
                try:
                    a[k] = json.loads(kind)  # 매번 같은 값으로 쓴 인자는 그대로
                except ValueError:
                    a[k] = kind
        steps.append({"id": f"s{i}", "title": fn.split("__")[-1], "tool": fn, "args": a})
    examples = task.get("examples") or []
    front = {
        "name": slug,
        "description": f"{task['label']} 업무. 업무 지식 그래프가 실행 기록에서 만든 초안 (성공 {task.get('success', 0)}/{task['count']}회, "
                       f"사용자 {task.get('users', 0)}명)" + (f". 예: {examples[0]}" if examples else ""),
        "inputs": list(inputs.values()),
        "steps": steps,
    }
    body = [f"# {task['label']} (자동 제안 초안)", "",
            f"> 사람들이 실제로 성공한 순서({wf['count']}회)로 만든 초안입니다. 편집자가 확인·수정한 뒤 설치하세요.", "",
            "## 절차", *[f"{i}. {t}" for i, t in enumerate(wf.get("steps") or [], 1)], ""]
    if pitfalls:
        body += ["## 주의 (자주 난 오류)", *[f"- {p}" for p in pitfalls], ""]
    if todo:
        body += ["## 설치 전에 확인할 것", *[f"- {t}" for t in todo], ""]
    if examples:
        body += ["## 이 업무의 실제 요청 예", *[f"- {e}" for e in examples], ""]
    return "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=1000) + "---\n" + "\n".join(body)


TYPE_KO = {"TaskType": "업무 유형", "Workflow": "작업 순서", "Tool": "도구", "Site": "사이트", "Concept": "개념",
           "ArtifactType": "파일 종류", "Skill": "스킬", "Pitfall": "주의할 오류"}
SCHEMA = {
    "classes": {k: v for k, v in TYPE_KO.items()},
    "relations": {
        "hasConcept": ["TaskType", "Concept", "업무 유형의 요청에 나온 개념"],
        "solvedBy": ["TaskType", "Workflow", "그 업무를 성공시킨 도구 순서"],
        "uses": ["Workflow", "Tool", "작업 순서에 쓰인 도구"],
        "partOf": ["Tool", "Site", "도구가 속한 사이트(MCP 서버)"],
        "followedBy": ["Tool", "Tool", "직접 이어서 쓴 도구 (프로세스 마이닝 DFG)"],
        "produces": ["TaskType", "ArtifactType", "그 업무가 만든 파일 종류"],
        "implementedBy": ["TaskType", "Skill", "그 업무를 하는 스킬"],
        "hasPitfall": ["Tool", "Pitfall", "그 도구에서 자주 난 오류"],
    },
}


# ---------------------------------------------------------------------- 내보내기 (외부 라이브러리 없이)
def _iri(nid: str) -> str:
    return "iak:" + re.sub(r"[^A-Za-z0-9_\-가-힣]", "_", nid.replace(":", "_"))


def _lit(s: Any) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '"'


def _dt(t: float | None) -> str | None:
    return f'"{datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S")}"^^xsd:dateTime' if t else None


def to_turtle(d: dict[str, Any]) -> str:
    """RDF/OWL (Turtle). Protégé·GraphDB·rdflib 로 열 수 있음. 스키마(TBox) + 사실(ABox)."""
    out = ["@prefix iak: <urn:inhouse-agent:knowledge#> .", "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
           "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .", "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
           "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .", "",
           "<urn:inhouse-agent:knowledge> a owl:Ontology ; rdfs:comment " + _lit("사내 에이전트 업무 지식 그래프 (실행 기록에서 자동 생성)") + " .", ""]
    for cls, ko in TYPE_KO.items():
        out.append(f"iak:{cls} a owl:Class ; rdfs:label {_lit(ko)}@ko .")
    for rel, (dom, rng, ko) in SCHEMA["relations"].items():
        out.append(f"iak:{rel} a owl:ObjectProperty ; rdfs:domain iak:{dom} ; rdfs:range iak:{rng} ; rdfs:comment {_lit(ko)}@ko .")
    for p in ("count", "success", "users"):
        out.append(f"iak:{p} a owl:DatatypeProperty ; rdfs:range xsd:integer .")
    out += ["iak:firstSeen a owl:DatatypeProperty ; rdfs:range xsd:dateTime .", "iak:lastSeen a owl:DatatypeProperty ; rdfs:range xsd:dateTime .",
            "iak:step a owl:DatatypeProperty .", ""]
    for nid, n in d["nodes"].items():
        props = [f"a iak:{n['type']}", f"rdfs:label {_lit(n['label'])}", f"iak:count {int(n.get('count') or 0)}"]
        if n.get("success") is not None and n["type"] == "TaskType":
            props += [f"iak:success {int(n['success'])}", f"iak:users {int(n.get('users') or 0)}"]
        for k, pred in (("first", "iak:firstSeen"), ("last", "iak:lastSeen")):
            if _dt(n.get(k)):
                props.append(f"{pred} {_dt(n.get(k))}")
        for i, s in enumerate(n.get("steps") or [], 1):
            props.append(f"iak:step {_lit(f'{i}. {s}')}")
        out.append(f"{_iri(nid)} " + " ;\n    ".join(props) + " .")
    out.append("")
    for e in d["edges"]:
        out.append(f"{_iri(e['s'])} iak:{e['p']} {_iri(e['o'])} .")
    # 관계의 횟수·시각은 RDF-star 대신 reification 노드로
    for i, e in enumerate(d["edges"]):
        if e["count"] > 1 or e.get("last"):
            props = [f"rdf:subject {_iri(e['s'])}", f"rdf:predicate iak:{e['p']}", f"rdf:object {_iri(e['o'])}", f"iak:count {int(e['count'])}"]
            if _dt(e.get("last")):
                props.append(f"iak:lastSeen {_dt(e['last'])}")
            out.append(f"iak:stmt_{i} a rdf:Statement ; " + " ; ".join(props) + " .")
    return "\n".join(out) + "\n"


def _x(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def to_graphml(d: dict[str, Any]) -> str:
    """GraphML: Gephi·yEd·Neo4j(APOC)·networkx 로 열 수 있음."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<key id="type" for="node" attr.name="type" attr.type="string"/>',
           '<key id="label" for="node" attr.name="label" attr.type="string"/>',
           '<key id="count" for="node" attr.name="count" attr.type="int"/>',
           '<key id="rel" for="edge" attr.name="rel" attr.type="string"/>',
           '<key id="weight" for="edge" attr.name="weight" attr.type="int"/>',
           '<graph id="knowledge" edgedefault="directed">']
    for nid, n in d["nodes"].items():
        out.append(f'<node id="{_x(nid)}"><data key="type">{_x(n["type"])}</data><data key="label">{_x(n["label"])}</data>'
                   f'<data key="count">{int(n.get("count") or 0)}</data></node>')
    for i, e in enumerate(d["edges"]):
        out.append(f'<edge id="e{i}" source="{_x(e["s"])}" target="{_x(e["o"])}"><data key="rel">{_x(e["p"])}</data>'
                   f'<data key="weight">{int(e["count"])}</data></edge>')
    out += ["</graph>", "</graphml>"]
    return "\n".join(out) + "\n"


def format_results(results: list[dict[str, Any]], source: dict[str, Any] | None = None) -> str:
    if not results:
        return "비슷한 과거 업무를 찾지 못했습니다 (지식 그래프에 없거나 아직 기록이 적음)."
    lines = [f"## 비슷한 과거 업무 (지식 그래프" + (f", 실행 {source['runs']}건에서 학습" if source else "") + ")"]
    for i, r in enumerate(results, 1):
        when = datetime.fromtimestamp(r["last"]).strftime("%m-%d") if r.get("last") else "-"
        lines.append(f"{i}. 「{r['task_type']}」 성공 {r['success']}/{r['runs']}회 · 사용자 {r['users']}명 · 최근 {when} · 일치: {', '.join(r['matched'])}")
        for w in r["workflows"][:2]:
            lines.append(f"   - 검증된 순서 ({w['count']}회): " + " → ".join(w["steps"]))
        if r["artifacts"]:
            lines.append("   - 만든 파일: " + ", ".join(f"{a} ×{n}" for a, n in r["artifacts"].items()))
        if r["skill"]:
            lines.append(f"   - 스킬: {r['skill']}")
        for p in r["pitfalls"]:
            lines.append(f"   - 주의: {p['tool']} '{p['text']}' ({p['count']}회)")
        if r["examples"]:
            lines.append("   - 예: " + " / ".join(f"\"{e}\"" for e in r["examples"][:2]))
    return "\n".join(lines)


def make_knowledge_tools(kg: KnowledgeGraph, skill_names: Any = None) -> list[Any]:
    import asyncio

    from .mcp_hub import BuiltinTool, ToolOutcome

    async def knowledge_search(args: dict[str, Any]) -> ToolOutcome:
        q = str(args.get("query") or "").strip()
        if not q:
            return ToolOutcome("오류: query(찾을 업무 설명)가 필요합니다.", True)
        res = await asyncio.to_thread(kg.search, q, int(args.get("top") or 3))
        d = kg.load() or {}
        return ToolOutcome(format_results(res, d.get("source")), False, [], {"results": res})

    async def knowledge_build(args: dict[str, Any]) -> ToolOutcome:
        await asyncio.to_thread(kg.build)
        return ToolOutcome(await asyncio.to_thread(kg.overview), False, [], (kg.load() or {}).get("source"))

    async def knowledge_export(args: dict[str, Any]) -> ToolOutcome:
        fmt = str(args.get("format") or "ttl").lower()
        try:
            p = await asyncio.to_thread(kg.export, fmt)
        except ValueError as e:
            return ToolOutcome(f"오류: {e}", True)
        return ToolOutcome(f"내보냄: {p} ({p.stat().st_size:,} bytes) - "
                           + {"ttl": "Protégé·GraphDB·rdflib 로 열 수 있는 RDF/OWL", "graphml": "Gephi·yEd·networkx·Neo4j(APOC) 로 열 수 있는 그래프",
                              "json": "원본 데이터"}[fmt], False, [{"path": str(p), "state": "complete"}])

    async def knowledge_suggest_skills(args: dict[str, Any]) -> ToolOutcome:
        existing = set(skill_names()) if skill_names else set()
        drafts = await asyncio.to_thread(kg.suggest_skills, int(args.get("min_success") or 3), existing)
        if not drafts:
            return ToolOutcome("스킬로 만들 만한 업무가 아직 없습니다 (같은 순서로 3번 이상 성공한, 스킬이 없는 업무가 없음).", False, [], {"drafts": []})
        lines = [f"## 스킬로 만들 만한 업무 {len(drafts)}개 (초안을 만들었습니다 - 편집자가 확인 후 설치)"]
        for x in drafts:
            lines.append(f"- 「{x['task_type']}」 성공 {x['success']}/{x['runs']}회 · 순서: {x['workflow']}\n  초안: {x['path']}")
        lines.append("설치: 스킬 탭 → 가져오기 → 폴더 경로에 초안 폴더, 또는 `inhouse-agent skills import <초안 폴더>`")
        return ToolOutcome("\n".join(lines), False, [{"path": x["path"], "state": "complete"} for x in drafts], {"drafts": drafts})

    return [
        BuiltinTool("knowledge_suggest_skills",
                    "업무 지식 그래프에서 스킬로 만들 만한 업무(같은 순서로 여러 번 성공했지만 스킬이 없는 업무)를 찾아 SKILL.md 초안을 만듭니다. "
                    "초안은 바로 설치되지 않고, 편집자가 확인한 뒤 스킬 가져오기로 설치합니다.",
                    {"type": "object", "properties": {"min_success": {"type": "integer", "description": "같은 순서로 최소 몇 번 성공 (기본 3)"}}},
                    knowledge_suggest_skills),
        BuiltinTool("knowledge_search",
                    "업무 지식 그래프에서 비슷한 과거 업무를 찾습니다: 성공한 도구 순서(인자 형태 포함), 만든 파일 종류, 관련 스킬, 자주 난 오류. "
                    "처음 하는 복잡한 업무를 시작하기 전에 쓰면 시행착오를 줄일 수 있습니다. (실행 기록에서 자동으로 만든 지식, 토큰 비용 없음)",
                    {"type": "object", "properties": {"query": {"type": "string", "description": "하려는 업무 설명"},
                                                      "top": {"type": "integer", "description": "몇 개 (기본 3)"}}, "required": ["query"]},
                    knowledge_search),
        BuiltinTool("knowledge_build", "실행 기록에서 업무 지식 그래프를 다시 만들고 개요(업무 유형, 자주 이어지는 도구)를 보여줍니다.",
                    {"type": "object", "properties": {}}, knowledge_build),
        BuiltinTool("knowledge_export", "업무 지식 그래프를 파일로 내보냅니다: ttl(RDF/OWL 온톨로지), graphml(그래프 도구용), json.",
                    {"type": "object", "properties": {"format": {"type": "string", "enum": ["ttl", "graphml", "json"]}}}, knowledge_export),
    ]
