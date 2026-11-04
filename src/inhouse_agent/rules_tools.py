"""규칙 학습/검증 tool (LLM 이 '스스로 분석 → 규칙화 → 검증 → 규칙 수정' 루프를 돌 수 있게)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .docstruct import doc_structure, outline_text
from .files import file_status, format_status
from .mcp_hub import BuiltinTool, ToolOutcome
from .rulebook import RULE_TYPES, Rulebook, RulebookError, RulebookStore
from .users import current_user

DOC_EXT = {".pptx", ".xlsx", ".xlsm", ".xls", ".csv", ".docx", ".pdf"}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        d["required"] = required
    return d


def _pct(x: Any) -> str:
    return "-" if x is None else f"{x:.0%}"


def fmt_blueprint(bp: list[dict[str, Any]], min_support: float = 0.0) -> str:
    rows = ["| # | 장 | 등장 | 위치 | 필요 요소 | 표 열 / 핵심어 | 평균 단어 |", "|---|---|---|---|---|---|---|"]
    for i, b in enumerate([x for x in bp if x["support"] >= min_support], 1):
        el = ", ".join(f"{ {'table': '표', 'chart': '차트', 'image': '이미지', 'notes': '노트'}.get(k, k)} {v:.0%}" for k, v in b["elements"].items()) or "-"
        extra = ("열: " + ", ".join(b["columns"][:5])) if b["columns"] else ("핵심어: " + ", ".join(b["keywords"][:5]) if b["keywords"] else "-")
        rows.append(f"| {i} | {b['name']} | {_pct(b['support'])} ({b['docs']}건) | {b['position']:.2f} | {el} | {extra} | {b['avg_words']} |")
    return "\n".join(rows)


def fmt_rules(rules: list[dict[str, Any]]) -> str:
    rows = ["| ID | 상태 | 중요도 | 유형 | 규칙 | 지지율(좋은 예시) | 검증 실패 |", "|---|---|---|---|---|---|---|"]
    for r in rules:
        sp = r.get("support") or {}
        st = r.get("stats") or {}
        lock = "🔒" if r.get("locked") else ""
        rows.append(f"| {r['id']} | {r['status']}{lock} | {r['severity']} | {r['type']} | {r['statement']} | {_pct(sp.get('ratio'))} ({sp.get('pass', 0)}/{(sp.get('pass') or 0) + (sp.get('fail') or 0)}) | {st.get('fails', 0)}/{st.get('validations', 0)} |")
    return "\n".join(rows)


def fmt_validation(v: dict[str, Any]) -> str:
    out = [f"# 검증 {v['id']}: {v['name']}" + (f" ({v['product']})" if v.get("product") else ""),
           f"점수 {v['score'] if v['score'] is not None else '-'}점 (활성 규칙 기준, must=3 should=2 nice=1 가중) · 규칙집 v{v['rulebook_version']}"]
    if v.get("checklist"):
        out.append("\n## 장별 체크리스트")
        out.append("| 장 | 있음 | 위치 | 필요 요소 | 관련 규칙 |")
        out.append("|---|---|---|---|---|")
        for c in v["checklist"]:
            need = ", ".join(f"{k}{'✅' if k in c['has'] else '❌'}" for k in c["needs"]) or "-"
            rules = ", ".join(f"{x['rule']}{'✅' if x['result'] == 'pass' else ('❌' if x['result'] == 'fail' else '·')}" for x in c["rules"]) or "-"
            out.append(f"| {c['section']} | {'✅' if c['found'] else '❌ 없음'} | {('#' + str(c['at']) + ' ' + c['title']) if c['found'] else '-'} | {need} | {rules} |")
    fails = [x for x in v["results"] if x["result"] == "fail"]
    if fails:
        out.append("\n## 어긴 규칙")
        out += [f"- {x['rule']} [{x['severity']}{'/후보' if x['status'] != 'active' else ''}] {x['statement']} → {x['detail']}" for x in sorted(fails, key=lambda x: -{"must": 3, "should": 2, "nice": 1}[x["severity"]])]
    judge = [x for x in v["results"] if x["result"] == "judge"]
    if judge:
        out.append("\n## 판단이 필요한 규칙 (내용을 읽고 rulebook_judge 로 판정하세요)")
        out += [f"- {x['rule']}: {x['detail']}" for x in judge]
    if v.get("extra_sections"):
        out.append(f"\n과거 자료에 없던 장: #{', #'.join(map(str, v['extra_sections']))} (새로 생긴 패턴일 수 있음 - 좋으면 규칙화 검토)")
    if v.get("learned"):
        out.append(f"\n좋은 예시로 학습함 → 상태 변경: {v['learned']['status_changes'] or '없음'}")
    return "\n".join(out)


def make_rule_tools(ws: Any, root: Path) -> list[BuiltinTool]:
    store = RulebookStore(root)

    def rb(name: str) -> Rulebook:
        return store.get(str(name))

    def expand(paths: list[str]) -> list[Path]:
        out: list[Path] = []
        for p in paths:
            rp = ws.resolve(p)
            if rp.is_dir():
                out += sorted(x for x in rp.rglob("*") if x.is_file() and x.suffix.lower() in DOC_EXT and not x.name.startswith(("~$", ".")))
            elif rp.is_file():
                out.append(rp)
            else:
                raise RulebookError(f"파일/폴더가 없습니다: {p}")
        return out

    def run(fn: Any, *a: Any, **k: Any) -> Any:
        return asyncio.to_thread(fn, *a, **k)

    async def guard(coro: Any) -> Any:
        try:
            return await coro
        except (RulebookError, PermissionError, ValueError) as e:
            return ToolOutcome(f"오류: {e}", True)

    # ---------------------------------------------------------------
    async def t_doc_structure(args: dict[str, Any]) -> Any:
        async def go() -> str:
            d = await run(doc_structure, ws.resolve(args["path"]))
            return outline_text(d, int(args.get("max_text") or 160), args.get("sections"))
        return await guard(go())

    async def t_list(args: dict[str, Any]) -> str:
        items = store.list()
        if not items:
            return "규칙집이 없습니다. rulebook_create 로 만들고 rulebook_learn 으로 과거 자료를 학습하세요."
        return "\n".join(f"- {x['name']}: {x['description']} (v{x['version']}, 좋은 예시 {x['exemplars']}건, 장 {x['sections']}개, 규칙 {x['rules']})" for x in items)

    async def t_create(args: dict[str, Any]) -> Any:
        async def go() -> str:
            await run(rb(args["name"]).create, str(args.get("description") or ""), str(args.get("domain") or ""), current_user.get())
            return f"규칙집 '{args['name']}' 생성. 이제 rulebook_learn 으로 과거 자료(좋은 예시)를 학습하세요."
        return await guard(go())

    async def t_learn(args: dict[str, Any]) -> Any:
        async def go() -> str:
            files = expand(args.get("paths") or [])
            if not files:
                return "오류: 학습할 파일이 없습니다 (pptx/xlsx/xls/csv/docx/pdf)."
            r = await run(rb(args["name"]).learn, files, str(args.get("label") or "exemplar"), current_user.get(), args.get("product"))
            out = [f"학습: 새 자료 {len(r['added'])}건 {r['added'][:10]}" + (f", 라벨 변경 {r['relabeled']}" if r["relabeled"] else ""),
                   f"좋은 예시 총 {r['exemplars']}건"]
            if r["errors"]:
                out.append("읽지 못한 파일: " + "; ".join(r["errors"]))
            out.append("\n## 장별 구성 (좋은 예시 기준, 등장 50% 이상)\n" + fmt_blueprint(r["blueprint"], 0.5))
            if r["new_candidates"]:
                out.append("\n## 새 규칙 후보 (자동 발굴 - 검토해서 의미 없는 것은 deprecated, 중요한 것은 severity 조정)\n" + fmt_rules(r["new_candidates"]))
            if r["status_changes"]:
                out.append("\n## 상태 자동 변경\n" + "\n".join(f"- {c['rule']} {c['from']} → {c['to']} (지지율 {_pct(c['support'])}) {c['statement']}" for c in r["status_changes"]))
            out.append("\n다음: 장별 구성과 자료 내용을 보고, 기계 검사로 못 잡는 기준(예: 결론이 데이터 근거로 도출되는지)은 judgment 규칙으로 추가하세요.")
            return "\n".join(out)
        return await guard(go())

    async def t_show(args: dict[str, Any]) -> Any:
        async def go() -> str:
            b = rb(args["name"])
            d = await run(b.load)
            view = str(args.get("view") or "summary")
            if view == "blueprint":
                return fmt_blueprint(d["blueprint"]) + "\n\n" + "\n".join(
                    f"- {x['name']} 예시: " + " / ".join(f"{e['doc']}#{e['no']} '{e['title']}' {e['text'][:60]}" for e in x["examples"][:2]) for x in d["blueprint"] if x["support"] >= 0.5)
            if view in ("rules", "candidates"):
                st = args.get("status")
                rules = [r for r in d["rules"] if (view == "rules" and (not st or r["status"] == st)) or (view == "candidates" and r["status"] in ("candidate", "needs_review"))]
                return fmt_rules(rules) if rules else "(해당 규칙 없음)"
            if view == "rule":
                r = next((x for x in d["rules"] if x["id"] == args.get("rule_id")), None)
                return json.dumps(r, ensure_ascii=False, indent=1, default=str) if r else "규칙 없음"
            if view == "corpus":
                return "\n".join(f"- [{c['label']}] {c['name']} ({c['kind']}, {c['sections']}장) {datetime.fromtimestamp(c['added']):%Y-%m-%d}" + (f" 제품: {c['product']}" if c.get("product") else "") for c in d["corpus"]) or "(자료 없음)"
            if view == "changelog":
                return "\n".join(f"- {datetime.fromtimestamp(x['time']):%m-%d %H:%M} {x['action']} {x.get('user') or ''} {json.dumps({k: v for k, v in x.items() if k not in ('time', 'action', 'user')}, ensure_ascii=False, default=str)[:200]}" for x in b.changelog(40))
            if view == "validations":
                return "\n".join(f"- {v['id']} {datetime.fromtimestamp(v['time']):%m-%d %H:%M} {v['name']} [{v['label']}] 점수 {v['score']} 실패 {v['fails']}" + (f" 제품 {v['product']}" if v.get("product") else "") for v in b.validations(30)) or "(검증 기록 없음)"
            if view == "products":
                return "\n".join(f"- {p['product']} {p.get('tags') or ''}: {p['latest']['summary'][:200]} (기록 {len(p['history'])}회)" for p in b.products()) or "(기록된 제품 없음)"
            from collections import Counter

            c = Counter(r["status"] for r in d["rules"])
            return (f"# {d['name']} v{d['version']}: {d.get('description', '')}\n좋은 예시 {sum(1 for x in d['corpus'] if x['label'] == 'exemplar')}건 · 규칙 {dict(c)}\n\n"
                    f"## 장별 구성\n{fmt_blueprint(d['blueprint'], 0.5)}\n\n## 활성 규칙\n{fmt_rules([r for r in d['rules'] if r['status'] == 'active'])}")
        return await guard(go())

    async def t_add(args: dict[str, Any]) -> Any:
        async def go() -> str:
            rule = {k: args.get(k) for k in ("type", "params", "statement", "rationale", "severity", "status", "tags") if args.get(k) is not None}
            r = await run(rb(args["name"]).add_rule, rule, current_user.get(), "agent")
            x = r["rule"]
            sp = x["support"]
            return (f"규칙 추가: {x['id']} [{x['status']}] {x['statement']}\n지지율(좋은 예시): {_pct(sp.get('ratio'))} (통과 {sp.get('pass')}, 실패 {sp.get('fail')}, 해당없음 {sp.get('na')})"
                    + (f"\n어긴 좋은 예시: {sp['failing_docs']}" if sp.get("failing_docs") else "")
                    + ("\n※ 지지율이 낮으면 조건을 바꾸거나 근거를 다시 확인하세요." if sp.get("ratio") is not None and sp["ratio"] < 0.75 else ""))
        return await guard(go())

    async def t_update(args: dict[str, Any]) -> Any:
        async def go() -> str:
            r = await run(rb(args["name"]).update_rule, str(args["rule_id"]), args.get("changes") or {}, str(args.get("reason") or ""), current_user.get())
            sp = r["support"]
            return f"규칙 수정: {r['id']} v{r['version']} [{r['status']}{'🔒' if r['locked'] else ''}] {r['statement']} · 지지율 {_pct(sp.get('ratio'))}" + (f"\n어긴 좋은 예시: {sp['failing_docs']}" if sp.get("failing_docs") else "")
        return await guard(go())

    async def t_validate(args: dict[str, Any]) -> Any:
        async def go() -> Any:
            path = ws.resolve(args["path"])
            v = await run(rb(args["name"]).validate, path, label=str(args.get("label") or "target"), product=args.get("product"),
                          user=current_user.get(), include_candidates=bool(args.get("include_candidates")))
            return fmt_validation(v)
        return await guard(go())

    async def t_judge(args: dict[str, Any]) -> Any:
        async def go() -> str:
            v = await run(rb(args["name"]).judge, str(args["validation_id"]), str(args["rule_id"]), str(args["verdict"]), str(args.get("reason") or ""), current_user.get())
            return f"판정 기록: {args['rule_id']} = {args['verdict']} · 검증 점수 {v['score']} · 남은 판정 {v['pending_judgments'] or '없음'}"
        return await guard(go())

    async def t_outline(args: dict[str, Any]) -> Any:
        async def go() -> str:
            plan = await run(rb(args["name"]).outline, str(args.get("product") or ""), str(args.get("focus") or ""))
            if not plan:
                return "장별 구성이 없습니다. 먼저 rulebook_learn 으로 과거 자료를 학습하세요."
            out = [f"# '{args.get('product') or '새 제품'}' 보고서 장별 계획 (과거 자료 기준)"]
            for i, p in enumerate(plan, 1):
                if p["section"] == "(문서 전체)":
                    out.append("\n## 문서 전체 규칙\n" + "\n".join(f"- {r}" for r in p["rules"]))
                    continue
                need = ", ".join({"table": "표", "chart": "차트", "image": "이미지", "notes": "노트"}.get(k, k) for k in p["needs"]) or "본문"
                out.append(f"\n## {i}. {p['section']}  (과거 {p['support']:.0%} 사용, 평균 {p['avg_words']}단어)" + (f" · 필수({', '.join(p['required_by'])})" if p.get("required_by") else ""))
                out.append(f"- 필요한 형식: {need}" + (f" / 표 열: {', '.join(p['columns'])}" if p["columns"] else ""))
                if p["keywords"]:
                    out.append(f"- 다뤄야 할 내용(과거 핵심어): {', '.join(p['keywords'])}")
                if p["rules"]:
                    out.append("- 지켜야 할 규칙: " + "; ".join(p["rules"]))
                if p["examples"]:
                    out.append("- 과거 예시: " + " / ".join(f"{e['doc']}#{e['no']} '{e['title']}': {e['text'][:80]}" for e in p["examples"]))
            out.append("\n다음: 장마다 필요한 자료를 MCP/웹/파일에서 모아 write_pptx(필요하면 write_excel)로 작성 → rulebook_validate 로 검증 → 고치기.")
            return "\n".join(out)
        return await guard(go())

    async def t_reflect(args: dict[str, Any]) -> Any:
        async def go() -> str:
            r = await run(rb(args["name"]).reflect)
            out = [f"# 규칙집 점검: {r['rulebook']} v{r['version']} · 좋은 예시 {r['exemplars']}건 · 규칙 {r['rules']}"]
            if r["recent_scores"]:
                out.append("최근 검증 점수: " + ", ".join(f"{n} {s}" for n, s in r["recent_scores"]))
            if not r["suggestions"]:
                out.append("특별한 문제 없음.")
            for s in r["suggestions"]:
                tgt = s.get("rule") or s.get("section") or ""
                out.append(f"- [{s['kind']}] {tgt}: {s['message']}" + (f"\n    근거: {s['evidence']}" if s.get("evidence") else ""))
            out.append("\n위 제안마다 근거 자료를 doc_structure 로 확인한 뒤 rulebook_update_rule / rulebook_add_rule 로 반영하고, 반영 이유(reason)를 꼭 남기세요.")
            return "\n".join(out)
        return await guard(go())

    async def t_product(args: dict[str, Any]) -> Any:
        async def go() -> str:
            item = await run(rb(args["name"]).record_product, str(args["product"]), summary=str(args.get("summary") or ""), facts=args.get("facts"),
                             files=args.get("files"), validation=args.get("validation_id"), tags=args.get("tags"), user=current_user.get())
            return f"제품 기록: {item['product']} (분석 {len(item['history'])}회) 태그 {item.get('tags')}"
        return await guard(go())

    async def t_export(args: dict[str, Any]) -> Any:
        async def go() -> Any:
            from .toolkit import _write_xlsx

            b = rb(args["name"])
            d = await run(b.load)
            p = ws.resolve(f"files/rulebooks/{b.name}_v{d['version']}.xlsx", write=True)
            p.parent.mkdir(parents=True, exist_ok=True)
            sheets = [
                {"name": "장별구성", "columns": ["장", "등장비율", "위치", "필요요소", "표 열", "핵심어", "평균단어"],
                 "rows": [[x["name"], x["support"], x["position"], ", ".join(f"{k}:{v}" for k, v in x["elements"].items()), ", ".join(x["columns"]), ", ".join(x["keywords"]), x["avg_words"]] for x in d["blueprint"]]},
                {"name": "규칙", "columns": ["ID", "상태", "중요도", "유형", "규칙", "근거", "지지율", "검증", "실패", "조건(JSON)"],
                 "rows": [[r["id"], r["status"], r["severity"], r["type"], r["statement"], r["rationale"], (r["support"] or {}).get("ratio"), r["stats"]["validations"], r["stats"]["fails"], json.dumps(r["params"], ensure_ascii=False)] for r in d["rules"]]},
                {"name": "검증이력", "columns": ["ID", "일시", "파일", "구분", "제품", "점수", "실패"],
                 "rows": [[v["id"], datetime.fromtimestamp(v["time"]).strftime("%Y-%m-%d %H:%M"), v["name"], v["label"], v.get("product") or "", v["score"], v["fails"]] for v in b.validations(500)]},
                {"name": "제품", "columns": ["제품", "태그", "최근 요약", "분석 횟수"],
                 "rows": [[x["product"], ", ".join(x.get("tags") or []), x["latest"]["summary"], len(x["history"])] for x in b.products()]},
            ]
            await run(_write_xlsx, p, sheets, f"규칙집 {b.name} v{d['version']}")
            st = await file_status(p, ws, stable_wait=0)
            return ToolOutcome(f"내보내기: {p}\n{format_status(st)}", False, [st])
        return await guard(go())

    async def t_write_pptx(args: dict[str, Any]) -> Any:
        async def go() -> Any:
            from .pptxgen import write_pptx

            rel = str(args["path"]).lstrip("/\\")
            if not rel.lower().endswith(".pptx"):
                rel += ".pptx"
            p = ws.resolve(rel if rel.startswith(("files/", "downloads/", "uploads/")) else f"files/{rel}", write=True)
            tpl = ws.resolve(args["template"]) if args.get("template") else None
            slides = args.get("slides") or []
            for s in slides:
                if s.get("image"):
                    s["image"] = str(ws.resolve(s["image"]))
            counts = await run(write_pptx, p, slides, tpl)
            st = await file_status(p, ws, stable_wait=0)
            return ToolOutcome(f"PPT 저장: {p}\n- {counts}\n{format_status(st)}\n※ rulebook_validate 로 규칙을 지켰는지 검증하세요.", False, [st])
        return await guard(go())

    rule_types = sorted(RULE_TYPES)
    name_prop = {"name": {"type": "string", "description": "규칙집 이름 (예: tech_report)"}}
    return [
        BuiltinTool("doc_structure", "PPT/엑셀/Word/PDF 를 '장(슬라이드/시트)' 단위로 분석합니다: 장마다 제목, 본문 요지, 표(열), 차트, 이미지, 노트. "
                    "과거 자료가 각 장에 어떤 내용을 담는지 볼 때 사용.",
                    _obj({"path": {"type": "string"}, "sections": {"type": "array", "items": {"type": "integer"}, "description": "특정 장 번호만"}, "max_text": {"type": "integer"}}, ["path"]),
                    t_doc_structure),
        BuiltinTool("rulebook_list", "규칙집 목록 (보고서 종류별로 과거 자료에서 배운 장별 구성과 규칙).", _obj({}), t_list),
        BuiltinTool("rulebook_create", "새 규칙집을 만듭니다 (예: 기술/제품 분석 보고서, 평가표).",
                    _obj({**name_prop, "description": {"type": "string"}, "domain": {"type": "string"}}, ["name", "description"]), t_create),
        BuiltinTool("rulebook_learn", "과거 자료(파일 또는 폴더)를 학습: 장 구조 추출 → 장별 구성 도출 → 규칙 후보 자동 발굴 → 모든 규칙의 지지율 재계산·상태 자동 갱신. "
                    "label=exemplar(좋은 예시, 규칙의 근거) / reference(참고만).",
                    _obj({**name_prop, "paths": {"type": "array", "items": {"type": "string"}}, "label": {"type": "string", "enum": ["exemplar", "reference"]},
                          "product": {"type": "string"}}, ["name", "paths"]), t_learn),
        BuiltinTool("rulebook_show", "규칙집 보기. view: summary | blueprint(장별 구성·예시) | rules | candidates | rule(rule_id) | corpus | validations | products | changelog",
                    _obj({**name_prop, "view": {"type": "string", "enum": ["summary", "blueprint", "rules", "candidates", "rule", "corpus", "validations", "products", "changelog"]},
                          "status": {"type": "string"}, "rule_id": {"type": "string"}}, ["name"]), t_show),
        BuiltinTool("rulebook_add_rule", "분석해서 찾은 규칙을 추가합니다. 추가하면 바로 좋은 예시들로 지지율을 계산해 알려줍니다. "
                    f"type: {', '.join(rule_types)}. params 예: section_required {{patterns:['경쟁사 비교','경쟁 현황']}}, "
                    "section_element {patterns, element: table|chart|image|visual, min}, table_columns {patterns, columns, min_match}, "
                    "section_keywords {patterns?, any:[..], all:[..]}, text_regex {pattern:'출처\\s*[:：]', patterns?}, section_length {patterns, min_words, max_words}, "
                    "excel_columns {sheet, columns}, excel_values {sheet, column, not_empty, min, max}, judgment {question:'시사점이 앞 장의 데이터로 뒷받침되는가'}.",
                    _obj({**name_prop, "type": {"type": "string", "enum": rule_types}, "params": {"type": "object"}, "statement": {"type": "string", "description": "사람이 읽는 규칙 문장"},
                          "rationale": {"type": "string", "description": "근거 (어떤 자료에서 무엇을 보고)"}, "severity": {"type": "string", "enum": ["must", "should", "nice"]},
                          "status": {"type": "string", "enum": ["candidate", "active"]}, "tags": {"type": "array", "items": {"type": "string"}}},
                         ["name", "type", "params", "statement", "rationale"]), t_add),
        BuiltinTool("rulebook_update_rule", "규칙을 수정합니다 (조건 완화/강화, 문장, 중요도, 상태: active|candidate|needs_review|deprecated). 이유(reason)는 변경기록에 남습니다. "
                    "status 를 직접 바꾸면 잠금(🔒)되어 자동 변경되지 않습니다.",
                    _obj({**name_prop, "rule_id": {"type": "string"}, "changes": {"type": "object", "description": "{params?, statement?, rationale?, severity?, status?, locked?, tags?}"},
                          "reason": {"type": "string"}}, ["name", "rule_id", "changes", "reason"]), t_update),
        BuiltinTool("rulebook_validate", "문서를 규칙집으로 검증: 장별 체크리스트(장마다 있어야 할 내용이 있는지), 어긴 규칙, 점수, 판단이 필요한 규칙. "
                    "label=target(새 자료) / exemplar(승인된 좋은 자료 → 검증 후 학습에도 추가).",
                    _obj({**name_prop, "path": {"type": "string"}, "label": {"type": "string", "enum": ["target", "exemplar"]}, "product": {"type": "string"},
                          "include_candidates": {"type": "boolean", "description": "후보 규칙도 같이 검사"}}, ["name", "path"]), t_validate),
        BuiltinTool("rulebook_judge", "판단형(judgment) 규칙을 문서를 읽고 판정하거나, 기계 판정이 틀렸을 때 바로잡습니다 (verdict: pass|fail|na, reason 필수).",
                    _obj({**name_prop, "validation_id": {"type": "string"}, "rule_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail", "na"]},
                          "reason": {"type": "string"}}, ["name", "validation_id", "rule_id", "verdict", "reason"]), t_judge),
        BuiltinTool("rulebook_outline", "새 제품/기술 분석 보고서의 장별 계획: 각 장에 필요한 형식(표/차트), 표 열, 다룰 내용, 지킬 규칙, 과거 예시.",
                    _obj({**name_prop, "product": {"type": "string"}, "focus": {"type": "string"}}, ["name"]), t_outline),
        BuiltinTool("rulebook_reflect", "규칙집 스스로 점검: 좋은 예시도 어기는 규칙(너무 엄격), 근거 없는 규칙, 규칙이 없는 장, 자주 빠지는 장, 판정 안 된 판단 규칙 등을 찾아 개선 제안.",
                    _obj(name_prop, ["name"]), t_reflect),
        BuiltinTool("rulebook_record_product", "분석한 제품/기술의 요약과 핵심 사실을 기록 (제품 간 비교, 다음 분석의 참고).",
                    _obj({**name_prop, "product": {"type": "string"}, "summary": {"type": "string"}, "facts": {"type": "object"}, "files": {"type": "array", "items": {"type": "string"}},
                          "validation_id": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, ["name", "product", "summary"]), t_product),
        BuiltinTool("rulebook_export", "규칙집(장별 구성·규칙·검증이력·제품)을 엑셀로 내보냅니다.", _obj(name_prop, ["name"]), t_export),
        BuiltinTool("write_pptx", "PPT 보고서를 만듭니다. template 에 과거 보고서 경로를 주면 그 디자인을 씁니다. "
                    "slides: [{title, bullets:[문장 또는 {text, level}], table:{columns, rows}, chart:{type: bar|barh|line|pie, title, categories, series:[{name, values}]}, image: 경로, notes, cover: true}]",
                    _obj({"path": {"type": "string", "description": "files 기준 경로. 예: reports/뮤즈_분석.pptx"}, "template": {"type": "string"},
                          "slides": {"type": "array", "items": {"type": "object"}}}, ["path", "slides"]), t_write_pptx),
    ]
