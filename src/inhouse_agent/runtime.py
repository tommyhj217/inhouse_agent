"""설정 → LLM / MCP 허브 / 에이전트 조립, 작업 실행 결과 저장, 알림."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import re
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .agent import Agent, AgentResult, EventCallback
from .builtins import make_builtin_tools
from .config import AppConfig, ScheduleConfig
from .downloader import DownloadConfig, Downloader
from .dynamic_tools import DynamicToolManager, DynamicToolsConfig
from .files import FileWorkspace
from .links import LinkSigner
from .toolkit import ToolkitConfig, make_toolkit
from .users import SessionStore, UserWorkspaces, UsersConfig, WorkspaceProxy, current_user
from .interaction import AskFn
from .llm import AzureChatModel, ChatModel
from .mcp_hub import MCPHub
from .runtime_utils import render_dates
from .skills import SkillStore

log = logging.getLogger(__name__)


def setup_logging(cfg: AppConfig, level: str = "INFO", console: bool = True) -> None:
    cfg.log_path.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.handlers.TimedRotatingFileHandler(
        cfg.log_path / "agent.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logging.WARNING if level.upper() == "INFO" else level.upper())
        root.addHandler(ch)
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "mcp", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


render_prompt = render_dates


class AgentRuntime:
    """CLI / 스케줄러 / API 서버가 공통으로 쓰는 실행 환경."""

    def __init__(self, cfg: AppConfig, llm: ChatModel | None = None):
        self.cfg = cfg
        self._llm: ChatModel | None = llm
        self.start_results: dict[str, str | None] = {}
        # 원격 MCP 의 path_map 이 가리키는 공유폴더도 다운로드 감시/읽기 대상에 포함
        from .remote_files import RemoteFilesConfig

        shared = [t for s in cfg.mcp_servers.values() if s.enabled for t in RemoteFilesConfig.from_dict(s.remote_files).local_targets()]
        self.base_workspace = FileWorkspace(
            cfg.workspace_path,
            watch_dirs=[cfg.resolve_path(d) for d in cfg.files.watch_dirs] + [cfg.resolve_path(str(t)) for t in shared],
            extra_read_dirs=[cfg.output_path] + [cfg.resolve_path(d) for d in cfg.files.extra_read_dirs],
            max_file_mb=cfg.files.max_file_mb,
        )
        # 사용자별 작업폴더: 도구들은 proxy 를 통해 '실행 중인 사용자'의 폴더를 씀
        self.users_cfg = UsersConfig.from_dict(cfg.raw.get("users"))
        self.users = UserWorkspaces(self.base_workspace, self.users_cfg)
        self.workspace: Any = WorkspaceProxy(self.users)
        self.skills = SkillStore(cfg.skills_path)
        self.downloader = Downloader(DownloadConfig.from_dict(cfg.downloads), self.workspace)
        dyn_cfg = DynamicToolsConfig.from_dict(cfg.dynamic_tools)
        self.dynamic = DynamicToolManager(dyn_cfg, cfg.resolve_path(dyn_cfg.dir), self.workspace)
        self.toolkit_cfg = ToolkitConfig.from_raw(cfg.raw)
        self.links = LinkSigner(self.workspace, self.toolkit_cfg.public_url, self.toolkit_cfg.link_expire_hours)
        builtin_tools = make_builtin_tools(
            self.workspace, cfg.agent.timezone, self.skills, self.downloader, self.dynamic, wait_timeout=cfg.files.wait_timeout
        )
        builtin_tools += make_toolkit(self.workspace, self.downloader, self.links, self.toolkit_cfg, cfg.agent.timezone)
        self.hub = MCPHub(
            cfg.mcp_servers,
            log_dir=cfg.log_path,
            builtins=builtin_tools,
            workspace=self.workspace,
            auto_detect=cfg.files.auto_detect,
        )
        self.hub.downloader = self.downloader
        if any(c.is_remote for c in self.hub.connections.values()):
            self.hub.builtins.append(self._fetch_remote_tool())
        self.dynamic.hub = self.hub
        if dyn_cfg.mode != "off":
            self.dynamic.register_all()  # 저장해 둔 동적 도구 등록
        self._agent: Agent | None = None
        self._job_locks: dict[str, asyncio.Lock] = {}

    def _fetch_remote_tool(self) -> Any:
        """원격 MCP 서버 PC 의 파일을 에이전트 PC 로 가져오는 tool (자동 처리가 안 됐거나 나중에 생긴 파일용)."""
        from .files import file_status, format_status
        from .mcp_hub import BuiltinTool, ToolOutcome
        from .remote_files import RemoteFileFetcher

        remotes = [n for n, c in self.hub.connections.items() if c.is_remote]
        max_wait = self.cfg.files.wait_timeout

        async def fetch_remote_file(args: dict[str, Any]) -> ToolOutcome:
            name = str(args.get("server") or "")
            conn = self.hub.connections.get(name)
            if conn is None or not conn.is_remote:
                return ToolOutcome(f"오류: 원격 MCP 서버가 아닙니다: {name} (원격 서버: {', '.join(remotes)})", True)
            wait = min(float(args.get("wait_sec") or 0), max(max_wait, 1800))
            deadline = asyncio.get_running_loop().time() + wait
            tries = 0
            while True:
                tries += 1
                p, how = await RemoteFileFetcher(self.hub).fetch(conn, str(args["path"]), explicit=True)
                if p is not None or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(3)  # 원격에서 아직 받는 중일 수 있음
            if p is None:
                return ToolOutcome(f"오류: 가져오지 못했습니다 ({tries}회 시도): {how}", True)
            st = await file_status(p, self.workspace, stable_wait=0)
            text = f"원격 파일을 가져왔습니다: {args['path']}\n→ {p} ({how})\n{format_status(st)}"
            try:
                text += f"\n다운로드 링크: {self.links.url(p)}"
            except Exception:  # noqa: BLE001
                pass
            return ToolOutcome(text, False, [st])

        return BuiltinTool(
            "fetch_remote_file",
            "원격 MCP 서버 PC 에 있는 파일(결과에 나온 서버 경로)을 에이전트 PC 로 가져옵니다. "
            f"원격 서버: {', '.join(remotes)}. 원격에서 아직 다운로드 중이면 wait_sec 로 최대 대기 시간을 주세요.",
            {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "enum": remotes},
                    "path": {"type": "string", "description": "원격 서버 기준 파일 경로 또는 file:// URI"},
                    "wait_sec": {"type": "number", "description": f"파일이 생길 때까지 재시도할 최대 초 (기본 0 = 1회, 권장 최대 {max_wait:.0f})"},
                },
                "required": ["server", "path"],
            },
            fetch_remote_file,
        )

    @property
    def llm(self) -> ChatModel:
        # LLM 설정이 없어도 tools/check 등 일부 명령은 동작하도록 지연 생성
        if self._llm is None:
            self._llm = AzureChatModel(self.cfg.llm)
        return self._llm

    @property
    def agent(self) -> Agent:
        if self._agent is None:
            self._agent = Agent(self.llm, self.hub, self.cfg.agent, self.skills)
            self._agent.recent_sessions_in_prompt = self.users_cfg.recent_sessions_in_prompt
        return self._agent

    def sessions(self) -> SessionStore:
        """현재 사용자(없으면 공용)의 대화 세션 저장소."""
        return SessionStore(self.workspace)

    def results_path(self) -> Path:
        return Path(self.workspace.root) / "results" if self.workspace.is_user else self.cfg.output_path

    async def __aenter__(self) -> "AgentRuntime":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> dict[str, str | None]:
        self.start_results = await self.hub.start()
        return self.start_results

    async def close(self) -> None:
        await self.hub.close()
        aclose = getattr(self._llm, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    async def run_job(self, job: ScheduleConfig, on_event: EventCallback | None = None) -> tuple[AgentResult, Path]:
        """예약작업 1건 실행 → 결과 파일 저장 → (선택) 웹훅 알림."""
        if job.user:  # 지정한 사용자의 작업폴더에서 실행
            token = current_user.set(self.users.check(job.user))
            try:
                return await self._run_job(job, on_event)
            finally:
                current_user.reset(token)
        return await self._run_job(job, on_event)

    async def _run_job(self, job: ScheduleConfig, on_event: EventCallback | None = None) -> tuple[AgentResult, Path]:
        lock = self._job_locks.setdefault(job.name, asyncio.Lock())
        async with lock:
            tz = job.timezone or self.cfg.agent.timezone
            started_at = datetime.now(ZoneInfo(tz))
            prompt = render_prompt(job.prompt, tz, started_at)
            log.info("예약작업 '%s' 시작", job.name)
            extra = (
                "이 요청은 예약작업으로 자동 실행되었습니다. 사용자에게 추가 질문을 할 수 없으니 "
                "합리적인 기본값으로 판단해서 끝까지 수행하고 결과를 정리하세요."
            )
            skill = self.skills.get(job.skill) if job.skill else None
            result = await self.agent.run(
                prompt, servers=job.servers, extra_system=extra, on_event=on_event, skill=skill, skill_inputs=job.inputs
            )
            path = self.save_result(job.name, prompt, result, started_at, job.output_format)
            log.info("예약작업 '%s' 종료 (%s, %.1fs) → %s", job.name, result.stopped_reason, result.elapsed, path)
            if job.webhook_url:
                await asyncio.to_thread(_post_webhook, job.webhook_url, job.name, result, path, self.file_links(result, path))
            return result, path

    async def run_skill(
        self,
        name: str,
        inputs: dict[str, Any] | None = None,
        prompt: str = "",
        *,
        on_event: EventCallback | None = None,
        ask: AskFn | None = None,
        messages: list[dict[str, Any]] | None = None,
        save: bool = True,
        images: list[str] | None = None,
    ) -> tuple[AgentResult, Path | None]:
        """스킬 1회 실행 (테스트용). 결과는 outputs/skill_<이름>/ (사용자면 그 사용자의 results/) 에 저장."""
        skill = self.skills.get(name)
        result = await self.agent.run(
            prompt, messages=messages, on_event=on_event, ask=ask, skill=skill, skill_inputs=inputs, images=images
        )
        path = None
        if save:
            label = prompt or f"[스킬 실행] {name} 입력값={json.dumps(inputs or {}, ensure_ascii=False)}"
            path = self.save_result(f"skill_{name}", label, result)
        return result, path

    def file_links(self, result: AgentResult, saved: Path | None = None) -> list[tuple[str, str]]:
        """결과 파일들의 (이름, 다운로드 링크). public_url 이 없으면 빈 목록 (외부 공유 불가)."""
        if not self.links.public_url:
            return []
        out = []
        paths = [Path(f["path"]) for f in result.files if f.get("path")] + ([saved] if saved else [])
        for p in dict.fromkeys(paths):
            try:
                out.append((p.name, self.links.url(p)))
            except Exception:  # noqa: BLE001
                continue
        return out

    def save_result(
        self,
        name: str,
        prompt: str,
        result: AgentResult,
        started_at: datetime | None = None,
        fmt: str = "md",
    ) -> Path:
        started_at = started_at or datetime.now(ZoneInfo(self.cfg.agent.timezone))
        safe = re.sub(r"[^\w.-]", "_", name)
        out_dir = self.results_path() / safe
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = started_at.strftime("%Y%m%d_%H%M%S")
        ext = {"md": "md", "txt": "txt", "json": "json"}.get(fmt, "md")
        path = out_dir / f"{stamp}.{ext}"

        meta = {
            "name": name,
            "started_at": started_at.isoformat(),
            "status": result.stopped_reason,
            "elapsed_sec": round(result.elapsed, 1),
            "iterations": result.iterations,
            "usage": result.usage,
            "tools_used": [f"{e.server}.{e.tool}" + (" (오류)" if e.is_error else "") for e in result.tool_events],
            "skills_used": result.skills_used,
            "files": [
                {k: f.get(k) for k in ("path", "state", "detected_type", "size", "issues")} for f in result.files
            ],
        }
        if ext == "json":
            path.write_text(
                json.dumps({**meta, "prompt": prompt, "output": result.output}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        elif ext == "txt":
            path.write_text(result.output, encoding="utf-8")
        else:
            lines = [
                f"# {name}",
                "",
                f"- 실행 시각: {meta['started_at']}",
                f"- 상태: {meta['status']} / 소요: {meta['elapsed_sec']}s / 반복: {meta['iterations']}",
                f"- 사용 tool: {', '.join(meta['tools_used']) or '-'}",
                *([f"- 사용 스킬: {', '.join(result.skills_used)}"] if result.skills_used else []),
                *([f"- 파일: {f['path']} [{f['state']}]" for f in meta["files"]]),
                "",
                "## 요청",
                "",
                prompt,
                "",
                "## 결과",
                "",
                result.output,
                "",
            ]
            path.write_text("\n".join(lines), encoding="utf-8")

        # 디버깅용 전체 실행 기록 (tool 입출력 포함)
        trace_dir = out_dir / "_trace"
        trace_dir.mkdir(exist_ok=True)
        (trace_dir / f"{stamp}.json").write_text(
            json.dumps(
                {**meta, "prompt": prompt, "tool_events": [asdict(e) for e in result.tool_events], "messages": result.messages},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        latest = out_dir / f"latest.{ext}"
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        return path


def _post_webhook(url: str, name: str, result: AgentResult, path: Path, links: list[tuple[str, str]] | None = None) -> None:
    """Teams/Slack 호환 단순 웹훅({"text": ...}) 전송."""
    status = "성공" if result.success else f"실패({result.stopped_reason})"
    text = f"[{name}] 예약작업 {status}\n저장: {path}\n\n{result.output[:3500]}"
    if links:
        text += "\n\n[파일 받기]\n" + "\n".join(f"- {n}: {u}" for n, u in links)
    req = urllib.request.Request(
        url,
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - 설정에 있는 사내 웹훅만 호출
            r.read()
    except Exception as e:  # noqa: BLE001
        log.warning("웹훅 전송 실패(%s): %s", name, e)
