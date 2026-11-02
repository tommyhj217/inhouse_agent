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
from .files import FileWorkspace
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
        self.workspace = FileWorkspace(
            cfg.workspace_path,
            watch_dirs=[cfg.resolve_path(d) for d in cfg.files.watch_dirs],
            extra_read_dirs=[cfg.output_path] + [cfg.resolve_path(d) for d in cfg.files.extra_read_dirs],
            max_file_mb=cfg.files.max_file_mb,
        )
        self.skills = SkillStore(cfg.skills_path)
        self.hub = MCPHub(
            cfg.mcp_servers,
            log_dir=cfg.log_path,
            builtins=make_builtin_tools(self.workspace, cfg.agent.timezone, self.skills),
            workspace=self.workspace,
            auto_detect=cfg.files.auto_detect,
        )
        self._agent: Agent | None = None
        self._job_locks: dict[str, asyncio.Lock] = {}

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
        return self._agent

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
                await asyncio.to_thread(_post_webhook, job.webhook_url, job.name, result, path)
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
    ) -> tuple[AgentResult, Path | None]:
        """스킬 1회 실행 (테스트용). 결과는 outputs/skill_<이름>/ 에 저장."""
        skill = self.skills.get(name)
        result = await self.agent.run(
            prompt, messages=messages, on_event=on_event, ask=ask, skill=skill, skill_inputs=inputs
        )
        path = None
        if save:
            label = prompt or f"[스킬 실행] {name} 입력값={json.dumps(inputs or {}, ensure_ascii=False)}"
            path = self.save_result(f"skill_{name}", label, result)
        return result, path

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
        out_dir = self.cfg.output_path / safe
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


def _post_webhook(url: str, name: str, result: AgentResult, path: Path) -> None:
    """Teams/Slack 호환 단순 웹훅({"text": ...}) 전송."""
    status = "성공" if result.success else f"실패({result.stopped_reason})"
    text = f"[{name}] 예약작업 {status}\n저장: {path}\n\n{result.output[:3500]}"
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
