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
from .users import UserError, current_user

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
    images: list[str] | None = None  # /api/images/upload 로 올린 이미지 경로
    mode: str = "auto"  # auto(LLM 있으면 에이전트, 없으면 워크플로) | agent | workflow
    tool: str | None = None  # 도구(기능) 직접 실행 - LLM 불필요
    args: dict[str, Any] | None = None


class RulebookCreate(BaseModel):
    name: str
    description: str = ""
    domain: str = ""


class RulebookLearn(BaseModel):
    paths: list[str]
    label: str = "exemplar"
    product: str | None = None


class RulebookValidate(BaseModel):
    path: str
    label: str = "target"
    product: str | None = None
    include_candidates: bool = False


class RuleChange(BaseModel):
    changes: dict[str, Any]
    reason: str = ""


class RuleAdd(BaseModel):
    type: str
    params: dict[str, Any] = {}
    statement: str
    rationale: str = ""
    severity: str = "should"
    status: str = "candidate"


class SessionPatch(BaseModel):
    title: str


class AnswerRequest(BaseModel):
    answer: str


class SkillSaveRequest(BaseModel):
    content: str


class ToolSaveRequest(BaseModel):
    description: str
    parameters: dict[str, Any] | None = None
    code: str


class ToolTestRequest(BaseModel):
    args: dict[str, Any] = {}
    code: str | None = None  # 저장 전 편집 중인 코드로 테스트


class LinksRequest(BaseModel):
    paths: list[str]
    expire_hours: float | None = None


class ScanRequest(BaseModel):
    text: str


