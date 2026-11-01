"""HTTP API 서버 (FastAPI) + 스케줄러 동시 실행.

사용자는 웹 화면(/) 또는 REST API(/api/chat)로 명령을 내리고,
같은 프로세스에서 예약작업이 돌아갑니다.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from .config import AppConfig
from .runtime import AgentRuntime
from .scheduler import AgentScheduler

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover
    raise RuntimeError("serve 명령을 쓰려면 `pip install -e .[server]` 로 fastapi/uvicorn 을 설치하세요.") from e


class ChatRequest(BaseModel):
    prompt: str
    session_id: str | None = None
    servers: list[str] | None = None


class _Session:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self.touched = time.time()


SESSION_TTL = 60 * 60 * 6


def create_app(cfg: AppConfig, runtime: AgentRuntime | None = None, enable_scheduler: bool = True) -> FastAPI:
    rt = runtime or AgentRuntime(cfg)
    sched = AgentScheduler(rt)
    sessions: dict[str, _Session] = {}
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
            await rt.close()

    app = FastAPI(title="사내 자료수집 에이전트", lifespan=lifespan)

    def auth(authorization: str = Header(default="")) -> None:
        if token and not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="인증 실패")

    def _gc() -> None:
        now = time.time()
        for sid in [k for k, s in sessions.items() if now - s.touched > SESSION_TTL]:
            sessions.pop(sid, None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/servers", dependencies=[Depends(auth)])
    async def servers() -> list[dict[str, Any]]:
        return rt.hub.server_status()

    @app.post("/api/servers/{name}/reconnect", dependencies=[Depends(auth)])
    async def reconnect(name: str) -> dict[str, Any]:
        if name not in rt.hub.connections:
            raise HTTPException(404, f"서버 '{name}' 없음")
        try:
            conn = await rt.hub.reconnect(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"재연결 실패: {e}") from e
        return {"ok": True, "tools": [t.name for t in conn.tools]}

    @app.get("/api/tools", dependencies=[Depends(auth)])
    async def tools() -> list[dict[str, Any]]:
        return [t["function"] for t in rt.hub.openai_tools()]

    @app.post("/api/chat", dependencies=[Depends(auth)])
    async def chat(req: ChatRequest) -> dict[str, Any]:
        _gc()
        sid = req.session_id or uuid.uuid4().hex
        sess = sessions.setdefault(sid, _Session())
        async with sess.lock:
            sess.touched = time.time()
            if not sess.messages:
                sess.messages = rt.agent.new_conversation(req.servers)
            result = await rt.agent.run(req.prompt, messages=sess.messages, servers=req.servers)
        return {
            "session_id": sid,
            "output": result.output,
            "status": result.stopped_reason,
            "elapsed": round(result.elapsed, 1),
            "tools_used": [
                {"server": e.server, "tool": e.tool, "arguments": e.arguments, "is_error": e.is_error, "elapsed": round(e.elapsed, 2)}
                for e in result.tool_events
            ],
            "usage": result.usage,
        }

    @app.delete("/api/chat/{session_id}", dependencies=[Depends(auth)])
    async def reset(session_id: str) -> dict[str, Any]:
        sessions.pop(session_id, None)
        return {"ok": True}

    @app.get("/api/jobs", dependencies=[Depends(auth)])
    async def jobs() -> list[dict[str, Any]]:
        return sched.jobs_info()

    @app.post("/api/jobs/{name}/run", dependencies=[Depends(auth)])
    async def run_job(name: str) -> dict[str, Any]:
        try:
            job = cfg.get_schedule(name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(404, str(e)) from e
        result, path = await rt.run_job(job)
        return {"status": result.stopped_reason, "output": result.output, "saved_to": str(path)}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    return app


_INDEX_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>사내 자료수집 에이전트</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--card:#fff;--muted:#6b7280;--accent:#2563eb;--line:#e5e7eb}
@media (prefers-color-scheme: dark){:root{--bg:#111318;--fg:#e6e8ec;--card:#1a1d24;--muted:#9aa1ad;--accent:#60a5fa;--line:#2a2f3a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,'Malgun Gothic',sans-serif}
main{max-width:900px;margin:0 auto;padding:16px;display:flex;flex-direction:column;height:100vh}
#log{flex:1;overflow:auto;display:flex;flex-direction:column;gap:10px;padding-bottom:12px}
.m{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;white-space:pre-wrap;word-break:break-word}
.u{align-self:flex-end;border-color:var(--accent)} .t{color:var(--muted);font-size:13px}
form{display:flex;gap:8px}textarea{flex:1;min-height:56px;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit}
button{padding:0 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}button:disabled{opacity:.5}
header{display:flex;justify-content:space-between;align-items:center}input{padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
</style></head><body><main>
<header><h3>사내 자료수집 에이전트</h3><span><input id="tok" type="password" placeholder="API 토큰(설정 시)"> <button id="new" type="button">새 대화</button></span></header>
<div id="log"></div>
<form id="f"><textarea id="p" placeholder="예) A 사이트에서 어제 등록된 공지사항 목록 가져와서 표로 정리해줘"></textarea><button id="b">전송</button></form>
</main><script>
let sid=null;const log=document.getElementById('log');
function add(t,c){const d=document.createElement('div');d.className='m '+c;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
document.getElementById('new').onclick=()=>{sid=null;log.innerHTML=''};
document.getElementById('p').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.getElementById('f').requestSubmit()}});
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const p=document.getElementById('p'),b=document.getElementById('b');const q=p.value.trim();if(!q)return;
add(q,'u');p.value='';b.disabled=true;const w=add('작업 중...','t');
try{const h={'Content-Type':'application/json'};const tk=document.getElementById('tok').value;if(tk)h['Authorization']='Bearer '+tk;
const r=await fetch('/api/chat',{method:'POST',headers:h,body:JSON.stringify({prompt:q,session_id:sid})});const j=await r.json();
if(!r.ok)throw new Error(j.detail||r.status);sid=j.session_id;
w.textContent=j.tools_used.length?('사용 tool: '+j.tools_used.map(t=>t.server+'.'+t.tool+(t.is_error?'(오류)':'')).join(', ')+' · '+j.elapsed+'s'):(j.elapsed+'s');
add(j.output,'a')}catch(err){w.textContent='오류: '+err.message}finally{b.disabled=false}};
</script></body></html>"""
