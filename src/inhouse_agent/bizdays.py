"""영업일 계산 (한국 공휴일 + 회사 휴무일).

- 공휴일: holidays 패키지(KR)로 설날·추석 같은 음력 공휴일, 대체공휴일, 선거일까지 계산
  (패키지가 없으면 양력 고정 공휴일만 쓰고 그 사실을 알려줌)
- 회사 설정 (config.yaml)
    calendar:
      holidays: ["2026-11-20 창립기념일", "2026-12-31"]   # 회사 휴무일 (공휴일 외)
      workdays: ["2026-10-10"]                            # 휴일이지만 근무하는 날
      weekend: [5, 6]                                     # 토·일 (월=0)
- 날짜 변수 {prev_business_day} {next_business_day} {this_month_first_business_day}
  {this_month_last_business_day} {last_month_last_business_day} (예약작업·스킬에서 사용)
- 예약작업 조건 business_days_only: true (휴일에는 건너뜀), business_day: first | last | 3 | -2 (그 달 n번째 영업일에만)
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

FIXED_KR = {(1, 1): "신정", (3, 1): "삼일절", (5, 5): "어린이날", (6, 6): "현충일", (8, 15): "광복절",
            (10, 3): "개천절", (10, 9): "한글날", (12, 25): "성탄절"}


def _parse_day(v: Any) -> tuple[date, str]:
    if isinstance(v, datetime):
        return v.date(), ""
    if isinstance(v, date):
        return v, ""
    m = re.match(r"^\s*(\d{4})[-./](\d{1,2})[-./](\d{1,2})\s*[:\s]?\s*(.*)$", str(v))
    if not m:
        raise ValueError(f"날짜 형식이 아닙니다 (YYYY-MM-DD): {v}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4).strip()


def to_date(v: Any, today: date | None = None) -> date:
    if v in (None, "", "today", "오늘"):
        return today or date.today()
    if str(v) in ("yesterday", "어제"):
        return (today or date.today()) - timedelta(days=1)
    return _parse_day(v)[0]


class BusinessCalendar:
    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.extra: dict[date, str] = {}
        for v in cfg.get("holidays") or []:
            d, name = _parse_day(v)
            self.extra[d] = name or "회사 휴무일"
        self.workdays = {_parse_day(v)[0] for v in cfg.get("workdays") or []}
        self.weekend = {int(x) for x in (cfg.get("weekend") if cfg.get("weekend") is not None else [5, 6])}
        self._cache: dict[int, dict[date, str]] = {}
        try:
            import holidays as _h  # noqa: F401

            self.precise = True
        except ImportError:
            self.precise = False

    def holidays(self, year: int) -> dict[date, str]:
        if year not in self._cache:
            out: dict[date, str] = {}
            if self.precise:
                import holidays

                out.update({d: str(n) for d, n in holidays.country_holidays("KR", years=year).items()})
            else:
                out.update({date(year, m, d): n for (m, d), n in FIXED_KR.items()})
            out.update({d: n for d, n in self.extra.items() if d.year == year})
            self._cache[year] = out
        return self._cache[year]

    def why_not(self, d: date) -> str | None:
        """영업일이 아니면 이유, 영업일이면 None."""
        if d in self.workdays:
            return None
        h = self.holidays(d.year).get(d)
        if h:
            return h
        if d.weekday() in self.weekend:
            return ["월", "화", "수", "목", "금", "토", "일"][d.weekday()] + "요일"
        return None

    def is_business_day(self, d: date) -> bool:
        return self.why_not(d) is None

    def add(self, d: date, n: int) -> date:
        """n 영업일 뒤(음수면 앞). n=0 이면 d 가 영업일이면 d, 아니면 다음 영업일."""
        if n == 0:
            while not self.is_business_day(d):
                d += timedelta(days=1)
            return d
        step = 1 if n > 0 else -1
        left = abs(n)
        while left:
            d += timedelta(days=step)
            if self.is_business_day(d):
                left -= 1
        return d

    def month_days(self, year: int, month: int) -> list[date]:
        d = date(year, month, 1)
        out = []
        while d.month == month:
            if self.is_business_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def count(self, start: date, end: date) -> int:
        if end < start:
            start, end = end, start
        return sum(1 for i in range((end - start).days + 1) if self.is_business_day(start + timedelta(days=i)))

    def nth_of_month(self, d: date, spec: Any) -> bool:
        """d 가 그 달의 spec(first/last/3/-2) 번째 영업일인지."""
        days = self.month_days(d.year, d.month)
        if not days:
            return False
        s = str(spec).strip().lower()
        idx = {"first": 1, "처음": 1, "첫": 1, "last": -1, "마지막": -1}.get(s)
        if idx is None:
            idx = int(s)
        if idx == 0 or abs(idx) > len(days):
            return False
        return days[idx - 1 if idx > 0 else idx] == d


_CAL: BusinessCalendar | None = None


def configure(cfg: dict[str, Any] | None) -> BusinessCalendar:
    global _CAL
    _CAL = BusinessCalendar(cfg)
    return _CAL


def calendar() -> BusinessCalendar:
    return _CAL or configure(None)


def date_vars(today: date) -> dict[str, str]:
    cal = calendar()
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    lm = cal.month_days(last_month_end.year, last_month_end.month)
    tm = cal.month_days(today.year, today.month)
    return {
        "prev_business_day": cal.add(today, -1).isoformat(),
        "next_business_day": cal.add(today, 1).isoformat(),
        "this_month_first_business_day": tm[0].isoformat() if tm else "",
        "this_month_last_business_day": tm[-1].isoformat() if tm else "",
        "last_month_last_business_day": lm[-1].isoformat() if lm else "",
    }


def make_business_day_tool(timezone: str) -> Any:
    from zoneinfo import ZoneInfo

    from .mcp_hub import BuiltinTool, ToolOutcome

    wk = ["월", "화", "수", "목", "금", "토", "일"]

    def fmt(d: date) -> str:
        return f"{d.isoformat()}({wk[d.weekday()]})"

    async def business_days(args: dict[str, Any]) -> ToolOutcome:
        cal = calendar()
        today = datetime.now(ZoneInfo(timezone)).date()
        op = str(args.get("op") or "check")
        try:
            d = to_date(args.get("date"), today)
            note = "" if cal.precise else "\n(holidays 패키지가 없어 양력 고정 공휴일만 반영 - 설날·추석·대체공휴일은 빠짐: pip install holidays)"
            if op == "check":
                why = cal.why_not(d)
                return ToolOutcome(f"{fmt(d)} 은(는) " + ("영업일입니다" if why is None else f"영업일이 아닙니다 ({why})")
                                   + f"\n전 영업일 {fmt(cal.add(d, -1))} · 다음 영업일 {fmt(cal.add(d, 1))}" + note, False, [],
                                   {"date": d.isoformat(), "business_day": why is None, "reason": why})
            if op in ("add", "prev", "next"):
                n = int(args.get("n") or (1 if op == "next" else -1 if op == "prev" else 0))
                if op == "prev":
                    n = -abs(n)
                r = cal.add(d, n)
                return ToolOutcome(f"{fmt(d)} 기준 {n:+d} 영업일 → {fmt(r)}" + note, False, [], {"date": r.isoformat()})
            if op == "count":
                s, e = to_date(args.get("start"), today), to_date(args.get("end"), today)
                c = cal.count(s, e)
                return ToolOutcome(f"{fmt(s)} ~ {fmt(e)} 영업일 {c}일 (양 끝 포함)" + note, False, [], {"count": c})
            if op in ("month", "first", "last"):
                ym = str(args.get("month") or today.strftime("%Y-%m"))
                y, m = int(ym[:4]), int(ym[5:7])
                days = cal.month_days(y, m)
                if op == "first":
                    return ToolOutcome(f"{ym} 첫 영업일: {fmt(days[0])}" + note, False, [], {"date": days[0].isoformat()})
                if op == "last":
                    return ToolOutcome(f"{ym} 마지막 영업일: {fmt(days[-1])}" + note, False, [], {"date": days[-1].isoformat()})
                hol = sorted((k, v) for k, v in cal.holidays(y).items() if k.month == m)
                return ToolOutcome(f"{ym} 영업일 {len(days)}일: " + ", ".join(f"{x.day}" for x in days)
                                   + ("\n휴일: " + ", ".join(f"{fmt(k)} {v}" for k, v in hol) if hol else "") + note, False, [],
                                   {"days": [x.isoformat() for x in days], "holidays": {k.isoformat(): v for k, v in hol}})
            if op == "holidays":
                y = int(args.get("year") or today.year)
                hol = sorted(cal.holidays(y).items())
                return ToolOutcome(f"{y}년 휴일 {len(hol)}일\n" + "\n".join(f"- {fmt(k)} {v}" for k, v in hol) + note, False, [],
                                   {k.isoformat(): v for k, v in hol})
        except (ValueError, IndexError) as e:
            return ToolOutcome(f"오류: {e}", True)
        return ToolOutcome("오류: op 는 check / add / prev / next / count / month / first / last / holidays 중 하나입니다.", True)

    return BuiltinTool(
        "business_days",
        "영업일 계산 (한국 공휴일·대체공휴일 + 회사 휴무일 반영). op: check(영업일인지), prev/next/add(n 영업일 전후), "
        "count(start~end 영업일 수), month(그 달 영업일·휴일 목록), first/last(그 달 첫·마지막 영업일), holidays(그해 휴일). "
        "'전 영업일 자료', '이번 달 마지막 영업일' 같은 요청에 사용.",
        {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["check", "add", "prev", "next", "count", "month", "first", "last", "holidays"]},
                "date": {"type": "string", "description": "기준일 YYYY-MM-DD (기본 오늘)"},
                "n": {"type": "integer", "description": "add: 영업일 수 (음수면 이전)"},
                "start": {"type": "string"}, "end": {"type": "string"},
                "month": {"type": "string", "description": "YYYY-MM (기본 이번 달)"},
                "year": {"type": "integer"},
            },
            "required": ["op"],
        },
        business_days,
    )
