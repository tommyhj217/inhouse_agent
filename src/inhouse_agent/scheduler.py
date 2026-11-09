"""config.yaml 의 schedules 를 cron 으로 등록해 주기적으로 실행합니다 (APScheduler 3.x)."""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import ScheduleConfig
from .runtime import AgentRuntime

log = logging.getLogger(__name__)


def make_trigger(job: ScheduleConfig, default_tz: str) -> CronTrigger:
    """5필드 crontab(분 시 일 월 요일) 문자열 → CronTrigger."""
    fields = job.cron.split()
    if len(fields) != 5:
        raise ValueError(f"예약작업 '{job.name}': cron 은 '분 시 일 월 요일' 5개 필드여야 합니다 (현재: {job.cron!r})")
    return CronTrigger.from_crontab(job.cron, timezone=job.timezone or default_tz)


class AgentScheduler:
    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime
        self.scheduler = AsyncIOScheduler(timezone=runtime.cfg.agent.timezone)

    def register_all(self) -> list[ScheduleConfig]:
        registered = []
        for job in self.runtime.cfg.schedules:
            if not job.enabled:
                log.info("예약작업 '%s' 비활성화 상태 - 건너뜀", job.name)
                continue
            self.scheduler.add_job(
                self._execute,
                trigger=make_trigger(job, self.runtime.cfg.agent.timezone),
                args=[job.name],
                id=job.name,
                name=job.name,
                max_instances=1,  # 이전 실행이 안 끝났으면 겹쳐 실행하지 않음
                coalesce=True,  # 밀린 실행은 1회로 합침
                misfire_grace_time=600,
                replace_existing=True,
            )
            registered.append(job)
        return registered

    def skip_reason(self, job: ScheduleConfig, today: Any = None) -> str | None:
        """영업일 조건 때문에 오늘 건너뛰어야 하면 이유."""
        if not job.business_days_only and not job.business_day:
            return None
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from .bizdays import calendar

        cal = calendar()
        today = today or datetime.now(ZoneInfo(job.timezone or self.runtime.cfg.agent.timezone)).date()
        why = cal.why_not(today)
        if why is not None:
            return f"휴일({why})"
        if job.business_day and not cal.nth_of_month(today, job.business_day):
            return f"이번 달 {job.business_day} 영업일이 아님"
        return None

    async def _execute(self, name: str) -> None:
        job = self.runtime.cfg.get_schedule(name)
        why = self.skip_reason(job)
        if why:
            log.info("예약작업 '%s' 건너뜀: %s", name, why)
            print(f"[예약작업] {name}: 건너뜀 - {why}", flush=True)
            return
        try:
            result, path = await self.runtime.run_job(job)
            print(f"[예약작업] {name}: {result.stopped_reason} → {path}", flush=True)
        except Exception:  # noqa: BLE001
            log.exception("예약작업 '%s' 실행 실패", name)

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def jobs_info(self) -> list[dict[str, Any]]:
        info = []
        scheduled = {j.id: j for j in self.scheduler.get_jobs()} if self.scheduler.running else {}
        for s in self.runtime.cfg.schedules:
            j = scheduled.get(s.name)
            nrt = getattr(j, "next_run_time", None) if j else None
            info.append(
                {
                    "name": s.name,
                    "cron": s.cron,
                    "enabled": s.enabled,
                    "servers": s.servers,
                    "description": s.description,
                    "business_days_only": s.business_days_only,
                    "business_day": s.business_day,
                    "next_run": nrt.isoformat() if nrt else None,
                }
            )
        return info