class FetchRequest(BaseModel):
    url: str
    cookies: str | None = None
    filename: str | None = None


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
    user: str | None = None


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
    user: str | None = None
    mode: str = "auto"
    tool: str | None = None
    args: dict[str, Any] | None = None
    skill_start: bool = False  # 이번 요청이 스킬 실행 자체인지 (아니면 스킬 대화에 이어서 하는 말)
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
        self.sessions: dict[tuple[str | None, str], Session] = {}  # (사용자, 세션ID)
        self.runs: dict[str, Run] = {}

    def _gc(self) -> None:
        now = time.time()
        for key in [k for k, s in self.sessions.items() if now - s.touched > SESSION_TTL and not s.lock.locked()]:
            self.sessions.pop(key, None)  # 디스크에 저장돼 있으므로 다음에 다시 불러옴
        done = sorted((r for r in self.runs.values() if r.finished), key=lambda r: r.finished or 0)
        for r in done[:-200]:
            self.runs.pop(r.id, None)

    def session(self, sid: str | None) -> Session:
        """현재 사용자의 세션. 메모리에 없으면 저장된 세션을 불러와 이어서 진행."""
        user = current_user.get()
        store = self.rt.sessions()
        sid = sid or store.new_id()
        key = (user, sid)
        sess = self.sessions.get(key)
        if sess is None:
            sess = Session(sid, user=user)
            if store.exists(sid):
                d = store.load(sid)
                sess.messages = d.get("messages") or []
                sess.skill, sess.inputs, sess.servers = d.get("skill"), d.get("inputs"), d.get("servers")
            self.sessions[key] = sess
        return sess

    def get_run(self, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        return run if run is not None and run.user == current_user.get() else None  # 다른 사용자 실행은 안 보임

    def _check_images(self, paths: list[str] | None) -> list[str]:
        from .images import is_image

        out = []
        for p in paths or []:
            rp = self.rt.workspace.resolve(p)
            if not rp.is_file() or not is_image(rp):
                raise SkillError(f"이미지 파일이 아닙니다: {p}")
            out.append(str(rp))
        return out[:10]

    async def start(self, req: RunRequest) -> Run:
        self._gc()
        images = self._check_images(req.images)
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
        if req.mode not in ("auto", "agent", "workflow"):
            raise SkillError("mode 는 auto / agent / workflow 중 하나입니다.")
        if req.tool:
            if self.rt.hub.route_of(req.tool) is None:
                raise SkillError(f"알 수 없는 도구입니다: {req.tool}")
            kind, title = "tool", f"/{req.tool} " + json.dumps(req.args or {}, ensure_ascii=False)[:60]
        else:
            if req.mode == "agent" and not self.rt.llm_status()[0]:
                raise SkillError(f"LLM 을 쓸 수 없습니다: {self.rt.llm_status()[1]}")
            kind = "skill" if sess.skill else "chat"
            title = (req.prompt or f"[스킬] {sess.skill}")[:80]
        run = Run(uuid.uuid4().hex[:12], sess.id, kind, title, user=sess.user, mode=req.mode, tool=req.tool, args=req.args, skill_start=bool(req.skill))
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._execute(run, sess, req.prompt, images))
        return run

    async def _execute(self, run: Run, sess: Session, prompt: str, images: list[str] | None = None) -> None:
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

        await run.push("start", {"kind": run.kind, "prompt": prompt, "skill": sess.skill if run.kind != "tool" else None, "inputs": sess.inputs, "tool": run.tool, "mode": run.mode,
                                 "session_id": sess.id, "images": images or [], "user": sess.user})
        result = None
        try:
            async with sess.lock:
                sess.touched = time.time()
                ready, why = self.rt.llm_status()
                if run.kind == "tool":  # 도구 직접 실행 (LLM/토큰 불필요)
                    step = {"id": "t", "tool": run.tool, "args": run.args or {}, "on_error": "continue", "title": run.tool}
                    result = await self.rt.run_workflow([step], {}, title=f"/{run.tool}", on_event=on_event, ask=ask)
                    sess.messages = (sess.messages or []) + result.messages
                elif sess.skill and (run.skill_start or ready):  # LLM 이 없으면 스킬 대화의 후속 말은 워크플로를 다시 돌리지 않음
                    result, path = await self.rt.run_skill(
                        sess.skill, sess.inputs, prompt, on_event=on_event, ask=ask,
                        messages=sess.messages if sess.messages else None, save=True, images=images, mode=run.mode,
                    )
                    if not sess.messages:
                        sess.messages = result.messages
                    elif result.messages and result.messages is not sess.messages:
                        sess.messages = sess.messages + result.messages  # 워크플로 결과도 대화에 이어 붙임
                    run.saved_to = str(path) if path else None
                elif not ready:
                    from .agent import AgentResult

                    msg = (f"LLM 이 연결되어 있지 않아(사유: {why}) 자유 질문에는 답할 수 없습니다.\n\n"
                           "토큰 없이 쓰는 방법:\n- 입력창에 **/** 를 치면 스킬과 기능(도구) 목록이 나옵니다.\n"
                           "- 워크플로(steps)가 있는 스킬과 모든 도구는 LLM 없이 바로 실행됩니다.")
                    await on_event("final", {"content": msg})
                    result = AgentResult(msg, [], stopped_reason="completed")
                else:
                    if not sess.messages:
                        sess.messages = self.rt.agent.new_conversation(sess.servers)
                    result = await self.rt.agent.run(
                        prompt, messages=sess.messages, servers=sess.servers, on_event=on_event, ask=ask, images=images
                    )
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
            self._persist(run, sess, prompt, images, result)

    def _persist(self, run: Run, sess: Session, prompt: str, images: list[str] | None, result: Any) -> None:
        """대화를 사용자 작업폴더에 저장 → 나중에 목록에서 골라 이어서 진행."""
        done = next((e for e in reversed(run.events) if e["type"] == "done"), {})
        turn = {
            "time": time.time(),
            "prompt": prompt or (f"/{run.tool} {json.dumps(run.args or {}, ensure_ascii=False)}" if run.kind == "tool" else
                                 f"/{sess.skill} {json.dumps(sess.inputs or {}, ensure_ascii=False)}" if sess.skill else ""),
            "images": images or [],
            "status": run.status,
            "output": done.get("output", ""),
            "tools": [f"{e['server']}.{e['tool']}" + ("(오류)" if e.get("is_error") else "") for e in run.events if e["type"] == "tool_end"],
            "files": [f["path"] for f in (done.get("files") or []) if f.get("path")],
            "saved_to": run.saved_to,
            "run_id": run.id,
        }
        try:
            token = current_user.set(sess.user)
            try:
                self.rt.sessions().save(sess.id, sess.messages, turn=turn, skill=sess.skill, inputs=sess.inputs, servers=sess.servers)
            finally:
                current_user.reset(token)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception("세션 저장 실패: %s", sess.id)

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


