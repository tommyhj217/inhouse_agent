"""웹 UI + REST API 서버 (FastAPI) + 예약작업.

- 조사(대화): 실행 과정(tool 호출, 파일 감지)을 실시간(SSE)으로 보여주고, ask_user 질문에 화면에서 답함
- 스킬: SKILL.md 편집 → 입력 폼 자동 생성 → 테스트 실행 → 결과 보고 수정 → 재실행
- 파일: 다운로드/업로드 파일 상태 점검·분석 (LLM 없이 즉시)
- 상태: MCP 서버 연결, 예약작업
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .config import AppConfig
from .runtime import AgentRuntime
from .scheduler import AgentScheduler
from .skills import SkillError

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover
    raise RuntimeError("serve 명령을 쓰려면 `pip install -e .[server]` 로 fastapi/uvicorn 을 설치하세요.") from e


SESSION_TTL = 60 * 60 * 6
ASK_TIMEOUT = 60 * 15


class ChatRequest(BaseModel):
    prompt: str
    session_id: str | None = None
    servers: list[str] | None = None


class RunRequest(BaseModel):
    prompt: str = ""
    session_id: str | None = None
    servers: list[str] | None = None
    skill: str | None = None
    inputs: dict[str, Any] | None = None
    new_session: bool = False


class AnswerRequest(BaseModel):
    answer: str


class SkillSaveRequest(BaseModel):
    content: str


class SkillCreateRequest(BaseModel):
    name: str
    description: str = ""
    content: str | None = None


@dataclass
class Session:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.time)
    skill: str | None = None
    inputs: dict[str, Any] | None = None
    servers: list[str] | None = None


@dataclass
class Run:
    id: str
    session_id: str
    kind: str  # chat | skill
    title: str
    status: str = "running"  # running | waiting_input | completed | max_iterations | error | cancelled
    events: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: float | None = None
    task: asyncio.Task[Any] | None = None
    question: dict[str, Any] | None = None
    answer_future: asyncio.Future[str] | None = None
    saved_to: str | None = None
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def push(self, kind: str, data: dict[str, Any]) -> None:
        async with self.cond:
            self.events.append({"seq": len(self.events) + 1, "type": kind, "time": time.time(), **data})
            self.cond.notify_all()

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "created": self.created,
            "finished": self.finished,
            "question": self.question,
            "saved_to": self.saved_to,
            "events": len(self.events),
        }


class RunManager:
    def __init__(self, rt: AgentRuntime):
        self.rt = rt
        self.sessions: dict[str, Session] = {}
        self.runs: dict[str, Run] = {}

    def _gc(self) -> None:
        now = time.time()
        for sid in [k for k, s in self.sessions.items() if now - s.touched > SESSION_TTL and not s.lock.locked()]:
            self.sessions.pop(sid, None)
        done = sorted((r for r in self.runs.values() if r.finished), key=lambda r: r.finished or 0)
        for r in done[:-200]:
            self.runs.pop(r.id, None)

    def session(self, sid: str | None) -> Session:
        sid = sid or uuid.uuid4().hex
        return self.sessions.setdefault(sid, Session(sid))

    async def start(self, req: RunRequest) -> Run:
        self._gc()
        sess = self.session(None if req.new_session else req.session_id)
        if req.skill:
            self.rt.skills.get(req.skill)  # 존재/형식 확인 (오류는 400)
            if sess.skill != req.skill or req.inputs is not None:
                # 다른 스킬이거나 입력을 새로 줬으면 새 대화로
                sess.messages = []
            sess.skill, sess.inputs = req.skill, req.inputs or sess.inputs or {}
        elif req.session_id is None:
            sess.skill = None
        if req.servers is not None:
            sess.servers = req.servers
        kind = "skill" if sess.skill else "chat"
        title = (req.prompt or f"[스킬] {sess.skill}")[:80]
        run = Run(uuid.uuid4().hex[:12], sess.id, kind, title)
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._execute(run, sess, req.prompt))
        return run

    async def _execute(self, run: Run, sess: Session, prompt: str) -> None:
        async def on_event(kind: str, data: dict[str, Any]) -> None:
            await run.push(kind, data)

        async def ask(question: str, options: list[str] | None, default: str | None) -> str:
            loop = asyncio.get_running_loop()
            run.answer_future = loop.create_future()
            run.question = {"question": question, "options": options or [], "default": default}
            run.status = "waiting_input"
            await run.push("ask", dict(run.question))
            try:
                answer = await asyncio.wait_for(run.answer_future, timeout=ASK_TIMEOUT)
            except asyncio.TimeoutError:
                answer = default or ""
                await run.push("answer", {"answer": answer, "timeout": True})
            finally:
                run.question = None
                run.answer_future = None
                run.status = "running"
            return answer

        await run.push("start", {"kind": run.kind, "prompt": prompt, "skill": sess.skill, "inputs": sess.inputs, "session_id": sess.id})
        try:
            async with sess.lock:
                sess.touched = time.time()
                if sess.skill:
                    result, path = await self.rt.run_skill(
                        sess.skill, sess.inputs, prompt, on_event=on_event, ask=ask,
                        messages=sess.messages if sess.messages else None, save=True,
                    )
                    if not sess.messages:
                        sess.messages = result.messages
                    run.saved_to = str(path) if path else None
                else:
                    if not sess.messages:
                        sess.messages = self.rt.agent.new_conversation(sess.servers)
                    result = await self.rt.agent.run(prompt, messages=sess.messages, servers=sess.servers, on_event=on_event, ask=ask)
            run.status = result.stopped_reason
            await run.push(
                "done",
                {
                    "status": result.stopped_reason,
                    "output": result.output,
                    "elapsed": round(result.elapsed, 1),
                    "usage": result.usage,
                    "files": result.files,
                    "skills_used": result.skills_used,
                    "saved_to": run.saved_to,
                },
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            await run.push("done", {"status": "cancelled", "output": "실행을 중단했습니다."})
        except SkillError as e:
            run.status = "error"
            await run.push("done", {"status": "error", "output": f"스킬 오류: {e}"})
        except Exception as e:  # noqa: BLE001
            run.status = "error"
            await run.push("done", {"status": "error", "output": f"오류: {type(e).__name__}: {e}"})
        finally:
            run.finished = time.time()

    async def stream(self, run: Run, after: int = 0):
        sent = after
        while True:
            async with run.cond:
                if len(run.events) <= sent and not run.finished:
                    try:
                        await asyncio.wait_for(run.cond.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                pending = run.events[sent:]
            if not pending:
                if run.finished:
                    return
                yield ": keepalive\n\n"
                continue
            for ev in pending:
                sent = ev["seq"]
                yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            if run.finished and sent >= len(run.events):
                return


def create_app(cfg: AppConfig, runtime: AgentRuntime | None = None, enable_scheduler: bool = True) -> FastAPI:
    rt = runtime or AgentRuntime(cfg)
    sched = AgentScheduler(rt)
    mgr = RunManager(rt)
    token = os.environ.get("AGENT_API_TOKEN", "")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await rt.start()
        if enable_scheduler:
            sched.register_all()
            sched.start()
        try:
            yield
        finally:
            sched.shutdown()
            for r in mgr.runs.values():
                if r.task and not r.task.done():
                    r.task.cancel()
            await rt.close()

    app = FastAPI(title="사내 자료수집 에이전트", lifespan=lifespan)
    app.state.runtime = rt
    app.state.runs = mgr

    def auth(authorization: str = Header(default=""), token_q: str | None = Query(default=None, alias="token")) -> None:
        if not token:
            return
        # EventSource/다운로드 링크는 헤더를 못 붙이므로 ?token= 도 허용
        if secrets.compare_digest(authorization, f"Bearer {token}") or (token_q and secrets.compare_digest(token_q, token)):
            return
        raise HTTPException(status_code=401, detail="인증 실패 (API 토큰 확인)")

    A = [Depends(auth)]

    # ---------------- 기본 ----------------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return resources.files("inhouse_agent").joinpath("web/index.html").read_text(encoding="utf-8")

    @app.get("/api/info", dependencies=A)
    async def info() -> dict[str, Any]:
        return {
            "deployment": cfg.llm.deployment,
            "workspace": str(rt.workspace.root),
            "watch_dirs": [str(d) for d in rt.workspace.watch_dirs],
            "skills_dir": str(rt.skills.root),
            "auth": bool(token),
        }

    # ---------------- 서버/tool ----------------
    @app.get("/api/servers", dependencies=A)
    async def servers() -> list[dict[str, Any]]:
        return rt.hub.server_status()

    @app.post("/api/servers/{name}/reconnect", dependencies=A)
    async def reconnect(name: str) -> dict[str, Any]:
        if name not in rt.hub.connections:
            raise HTTPException(404, f"서버 '{name}' 없음")
        try:
            conn = await rt.hub.reconnect(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"재연결 실패: {e}") from e
        return {"ok": True, "tools": [t.name for t in conn.tools]}

    @app.get("/api/tools", dependencies=A)
    async def tools() -> list[dict[str, Any]]:
        return [t["function"] for t in rt.hub.openai_tools()]

    # ---------------- 실행(대화/스킬) ----------------
    @app.post("/api/runs", dependencies=A)
    async def start_run(req: RunRequest) -> dict[str, Any]:
        if not req.prompt.strip() and not req.skill:
            raise HTTPException(400, "prompt 또는 skill 이 필요합니다.")
        try:
            run = await mgr.start(req)
        except SkillError as e:
            raise HTTPException(400, str(e)) from e
        return run.info()

    @app.get("/api/runs", dependencies=A)
    async def list_runs() -> list[dict[str, Any]]:
        return [r.info() for r in sorted(mgr.runs.values(), key=lambda r: r.created, reverse=True)[:100]]

    def _run(run_id: str) -> Run:
        run = mgr.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "실행을 찾을 수 없습니다.")
        return run

    @app.get("/api/runs/{run_id}", dependencies=A)
    async def get_run(run_id: str) -> dict[str, Any]:
        run = _run(run_id)
        return {**run.info(), "events": run.events}

    @app.get("/api/runs/{run_id}/events", dependencies=A)
    async def run_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        run = _run(run_id)
        last = request.headers.get("last-event-id")
        start = int(last) if last and last.isdigit() else after
        return StreamingResponse(
            mgr.stream(run, start),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/answer", dependencies=A)
    async def answer(run_id: str, req: AnswerRequest) -> dict[str, Any]:
        run = _run(run_id)
        if run.answer_future is None or run.answer_future.done():
            raise HTTPException(409, "지금은 답변을 기다리고 있지 않습니다.")
        run.answer_future.set_result(req.answer)
        await run.push("answer", {"answer": req.answer})
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel", dependencies=A)
    async def cancel(run_id: str) -> dict[str, Any]:
        run = _run(run_id)
        if run.task and not run.task.done():
            run.task.cancel()
        return {"ok": True}

    # 하위 호환: 동기 대화 API
    @app.post("/api/chat", dependencies=A)
    async def chat(req: ChatRequest) -> dict[str, Any]:
        run = await mgr.start(RunRequest(prompt=req.prompt, session_id=req.session_id, servers=req.servers))
        assert run.task is not None
        await asyncio.shield(run.task)
        done = next((e for e in reversed(run.events) if e["type"] == "done"), {})
        tools_used = [
            {k: e[k] for k in ("server", "tool", "arguments", "is_error", "elapsed")} for e in run.events if e["type"] == "tool_end"
        ]
        return {
            "session_id": run.session_id,
            "run_id": run.id,
            "output": done.get("output", ""),
            "status": done.get("status", run.status),
            "elapsed": done.get("elapsed"),
            "tools_used": tools_used,
            "files": done.get("files", []),
            "usage": done.get("usage", {}),
        }

    @app.delete("/api/chat/{session_id}", dependencies=A)
    async def reset(session_id: str) -> dict[str, Any]:
        mgr.sessions.pop(session_id, None)
        return {"ok": True}

    # ---------------- 스킬 ----------------
    @app.get("/api/skills", dependencies=A)
    async def skills_list() -> dict[str, Any]:
        skills, errors = rt.skills.list()
        return {"skills": [s.summary() for s in skills], "errors": errors}

    @app.get("/api/skills/{name}", dependencies=A)
    async def skill_get(name: str) -> dict[str, Any]:
        try:
            raw = rt.skills.raw(name)
        except (SkillError, OSError) as e:
            raise HTTPException(404, str(e)) from e
        try:
            parsed: dict[str, Any] | None = rt.skills.get(name).summary()
            err = None
        except SkillError as e:
            parsed, err = None, str(e)
        return {"name": name, "content": raw, "parsed": parsed, "error": err, "history": rt.skills.history(name)}

    @app.put("/api/skills/{name}", dependencies=A)
    async def skill_save(name: str, req: SkillSaveRequest) -> dict[str, Any]:
        try:
            sk = rt.skills.save(name, req.content)
        except SkillError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "parsed": sk.summary()}

    @app.post("/api/skills/validate", dependencies=A)
    async def skill_validate(req: SkillSaveRequest) -> dict[str, Any]:
        from .skills import parse_skill

        try:
            sk = parse_skill(req.content, rt.skills.root / "_validate")
            return {"ok": True, "parsed": {**sk.summary(), "files": []}}
        except SkillError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/skills", dependencies=A)
    async def skill_create(req: SkillCreateRequest) -> dict[str, Any]:
        try:
            sk = rt.skills.create(req.name, req.description, req.content)
        except SkillError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "parsed": sk.summary()}

    @app.delete("/api/skills/{name}", dependencies=A)
    async def skill_delete(name: str) -> dict[str, Any]:
        try:
            rt.skills.delete(name)
        except SkillError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

    @app.get("/api/skills/{name}/history/{fname}", dependencies=A)
    async def skill_history(name: str, fname: str) -> dict[str, Any]:
        if not re.fullmatch(r"SKILL_\d{8}_\d{6}\.md", fname):
            raise HTTPException(400, "잘못된 파일명")
        p = rt.skills.root / name / ".history" / fname
        if not p.is_file():
            raise HTTPException(404, "없음")
        return {"content": p.read_text(encoding="utf-8")}

    @app.get("/api/skills/{name}/results", dependencies=A)
    async def skill_results(name: str) -> list[dict[str, Any]]:
        d = cfg.output_path / f"skill_{name}"
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("2*.md"), reverse=True)[:30]:
            out.append({"file": p.name, "path": str(p), "size": p.stat().st_size, "preview": p.read_text(encoding="utf-8")[:4000]})
        return out

    # ---------------- 파일 ----------------
    @app.get("/api/files", dependencies=A)
    async def files_list(where: str = "all", pattern: str = "*", recent_minutes: float | None = None) -> list[dict[str, Any]]:
        out = []
        for p in rt.workspace.list_files(pattern, recent_minutes, where):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"path": str(p), "display": rt.workspace.display(p), "name": p.name, "size": st.st_size, "modified": st.st_mtime})
        return out

    @app.get("/api/files/status", dependencies=A)
    async def files_status(path: str) -> dict[str, Any]:
        from .files import FileAccessError, file_status

        try:
            p = rt.workspace.resolve(path)
        except FileAccessError as e:
            raise HTTPException(403, str(e)) from e
        return await file_status(p, rt.workspace)

    @app.get("/api/files/analyze", dependencies=A)
    async def files_analyze(path: str, sheet: str | None = None) -> dict[str, Any]:
        out = await rt.hub.call("analyze_file", {"path": path, **({"sheet": sheet} if sheet else {})})
        return {"text": out.text, "is_error": out.is_error, "files": out.files}

    @app.post("/api/files/upload", dependencies=A)
    async def files_upload(request: Request, name: str) -> dict[str, Any]:
        safe = re.sub(r'[\\/:*?"<>|]', "_", Path(name).name).strip() or "upload.bin"
        data = await request.body()
        if len(data) > rt.workspace.max_file_mb * 1024 * 1024:
            raise HTTPException(413, "파일이 너무 큽니다.")
        target = rt.workspace.uploads / safe
        if target.exists():
            target = target.with_name(f"{target.stem}_{time.strftime('%H%M%S')}{target.suffix}")
        target.write_bytes(data)
        return {"ok": True, "path": str(target), "display": rt.workspace.display(target)}

    @app.get("/api/files/download", dependencies=A)
    async def files_download(path: str) -> FileResponse:
        from .files import FileAccessError

        try:
            p = rt.workspace.resolve(path)
        except FileAccessError as e:
            raise HTTPException(403, str(e)) from e
        if not p.is_file():
            raise HTTPException(404, "파일 없음")
        return FileResponse(p, filename=p.name)

    # ---------------- 예약작업 ----------------
    @app.get("/api/jobs", dependencies=A)
    async def jobs() -> list[dict[str, Any]]:
        info = sched.jobs_info()
        for j in info:
            j["skill"] = next((s.skill for s in cfg.schedules if s.name == j["name"]), None)
        return info

    @app.post("/api/jobs/{name}/run", dependencies=A)
    async def run_job(name: str) -> dict[str, Any]:
        try:
            job = cfg.get_schedule(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404, str(e)) from e
        result, path = await rt.run_job(job)
        return {"status": result.stopped_reason, "output": result.output, "saved_to": str(path)}

    return app
