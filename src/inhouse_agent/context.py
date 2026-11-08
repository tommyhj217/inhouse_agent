"""토큰 기준 자동 압축 (대화가 모델 한도에 가까워지면 줄임).

LLM 을 부르기 직전마다 대화 토큰을 계산해 한도(context_window)의 compact_at(기본 75%)을 넘으면:
  1단계 - 도구 결과 줄이기: 최근 차례를 뺀 오래된 도구 결과를 '앞부분 + 파일 경로' 로 바꿈
  2단계 - 요약: 그래도 크면 앞쪽 대화를 LLM 요약 한 개로 바꿈
           (현재 요청 원문은 그대로 남기고, 도구 호출과 결과의 짝은 깨지지 않게 '경계'에서만 자름)
토큰 수는 글자 수로 어림하고, 직전 LLM 호출에서 Azure 가 알려준 실제 입력 토큰으로 보정합니다.
(참고: Gemini CLI 는 한도 50% 에서 요약 + 오래된 도구 출력 가리기, Goose 는 80% 에서 압축)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

IMAGE_TOKENS = 1100
MASK_MIN_CHARS = 1500  # 이보다 짧은 도구 결과는 그대로 둠
_KEEP_LINE = re.compile(r"(📎|경로|path|저장|파일|https?://|\.(xlsx|xls|csv|pdf|pptx|docx|hwp|zip|json|png|md)\b|/files/|/downloads/|[A-Za-z]:\\)", re.I)
OVERFLOW_HINTS = ("context_length_exceeded", "maximum context length", "context length", "too many tokens", "prompt is too long",
                  "reduce the length", "token limit")


def estimate_text(s: str) -> int:
    """글자 수로 토큰 어림: 영문·숫자는 약 3.6자당 1토큰, 한글 등은 글자당 약 0.9토큰 (보수적으로)."""
    if not s:
        return 0
    ascii_n = sum(1 for ch in s if ord(ch) < 128)
    return int(ascii_n / 3.6 + (len(s) - ascii_n) * 0.9) + 1


def estimate_message(m: dict[str, Any]) -> int:
    n = 4
    c = m.get("content")
    if isinstance(c, str):
        n += estimate_text(c)
    elif isinstance(c, list):
        for p in c:
            if p.get("type") == "image_url":
                n += IMAGE_TOKENS
            else:
                n += estimate_text(str(p.get("text") or ""))
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += 8 + estimate_text(str(fn.get("name") or "")) + estimate_text(str(fn.get("arguments") or ""))
    return n


def estimate(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    n = sum(estimate_message(m) for m in messages)
    if tools:
        n += estimate_text(json.dumps(tools, ensure_ascii=False)) // 2  # 도구 설명은 압축률이 높음
    return n


def is_overflow_error(e: BaseException) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return any(h in s for h in OVERFLOW_HINTS)


def boundaries(messages: list[dict[str, Any]]) -> list[int]:
    """잘라도 되는 위치: system 다음의 user / assistant 메시지 앞 (tool 결과 앞에서는 자르지 않음)."""
    return [i for i, m in enumerate(messages) if i > 0 and m.get("role") in ("user", "assistant")]


def _render(m: dict[str, Any], limit: int) -> str:
    role = m.get("role")
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(str(p.get("text") or "[이미지]") for p in c)
    text = str(c or "")
    if len(text) > limit:
        text = text[: limit // 2] + f"\n…(중략 {len(text) - limit}자)…\n" + text[-limit // 2:]
    calls = "; ".join(f"{(tc.get('function') or {}).get('name')}({str((tc.get('function') or {}).get('arguments'))[:300]})"
                      for tc in m.get("tool_calls") or [])
    head = {"user": "사용자", "assistant": "에이전트", "tool": "도구 결과"}.get(str(role), str(role))
    return f"[{head}] {text}" + (f"\n  → 도구 호출: {calls}" if calls else "")


SUMMARY_PROMPT = """당신은 업무 에이전트의 작업 기록을 줄이는 요약기입니다. 아래 기록은 길어서 잘라낼 앞부분입니다.
에이전트가 이 요약만 보고 작업을 이어갈 수 있도록 한국어로 정리하세요. 꼭 남길 것:
1. 사용자가 요청한 내용과 조건 (날짜, 부서, 형식 등)
2. 지금까지 확인한 사실과 숫자 (표의 합계, 건수 등은 정확히)
3. 받거나 만든 파일의 전체 경로와 상태
4. 이미 한 일 / 실패한 시도와 원인 / 남은 일
추측을 더하지 말고, 기록에 없는 내용은 쓰지 마세요. 2000자 이내."""


@dataclass
class ContextManager:
    window: int = 128000
    compact_at: float = 0.75
    target: float = 0.5
    keep_recent: float = 0.2  # 요약하지 않고 남길 최근 대화 (한도 대비)
    ratio: float = 1.0  # 실제 토큰 / 어림값 (직전 호출로 보정)
    stats: dict[str, int] = field(default_factory=lambda: {"masked": 0, "summarized": 0})

    def calibrate(self, sent: list[dict[str, Any]], tools: list[dict[str, Any]] | None, prompt_tokens: int | None) -> None:
        if not prompt_tokens:
            return
        est = estimate(sent, tools)
        if est > 200:
            self.ratio = min(2.5, max(0.5, prompt_tokens / est))

    def tokens(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
        return int(estimate(messages, tools) * self.ratio)

    def needs(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> bool:
        return self.tokens(messages, tools) > self.window * self.compact_at

    # ---------------------------------------------------------------- 1단계
    def _tail_start(self, messages: list[dict[str, Any]], keep_tokens: int) -> int:
        """남길 최근 부분의 시작 위치 (경계). 최소한 마지막 경계부터는 남김."""
        bs = boundaries(messages)
        if not bs:
            return len(messages)
        start = bs[-1]
        acc = sum(estimate_message(m) for m in messages[start:]) * self.ratio
        for b in reversed(bs[:-1]):
            add = sum(estimate_message(m) for m in messages[b:start]) * self.ratio
            if acc + add > keep_tokens:
                break
            acc += add
            start = b
        return start

    def mask_tool_outputs(self, messages: list[dict[str, Any]], protect_from: int) -> int:
        n = 0
        for m in messages[:protect_from]:
            if m.get("role") != "tool" or m.get("_masked"):
                continue
            c = m.get("content")
            if not isinstance(c, str) or len(c) < MASK_MIN_CHARS:
                continue
            keep = [ln for ln in c.splitlines()[8:] if _KEEP_LINE.search(ln)][:12]
            m["content"] = (f"[자동 압축: 이전 도구 결과 {len(c):,}자 중 일부만 남김]\n{c[:700]}\n…"
                            + ("\n" + "\n".join(ln[:300] for ln in keep) if keep else "")
                            + "\n(전체가 필요하면 같은 도구를 다시 부르거나 파일을 다시 읽으세요)")
            m["_masked"] = True
            n += 1
        self.stats["masked"] += n
        return n

    # ---------------------------------------------------------------- 2단계
    async def summarize(self, llm: Any, messages: list[dict[str, Any]], keep_tokens: int) -> dict[str, Any] | None:
        """앞쪽 대화를 요약 한 개로 바꿈. 바꾼 내용(usage 포함)을 돌려주고, 줄일 게 없으면 None."""
        cut = self._tail_start(messages, keep_tokens)
        head = messages[1:cut]
        if not head:
            return None
        prompts = [i for i in range(1, len(messages)) if messages[i].get("role") == "user"
                   and not messages[i].get("_summary") and not messages[i].get("_auto")]
        current = prompts[-1] if prompts else None
        keep_prompt = current is not None and current < cut  # 현재 요청이 잘려 나가면 원문을 그대로 붙임
        per = max(800, min(6000, int(self.window * 0.4 * 3 / max(1, len(head)))))
        transcript = "\n\n".join(_render(m, per) for m in head)
        max_chars = int(self.window * 0.45 * 2.5)
        if len(transcript) > max_chars:
            transcript = transcript[: max_chars // 3] + "\n…(중략)…\n" + transcript[-(max_chars * 2) // 3:]
        resp = await llm.chat([{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": transcript}], None)
        summary = (resp.content or "").strip()
        if not summary:
            return None
        text = "[이전 대화 요약 - 대화가 길어져 앞부분을 자동으로 요약했습니다]\n" + summary
        if keep_prompt:
            c = messages[current].get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text") or "") for p in c)
            text += "\n\n[지금 처리 중인 요청 원문]\n" + str(c)
        messages[:] = [messages[0], {"role": "user", "content": text, "_summary": True}] + messages[cut:]
        self.stats["summarized"] += len(head)
        return {"summarized": len(head), "usage": getattr(resp, "usage", None) or {}}

    async def compact(self, llm: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                      force: bool = False, aggressive: bool = False) -> dict[str, Any] | None:
        """필요하면 압축하고 결과를 돌려줌 (안 했으면 None)."""
        before = self.tokens(messages, tools)
        if not force and before <= self.window * self.compact_at:
            return None
        keep = int(self.window * (self.keep_recent / 2 if aggressive else self.keep_recent))
        target = self.window * (self.target * 0.8 if aggressive else self.target)
        info: dict[str, Any] = {"before": before, "masked": 0, "summarized": 0, "usage": {}}
        info["masked"] = self.mask_tool_outputs(messages, self._tail_start(messages, keep))
        if force or self.tokens(messages, tools) > target:
            try:
                r = await self.summarize(llm, messages, keep)
            except Exception as e:  # noqa: BLE001 - 요약이 실패해도 작업은 계속 (1단계만 적용)
                log.warning("대화 요약 실패: %s", e)
                r = None
            if r:
                info["summarized"], info["usage"] = r["summarized"], r["usage"]
        info["after"] = self.tokens(messages, tools)
        if not info["masked"] and not info["summarized"]:
            return None
        return info
