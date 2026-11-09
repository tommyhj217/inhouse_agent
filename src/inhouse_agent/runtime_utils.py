"""순환 import 없이 여러 모듈에서 쓰는 작은 유틸."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BUSINESS_VARS = ("prev_business_day", "next_business_day", "this_month_first_business_day", "this_month_last_business_day",
                 "last_month_last_business_day")
DATE_VARS = ("today", "yesterday", "tomorrow", "now", "weekday", "this_month", "last_month", "week_start", "week_end", "last_week_start",
             "last_week_end", *BUSINESS_VARS)

_PLACEHOLDER = re.compile(r"\{(" + "|".join(DATE_VARS) + r")\}")


def render_dates(template: str, timezone: str, now: datetime | None = None) -> str:
    """프롬프트/스킬 본문의 {today}, {yesterday} 등 날짜 변수를 치환합니다."""
    now = now or datetime.now(ZoneInfo(timezone))
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    values = {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": ["월", "화", "수", "목", "금", "토", "일"][today.weekday()],
        "this_month": today.strftime("%Y-%m"),
        "last_month": last_month_end.strftime("%Y-%m"),
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "last_week_start": (week_start - timedelta(days=7)).isoformat(),
        "last_week_end": (week_start - timedelta(days=1)).isoformat(),
    }
    if any("{" + v + "}" in template for v in BUSINESS_VARS):  # 영업일 변수 (공휴일·회사 휴무일 반영)
        from .bizdays import date_vars

        values.update(date_vars(today))
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)