def _disposition(name: str, inline: bool = False) -> str:
    """RFC 6266: 구형 클라이언트용 ASCII 이름 + 한글 이름(filename*) 함께."""
    import urllib.parse

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem.encode("ascii", "ignore").decode()).strip("_.") or "download"
    ascii_ext = re.sub(r"[^A-Za-z0-9]+", "", ext)
    ascii_name = f"{ascii_stem}.{ascii_ext}" if ascii_ext else ascii_stem
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


class UserMiddleware:
    """요청의 사용자 ID(X-Agent-User 헤더 또는 ?user=)를 current_user 로 설정 (순수 ASGI - 백그라운드 실행까지 전달됨)."""

    def __init__(self, app: Any, runtime: AgentRuntime):
        self.app = app
        self.rt = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import urllib.parse

        headers = {k.decode().lower(): v.decode("utf-8", "replace") for k, v in scope.get("headers", [])}
        raw = headers.get("x-agent-user") or ""
        if raw:
            raw = urllib.parse.unquote(raw)  # 한글 ID 는 헤더에 인코딩해서 보냄
        if not raw:
            q = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
            raw = (q.get("user") or [""])[0]
        path = scope.get("path", "")
        uid = None
        if raw and self.rt.users_cfg.enabled:
            try:
                uid = self.rt.users.check(raw)
            except UserError as e:
                await _json_error(send, 400, str(e))
                return
        elif self.rt.users_cfg.require and path.startswith("/api/") and path not in ("/api/info",):
            await _json_error(send, 401, "사용자 ID 가 필요합니다 (화면 오른쪽 위에 입력).")
            return
        token = current_user.set(uid)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user.reset(token)


