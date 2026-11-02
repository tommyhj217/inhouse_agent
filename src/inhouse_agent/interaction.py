"""실행 중 사용자와의 상호작용(ask_user) 연결 고리.

Agent.run 이 실행마다 RunContext 를 ContextVar 에 넣어 두면, 내장 tool(ask_user)이
현재 실행의 UI(웹/CLI)로 질문을 보내고 답을 기다릴 수 있습니다.
예약작업처럼 사람이 없는 실행에서는 ask 가 None 이라 기본값으로 진행합니다.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

AskFn = Callable[[str, "list[str] | None", "str | None"], Awaitable[str]]


@dataclass
class RunContext:
    ask: AskFn | None = None
    interactive: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("current_run", default=None)
