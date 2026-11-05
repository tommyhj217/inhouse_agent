"""에이전트 사용 이력(누가 언제 무엇을 실행했고 토큰을 얼마나 썼는지) 과 모델 목록 조회.

사용 이력 원본: 사용자 작업폴더의 sessions/<id>.json 의 turns (웹/CLI 실행마다 1건 기록)
             + results/ 아래 저장된 결과 파일 (스킬 테스트, 예약작업)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .mcp_hub import BuiltinTool, ToolOutcome
from .users import current_user

if TYPE_CHECKING:
    from .runtime import AgentRuntime


def _turns(sess_dir: Path, since: float) -> list[dict[str, Any]]:
    out = []
    if not sess_dir.is_dir():
        return out
    for p in sess_dir.glob("*.json"):
        try:
            if p.stat().st_mtime < since:
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for t in d.get("turns") or []:
            if (t.get("time") or 0) >= since:
                out.append({**t, "session_id": d.get("id", p.stem), "session_title": d.get("title") or "", "session_skill": d.get("skill")})
    return out


def _results(res_dir: Path, since: float) -> list[dict[str, Any]]:
    out = []
    if not res_dir.is_dir():
        return out
    for tr in res_dir.glob("*/_trace/*.json"):
        try:
            if tr.stat().st_mtime < since:
                continue
            d = json.loads(tr.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": tr.parent.parent.name, "time": tr.stat().st_mtime, "status": d.get("status"), "usage": d.get("usage") or {},
                    "elapsed": d.get("elapsed_sec"), "tools": d.get("tools_used") or [], "skills": d.get("skills_used") or []})
    return out


def collect_usage(rt: "AgentRuntime", users: list[str | None], days: float = 30) -> dict[str, Any]:
    """users: 사용자 ID 목록 (None = 공용 작업폴더)."""
    since = time.time() - days * 86400
    per_user: dict[str, Any] = {}
    all_turns: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    for uid in users:
        ws = rt.users.get(uid) if uid else rt.users.base
        turns = _turns(Path(ws.root) / "sessions", since)
        results = _results(Path(ws.root) / "results" if uid else rt.cfg.output_path, since)
        for t in turns:
            t["user"] = uid or "(공용)"
        all_turns += turns
        all_results += [{**r, "user": uid or "(공용)"} for r in results]
        per_user[uid or "(공용)"] = {"runs": len(turns), "tokens": sum(int((t.get("usage") or {}).get("total_tokens") or 0) for t in turns),
                                     "sessions": len({t["session_id"] for t in turns}), "results": len(results)}
    status = Counter(t.get("status") or "?" for t in all_turns)
    kinds = Counter(t.get("kind") or ("skill" if t.get("session_skill") else "chat") for t in all_turns)
    skills = Counter((t.get("skill") or t.get("session_skill")) for t in all_turns if (t.get("skill") or t.get("session_skill")))
    def tname(x: str) -> str:
        x = x.split("(")[0]
        return x[len("builtin."):] if x.startswith("builtin.") else x

    tools = Counter(tname(x) for t in all_turns for x in t.get("tools") or [])
    tool_err = Counter(tname(x) for t in all_turns for x in t.get("tools") or [] if "(오류)" in x)
    tok = {k: sum(int((t.get("usage") or {}).get(k) or 0) for t in all_turns) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    res_tok = sum(int((r.get("usage") or {}).get("total_tokens") or 0) for r in all_results if not r["name"].startswith("skill_"))  # 스킬 실행은 turns 에 이미 있음
    elapsed = [float(t["elapsed"]) for t in all_turns if isinstance(t.get("elapsed"), (int, float))]
    by_day = Counter(datetime.fromtimestamp(t.get("time") or 0).strftime("%Y-%m-%d") for t in all_turns)
    files = [f for t in all_turns for f in t.get("files") or []]
    recent = sorted(all_turns, key=lambda t: t.get("time") or 0, reverse=True)
    jobs = Counter(r["name"] for r in all_results if not r["name"].startswith("skill_"))
    return {
        "days": days, "since": since, "users": per_user,
        "runs": len(all_turns), "sessions": len({(t["user"], t["session_id"]) for t in all_turns}),
        "status": dict(status), "kinds": dict(kinds), "skills": dict(skills.most_common()), "tools": dict(tools.most_common(15)),
        "tool_errors": dict(tool_err), "tokens": tok, "job_tokens": res_tok, "avg_elapsed": round(sum(elapsed) / len(elapsed), 1) if elapsed else None,
        "by_day": dict(sorted(by_day.items())), "files": len(files), "jobs": dict(jobs),
        "recent": [
            {"time": t.get("time"), "user": t["user"], "prompt": (t.get("prompt") or "")[:80], "status": t.get("status"), "kind": t.get("kind"),
             "tokens": int((t.get("usage") or {}).get("total_tokens") or 0), "elapsed": t.get("elapsed"), "session_id": t["session_id"],
             "files": len(t.get("files") or [])}
            for t in recent[:20]
        ],
    }


def format_usage(u: dict[str, Any], recent: int = 10) -> str:
    when = lambda ts: datetime.fromtimestamp(ts or 0).strftime("%m-%d %H:%M")  # noqa: E731
    who = ", ".join(u["users"]) or "-"
    lines = [f"## 에이전트 사용 이력 (최근 {u['days']:g}일, 대상: {who})",
             f"- 실행 {u['runs']}회 / 대화 {u['sessions']}개 / 만든 파일 {u['files']}개" + (f" / 평균 소요 {u['avg_elapsed']}초" if u["avg_elapsed"] is not None else ""),
             "- 결과: " + (", ".join(f"{k} {v}" for k, v in u["status"].items()) or "-"),
             "- 종류: " + (", ".join(f"{ {'chat': '대화', 'skill': '스킬', 'tool': '기능'}.get(k, k)} {v}" for k, v in u["kinds"].items()) or "-"),
             f"- 토큰: 합계 {u['tokens']['total_tokens']:,} (입력 {u['tokens']['prompt_tokens']:,} / 출력 {u['tokens']['completion_tokens']:,})"
             + (f" + 예약작업 {u['job_tokens']:,}" if u["job_tokens"] else "")]
    if len(u["users"]) > 1:
        lines.append("\n### 사용자별\n| 사용자 | 실행 | 대화 | 토큰 | 저장 결과 |\n|---|---:|---:|---:|---:|")
        lines += [f"| {k} | {v['runs']} | {v['sessions']} | {v['tokens']:,} | {v['results']} |" for k, v in sorted(u["users"].items(), key=lambda x: -x[1]["runs"])]
    if u["skills"]:
        lines.append("\n### 스킬별 실행\n" + ", ".join(f"{k} {v}회" for k, v in u["skills"].items()))
    if u["tools"]:
        lines.append("\n### 많이 쓴 도구\n" + ", ".join(f"{k} {v}" + (f"(오류 {u['tool_errors'][k]})" if u["tool_errors"].get(k) else "") for k, v in u["tools"].items()))
    if u["jobs"]:
        lines.append("\n### 예약작업 결과\n" + ", ".join(f"{k} {v}회" for k, v in u["jobs"].items()))
    if u["by_day"]:
        lines.append("\n### 날짜별 실행\n" + ", ".join(f"{k[5:]} {v}" for k, v in list(u["by_day"].items())[-14:]))
    if u["recent"]:
        lines.append(f"\n### 최근 실행 {min(recent, len(u['recent']))}건\n| 시각 | 사용자 | 요청 | 상태 | 토큰 |\n|---|---|---|---|---:|")
        lines += [f"| {when(r['time'])} | {r['user']} | {r['prompt'].replace('|', '/')} | {r['status']} | {r['tokens']:,} |" for r in u["recent"][:recent]]
    if not u["runs"] and not u["jobs"]:
        lines.append("\n(이 기간에 기록된 실행이 없습니다)")
    return "\n".join(lines)


async def list_models(rt: "AgentRuntime", remote: bool = True) -> dict[str, Any]:
    c = rt.cfg.llm
    ready, why = rt.llm_status()
    info: dict[str, Any] = {
        "provider": c.provider, "ready": ready, "status": why, "endpoint": c.base_url or c.endpoint, "deployment": c.deployment,
        "api_version": c.api_version, "auth": c.auth, "vision": c.vision, "max_output_tokens": c.max_output_tokens,
        "reasoning_effort": c.reasoning_effort, "models": [], "configured": [],
    }
    extra = (rt.cfg.raw.get("llm") or {}).get("models") or []
    for m in extra:  # 설정에 적어 둔 다른 배포(참고용)
        if isinstance(m, dict):
            info["configured"].append({k: m.get(k) for k in ("name", "deployment", "description", "vision")})
        else:
            info["configured"].append({"name": str(m), "deployment": str(m)})
    if remote and ready:
        try:
            client = getattr(rt.llm, "_client", None)
            if client is not None:
                page = await asyncio.wait_for(client.models.list(), 20)
                items = getattr(page, "data", None) or [x async for x in page]
                for m in items[:200]:
                    d = m.model_dump() if hasattr(m, "model_dump") else dict(m)
                    caps = d.get("capabilities") or {}
                    info["models"].append({"id": d.get("id"), "created": d.get("created_at") or d.get("created"),
                                           "chat": caps.get("chat_completion"), "lifecycle": d.get("lifecycle_status")})
        except Exception as e:  # noqa: BLE001 - 권한/게이트웨이에 따라 목록 API 가 막혀 있을 수 있음
            info["remote_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return info


def format_models(i: dict[str, Any]) -> str:
    lines = ["## 모델 목록",
             f"- 현재 사용 모델(배포): **{i['deployment'] or '(없음)'}** · provider {i['provider']} · 상태: {'연결 가능' if i['ready'] else '사용 불가'} ({i['status']})",
             f"- 엔드포인트: {i['endpoint'] or '-'} · api_version {i['api_version']} · 인증 {i['auth']} · 이미지 입력 {'지원' if i['vision'] else '미지원'}"
             + (f" · 최대 출력 {i['max_output_tokens']}" if i["max_output_tokens"] else "") + (f" · reasoning {i['reasoning_effort']}" if i["reasoning_effort"] else "")]
    if i["configured"]:
        lines.append("\n### 설정에 등록한 모델 (llm.models)")
        lines += [f"- {m.get('name')}: 배포 {m.get('deployment')}" + (f" - {m['description']}" if m.get("description") else "") for m in i["configured"]]
    if i["models"]:
        chat = [m for m in i["models"] if m.get("chat") is not False]
        lines.append(f"\n### Azure 리소스에서 쓸 수 있는 모델 ({len(i['models'])}개, 채팅 가능 {len(chat)}개)")
        lines += [f"- {m['id']}" + (f" ({m['lifecycle']})" if m.get("lifecycle") else "") for m in chat[:60]]
    elif i.get("remote_error"):
        lines.append(f"\n(Azure 모델 목록 조회 실패: {i['remote_error']} - 게이트웨이/권한에 따라 막혀 있을 수 있습니다)")
    elif not i["ready"]:
        lines.append("\n(LLM 이 연결되어 있지 않아 Azure 쪽 목록은 조회하지 않았습니다. 스킬 워크플로와 도구는 토큰 없이 동작합니다.)")
    return "\n".join(lines)


def make_usage_tools(rt: "AgentRuntime") -> list[BuiltinTool]:
    async def agent_usage(args: dict[str, Any]) -> ToolOutcome:
        days = float(args.get("days") or 30)
        me = current_user.get()
        target = str(args.get("user") or "").strip()
        if args.get("all_users") or (target and target != me):
            if not rt.can_edit_skills(me):
                return ToolOutcome("오류: 다른 사용자의 이력은 관리자(skills.editors)만 볼 수 있습니다.", True)
            users: list[str | None] = [target] if target else [*rt.users.list_users(), None]
        else:
            users = [me] if (me and rt.users_cfg.enabled) else [None]
        u = await asyncio.to_thread(collect_usage, rt, users, days)
        return ToolOutcome(format_usage(u, int(args.get("recent") or 10)), False, [], u)

    async def list_models_tool(args: dict[str, Any]) -> ToolOutcome:
        i = await list_models(rt, remote=args.get("remote", True) is not False)
        return ToolOutcome(format_models(i), False, [], i)

    return [
        BuiltinTool(
            "agent_usage",
            "에이전트 사용 이력을 조회합니다: 실행 횟수, 성공/실패, 스킬·도구별 사용, 토큰 사용량, 날짜별 추이, 최근 실행 목록. "
            "기본은 현재 사용자 본인. all_users=true 또는 user 지정은 관리자만.",
            {"type": "object", "properties": {
                "days": {"type": "number", "description": "최근 며칠 (기본 30)"},
                "user": {"type": "string", "description": "다른 사용자 ID (관리자만)"},
                "all_users": {"type": "boolean", "description": "모든 사용자 합계 (관리자만)"},
                "recent": {"type": "integer", "description": "최근 실행 몇 건 표시 (기본 10)"},
            }},
            agent_usage,
        ),
        BuiltinTool(
            "list_models",
            "에이전트가 쓰는 LLM 모델 목록을 봅니다: 현재 배포(deployment), 연결 상태, 이미지 지원 여부, 설정에 등록한 모델, Azure 리소스에서 쓸 수 있는 모델.",
            {"type": "object", "properties": {"remote": {"type": "boolean", "description": "Azure 에 모델 목록을 물어볼지 (기본 true)"}}},
            list_models_tool,
        ),
    ]