async def _json_error(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


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
    app.add_middleware(UserMiddleware, runtime=rt)
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
            "public_url": rt.links.public_url,
            "users": {"enabled": rt.users_cfg.enabled, "require": rt.users_cfg.require},
            "llm": {"ready": rt.llm_status()[0], "reason": rt.llm_status()[1]},
            "user": current_user.get(),
            "auth": bool(token),
        }

    # ---------------- / 명령 (스킬·도구) ----------------
    @app.get("/api/commands", dependencies=A)
    async def commands() -> dict[str, Any]:
        """채팅창 / 메뉴: 등록된 스킬 + 도구(기능) + 내장 명령. LLM 없이 실행 가능한지도 표시."""
        ready, why = rt.llm_status()
        skills, errors = rt.skills.list()
        items: list[dict[str, Any]] = [
            {"kind": "command", "name": "new", "description": "새 대화 시작", "offline": True},
            {"kind": "command", "name": "help", "description": "/ 명령 사용법", "offline": True},
        ]
        for sk in skills:
            items.append({"kind": "skill", "name": sk.name, "description": sk.description, "inputs": [i.to_dict() for i in sk.inputs],
                          "workflow": bool(sk.steps), "steps": len(sk.steps), "offline": bool(sk.steps)})
        for t in rt.hub.openai_tools():
            f = t["function"]
            items.append({"kind": "tool", "name": f["name"], "description": f["description"], "parameters": f["parameters"], "offline": True})
        return {"llm": {"ready": ready, "reason": why}, "items": items, "skill_errors": errors}

    # ---------------- 사용자 / 세션 ----------------
    @app.get("/api/me", dependencies=A)
    async def me() -> dict[str, Any]:
        user = current_user.get()
        ws = rt.workspace
        return {
            "user": user,
            "workspace": str(ws.root),
            "users_enabled": rt.users_cfg.enabled,
            "require": rt.users_cfg.require,
            "sessions": len(rt.sessions().list(1000)),
        }

    @app.get("/api/sessions", dependencies=A)
    async def sessions_list(q: str = "") -> list[dict[str, Any]]:
        items = rt.sessions().list(300)
        if q:
            ql = q.lower()
            items = [i for i in items if ql in i["title"].lower() or ql in i["last_output"].lower()]
        return items

    @app.get("/api/sessions/{sid}", dependencies=A)
    async def session_get(sid: str) -> dict[str, Any]:
        try:
            d = rt.sessions().load(sid)
        except UserError as e:
            raise HTTPException(404, str(e)) from e
        d.pop("messages", None)  # 화면에는 대화 기록(turns)만
        return d

    @app.patch("/api/sessions/{sid}", dependencies=A)
    async def session_rename(sid: str, req: SessionPatch) -> dict[str, Any]:
        try:
            rt.sessions().rename(sid, req.title)
        except UserError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @app.delete("/api/sessions/{sid}", dependencies=A)
    async def session_delete(sid: str) -> dict[str, Any]:
        try:
            rt.sessions().delete(sid)
        except UserError as e:
            raise HTTPException(400, str(e)) from e
        mgr.sessions.pop((current_user.get(), sid), None)
        return {"ok": True}

    @app.post("/api/images/upload", dependencies=A)
    async def image_upload(request: Request, name: str = "image.png") -> dict[str, Any]:
        """채팅 이미지 첨부 (붙여넣기/끌어놓기/선택) → 사용자 uploads/images/ 에 저장."""
        from .images import IMAGE_EXT

        data = await request.body()
        if not data:
            raise HTTPException(400, "빈 파일입니다.")
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, "이미지는 20MB 이하만 올릴 수 있습니다.")
        stem = re.sub(r'[\\/:*?"<>|\s]+', "_", Path(name).stem).strip("_") or "image"
        ext = Path(name).suffix.lower()
        if ext not in IMAGE_EXT:
            ext = ".png"
        folder = Path(rt.workspace.uploads) / "images"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{time.strftime('%Y%m%d_%H%M%S')}_{stem[:40]}{ext}"
        target.write_bytes(data)
        from .images import ImageError, image_part

        try:
            await asyncio.to_thread(image_part, target)  # 실제 이미지인지 확인
        except ImageError as e:
            target.unlink(missing_ok=True)
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "path": str(target), "display": rt.workspace.display(target), "name": target.name}

    # ---------------- 규칙집 ----------------
    from .rulebook import RulebookError, RulebookStore

    def _rb(name: str):
        return RulebookStore(rt.rules_path).get(name)

    def _rbcall(fn, *a, **k):
        async def go():
            try:
                return await asyncio.to_thread(fn, *a, **k)
            except (RulebookError, PermissionError) as e:
                raise HTTPException(400, str(e)) from e
        return go()

    @app.get("/api/rulebooks", dependencies=A)
    async def rb_list() -> list[dict[str, Any]]:
        return RulebookStore(rt.rules_path).list()

    @app.post("/api/rulebooks", dependencies=A)
    async def rb_create(req: RulebookCreate) -> dict[str, Any]:
        await _rbcall(_rb(req.name).create, req.description, req.domain, current_user.get())
        return {"ok": True}

    @app.get("/api/rulebooks/{name}", dependencies=A)
    async def rb_get(name: str) -> dict[str, Any]:
        d = await _rbcall(_rb(name).load)
        return d

    @app.post("/api/rulebooks/{name}/learn", dependencies=A)
    async def rb_learn(name: str, req: RulebookLearn) -> dict[str, Any]:
        from .rules_tools import DOC_EXT

        files = []
        for p in req.paths:
            try:
                rp = rt.workspace.resolve(p)
            except PermissionError as e:
                raise HTTPException(403, str(e)) from e
            files += sorted(x for x in rp.rglob("*") if x.is_file() and x.suffix.lower() in DOC_EXT) if rp.is_dir() else [rp]
        r = await _rbcall(_rb(name).learn, files, req.label, current_user.get(), req.product)
        return {k: r[k] for k in ("added", "relabeled", "errors", "exemplars", "new_candidates", "status_changes", "new_sections")}

    @app.post("/api/rulebooks/{name}/validate", dependencies=A)
    async def rb_validate(name: str, req: RulebookValidate) -> dict[str, Any]:
        try:
            path = rt.workspace.resolve(req.path)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return await _rbcall(_rb(name).validate, path, label=req.label, product=req.product, user=current_user.get(), include_candidates=req.include_candidates)

    @app.get("/api/rulebooks/{name}/validations", dependencies=A)
    async def rb_validations(name: str) -> list[dict[str, Any]]:
        return await _rbcall(_rb(name).validations, 100)

    @app.get("/api/rulebooks/{name}/validations/{vid}", dependencies=A)
    async def rb_validation(name: str, vid: str) -> dict[str, Any]:
        return await _rbcall(_rb(name).validation, vid)

    @app.post("/api/rulebooks/{name}/rules", dependencies=A)
    async def rb_add_rule(name: str, req: RuleAdd) -> dict[str, Any]:
        return await _rbcall(_rb(name).add_rule, req.model_dump(), current_user.get(), "user")

    @app.patch("/api/rulebooks/{name}/rules/{rid}", dependencies=A)
    async def rb_update_rule(name: str, rid: str, req: RuleChange) -> dict[str, Any]:
        return await _rbcall(_rb(name).update_rule, rid, req.changes, req.reason or "화면에서 수정", current_user.get())

    @app.get("/api/rulebooks/{name}/reflect", dependencies=A)
    async def rb_reflect(name: str) -> dict[str, Any]:
        return await _rbcall(_rb(name).reflect)

    @app.get("/api/rulebooks/{name}/changelog", dependencies=A)
    async def rb_changelog(name: str) -> list[dict[str, Any]]:
        return await _rbcall(_rb(name).changelog, 200)

    @app.get("/api/rulebooks/{name}/products", dependencies=A)
    async def rb_products(name: str) -> list[dict[str, Any]]:
        return await _rbcall(_rb(name).products)

    @app.post("/api/rulebooks/{name}/export", dependencies=A)
    async def rb_export(name: str) -> dict[str, Any]:
        out = await rt.hub.call("rulebook_export", {"name": name})
        if out.is_error:
            raise HTTPException(400, out.text)
        return {"text": out.text, "files": out.files}

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
        if not req.prompt.strip() and not req.skill and not req.tool:
            raise HTTPException(400, "prompt, skill 또는 tool 이 필요합니다.")
        try:
            run = await mgr.start(req)
        except (SkillError, UserError, PermissionError) as e:
            raise HTTPException(400, str(e)) from e
        return run.info()

    @app.get("/api/runs", dependencies=A)
    async def list_runs() -> list[dict[str, Any]]:
        me = current_user.get()
        return [r.info() for r in sorted(mgr.runs.values(), key=lambda r: r.created, reverse=True) if r.user == me][:100]

    def _run(run_id: str) -> Run:
        run = mgr.get_run(run_id)
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
        mgr.sessions.pop((current_user.get(), session_id), None)
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
        d = rt.results_path() / f"skill_{name}"
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

    def _send_path(p: Path, inline: bool = False) -> FileResponse:
        """파일이면 그대로, 폴더면 zip 으로 묶어서 보냄."""
        if p.is_dir():
            import tempfile
            import zipfile

            from starlette.background import BackgroundTask

            tmp = Path(tempfile.mkstemp(suffix=".zip", dir=rt.workspace.root)[1])
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(p.rglob("*")):
                    if f.is_file() and not f.name.startswith("."):
                        z.write(f, f"{p.name}/{f.relative_to(p)}")
            return FileResponse(tmp, headers={"Content-Disposition": _disposition(f"{p.name}.zip")},
                                media_type="application/zip", background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))
        if not p.is_file():
            raise HTTPException(404, "파일 없음")
        import mimetypes

        return FileResponse(p, headers={"Content-Disposition": _disposition(p.name, inline)},
                            media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    @app.get("/api/files/download", dependencies=A)
    async def files_download(path: str, inline: bool = False) -> FileResponse:
        from .files import FileAccessError

        try:
            p = rt.workspace.resolve(path)
        except FileAccessError as e:
            raise HTTPException(403, str(e)) from e
        return _send_path(p, inline)

    @app.post("/api/files/links", dependencies=A)
    async def files_links(req: LinksRequest, request: Request) -> list[dict[str, Any]]:
        """화면에 나온 경로들 → 실제 파일인지 확인하고 다운로드/공유 링크 생성."""
        from .links import LinkError

        base = rt.links.public_url or str(request.base_url).rstrip("/")
        out = []
        for raw in req.paths[:200]:
            item: dict[str, Any] = {"input": raw, "ok": False}
            try:
                info = rt.links.describe(raw, req.expire_hours)
                rel = info["url"] if not info["absolute"] else info["url"][len(rt.links.public_url):]
                item.update(ok=True, path=info["path"], name=info["name"], is_dir=info["is_dir"], size=info["size"],
                            href=rel, share_url=base + rel, expires_in_hours=info["expires_in_hours"])
            except (LinkError, PermissionError, ValueError, OSError) as e:
                item["error"] = str(e)
            out.append(item)
        return out

    @app.post("/api/files/scan", dependencies=A)
    async def files_scan(req: ScanRequest, request: Request) -> list[dict[str, Any]]:
        """텍스트(답변/tool 결과)에 들어 있는 실제 파일 경로를 찾아 다운로드 링크와 함께 돌려줌.
        파일명에 공백이 있어도 되도록, 정규식 대신 실제 파일 목록과 대조합니다."""
        text = req.text[:300_000]
        if not text.strip():
            return []
        norm = text.replace("\\", "/")
        cands: list[Path] = list(rt.workspace.list_files("*"))
        out_dir = rt.results_path()
        if out_dir.is_dir():
            cands += sorted((p for p in out_dir.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)[:300]
        roots = [rt.workspace.root, *rt.workspace.watch_dirs, out_dir]
        dirs: list[Path] = []
        for p in cands:
            for parent in p.parents:
                if parent in roots or not any(r in parent.parents for r in roots):
                    break
                if parent not in dirs:
                    dirs.append(parent)
        cands += dirs
        found: dict[str, Path] = {}
        for p in cands:
            forms = {str(p), str(p).replace("\\", "/"), rt.workspace.display(p)}
            for f in forms:
                if len(f) > 4 and (f in text or f in norm):
                    found.setdefault(f, p)
        # 긴 경로가 그 일부(짧은 상대경로)보다 우선
        keys = sorted(found, key=len, reverse=True)
        chosen: list[str] = []
        rest = norm  # 긴 경로부터 지워 가며, 짧은 경로가 '따로' 나온 경우만 채택
        for k in keys:
            kk = k.replace("\\", "/")
            if kk in rest:
                chosen.append(k)
                rest = rest.replace(kk, "\0")
        base = rt.links.public_url or str(request.base_url).rstrip("/")
        res = []
        for k in chosen:
            try:
                info = rt.links.describe(found[k])
            except Exception:  # noqa: BLE001
                continue
            rel = info["url"][len(rt.links.public_url):] if info["absolute"] else info["url"]
            # 화면에 역슬래시 경로로 나와도 찾을 수 있도록 원래 표기도 함께
            variants = [k] + ([k.replace("/", "\\")] if "/" in k and k.replace("/", "\\") in text else [])
            for v in variants:
                res.append({"match": v, "path": info["path"], "name": info["name"], "is_dir": info["is_dir"], "size": info["size"],
                            "href": rel, "share_url": base + rel, "expires_in_hours": info["expires_in_hours"]})
        return res

    @app.get("/d/{token}/{name}")
    @app.get("/d/{token}")
    async def signed_download(token: str, name: str = "", inline: bool = False) -> FileResponse:
        """서명 링크 (API 토큰 불필요, 기한 있음) - 메일/메신저 공유용."""
        from .links import LinkError

        try:
            p = rt.links.verify(token)
        except LinkError as e:
            raise HTTPException(403, str(e)) from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return _send_path(p, inline)

    @app.post("/api/files/fetch", dependencies=A)
    async def files_fetch(req: FetchRequest) -> dict[str, Any]:
        args: dict[str, Any] = {"url": req.url}
        if req.cookies:
            args["cookies"] = req.cookies
        if req.filename:
            args["filename"] = req.filename
        out = await rt.hub.call("download_url", args)
        return {"text": out.text, "is_error": out.is_error, "files": out.files}

    # ---------------- 동적 도구 ----------------
    from .dynamic_tools import DEFAULT_CODE, ToolCodeError, check_code

    @app.get("/api/ctools", dependencies=A)
    async def ctools_list() -> dict[str, Any]:
        tools, errors = rt.dynamic.list()
        return {"mode": rt.dynamic.cfg.mode, "tools": [t.summary() for t in tools], "errors": errors, "template": DEFAULT_CODE}

    @app.get("/api/ctools/{name}", dependencies=A)
    async def ctools_get(name: str) -> dict[str, Any]:
        try:
            t = rt.dynamic.load(name)
        except (ToolCodeError, OSError) as e:
            raise HTTPException(404, str(e)) from e
        return {**t.summary(), "code": t.code}

    @app.put("/api/ctools/{name}", dependencies=A)
    async def ctools_save(name: str, req: ToolSaveRequest) -> dict[str, Any]:
        # 사람이 화면에서 직접 저장 → 승인 절차 없이 저장 (정적 검사는 적용)
        try:
            t = rt.dynamic.save(name, req.description, req.parameters, req.code, created_by="user")
        except ToolCodeError as e:
            raise HTTPException(400, str(e)) from e
        return t.summary()

    @app.post("/api/ctools/{name}/check", dependencies=A)
    async def ctools_check(name: str, req: ToolTestRequest) -> dict[str, Any]:
        problems = check_code(req.code or "", rt.dynamic.cfg, require_run=True)
        return {"ok": not problems, "problems": problems}

    @app.post("/api/ctools/{name}/test", dependencies=A)
    async def ctools_test(name: str, req: ToolTestRequest) -> dict[str, Any]:
        try:
            if req.code is not None:
                r = await rt.dynamic.test_draft(name, req.code, req.args)
            else:
                r = await rt.dynamic.execute("tool", rt.dynamic.load(name).path, req.args)
        except ToolCodeError as e:
            raise HTTPException(400, str(e)) from e
        return {**r, "text": rt.dynamic.format_result(r)}

    @app.delete("/api/ctools/{name}", dependencies=A)
    async def ctools_delete(name: str) -> dict[str, Any]:
        try:
            rt.dynamic.delete(name)
        except ToolCodeError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

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
