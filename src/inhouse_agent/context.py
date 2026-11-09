"""토큰 기준 자동 압축 (대화가 모델 한도에 가까워지면 줄임).

LLM 을 부르기 직전마다 대화 토큰을 계산해 한도(context_window)의 compact_at(기본 75%)을 넘으면:
  1단계 - 도구 결과 줄이기: 최근 차례를 뺀 오래된 도구 결과를 '앞부분 + 파일 경로' 로 바꿈
  2단계 - 요약: 그래도 크면 앞쪽 대화를 LLM 요약 한 개로 바꿈
           (현재 요청 원문은 그대로 남기고, 도구 호출과 결과의 짝은 깨지지 않게 '경계'에서만 자름)
           요약 양식은 pi 방식: 목표 / 조건·선호 / 진행(완료·진행 중·막힘) / 주요 결정 / 다음 할 일 / 꼭 필요한 정보
           + 기계적으로 모은 파일 목록. 두 번째 압축부터는 이전 요약을 따로 넘겨 '갱신' 시킴 (요약의 요약으로 잘리지 않게)
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


TEXT_CHARS = 4000  # 요약 기록에서 사용자·에이전트 글 하나의 최대 글자 (조건이 잘리지 않게 넉넉히)
TOOL_CHARS = 2000  # 도구 결과 하나의 최대 글자 (pi 와 같음 - 큰 결과가 요약 요청을 키우는 주범)
FILE_LIST_MAX = 40
_PATH_KEY = re.compile(r"(^|_)(path|paths|file|files|output|template|src|source|dest|target|out)$", re.I)
_PATH_VALUE = re.compile(r"([/\\]|\.(xlsx|xlsm|xls|csv|pdf|pptx|docx|txt|json|md|png|jpg|zip|py)$)", re.I)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n…(중략 {len(text) - limit:,}자)…\n" + text[-limit // 2:]


def _render(m: dict[str, Any], limit: int = TEXT_CHARS) -> str:
    """요약할 기록 한 줄. 도구 결과는 TOOL_CHARS, 사용자·에이전트 글은 limit 까지."""
    role = m.get("role")
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(str(p.get("text") or "[이미지]") for p in c)
    text = _clip(str(c or ""), min(limit, TOOL_CHARS) if role == "tool" else limit)
    calls = "; ".join(f"{(tc.get('function') or {}).get('name')}({str((tc.get('function') or {}).get('arguments'))[:300]})"
                      for tc in m.get("tool_calls") or [])
    head = {"user": "사용자", "assistant": "에이전트", "tool": "도구 결과"}.get(str(role), str(role))
    if m.get("_auto"):
        head = "시스템 안내"
    return f"[{head}] {text}" + (f"\n[에이전트 도구 호출] {calls}" if calls else "")


def _arg_paths(value: Any) -> list[str]:
    """도구 인자에서 파일 경로로 보이는 값 (path / file / output / template … 키)."""
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if _PATH_KEY.search(str(k)):
                for x in v if isinstance(v, list) else [v]:
                    if isinstance(x, str) and 2 < len(x) < 400 and "\n" not in x and _PATH_VALUE.search(x):
                        out.append(x)
            elif isinstance(v, (dict, list)):
                out += _arg_paths(v)
    elif isinstance(value, list):
        for v in value:
            out += _arg_paths(v)
    return out


def collect_files(messages: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """(받거나 만든 파일, 도구에 넘긴 파일) - 앞선 요약에 적어 둔 목록까지 누적 (pi 의 read/modified 파일 목록 방식).
    받거나 만든 파일: 도구 결과 메시지의 _files (다운로드 감지·저장한 파일), 사용한 파일: 도구 호출 인자의 경로."""
    made: list[str] = []
    used: list[str] = []
    for m in messages:
        made += m.get("_files") or []
        used += m.get("_used_files") or []
        for tc in m.get("tool_calls") or []:
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except (ValueError, TypeError):
                continue
            used += _arg_paths(args)
    made = list(dict.fromkeys(made))
    used = [p for p in dict.fromkeys(used) if p not in made]
    return made[-FILE_LIST_MAX:], used[-FILE_LIST_MAX:]


SUMMARY_PROMPT = """당신은 업무 에이전트의 작업 기록을 줄이는 요약기입니다. <기록> 은 대화가 길어져 잘라낼 앞부분이며, 이어서 대답할 대화가 아닙니다.
에이전트가 이 요약만 보고 작업을 그대로 이어갈 수 있도록 아래 양식 그대로 한국어로 정리하세요.
<이전 요약> 이 함께 오면 새 기록을 반영해 그 요약을 갱신하세요: 이전 요약의 사실·숫자·결정은 지우지 말고 유지하고,
새로 끝난 일은 완료로 옮기고, 더 이상 맞지 않는 내용만 고치세요.

## 목표
- 사용자가 최종적으로 원하는 결과 (한두 줄)

## 조건·선호
- 사용자가 말한 조건 (날짜·기간, 부서, 사이트, 형식, 파일 이름 규칙, 하지 말라고 한 것)

## 진행
### 완료
- [x] 끝난 일과 그 결과 (숫자·건수·합계는 기록 그대로 정확히)
### 진행 중
- [ ] 하던 일과 어디까지 했는지
### 막힘
- 실패한 시도와 원인 (오류 메시지 요지), 같은 방법을 다시 하지 않도록

## 주요 결정
- **결정**: 이유

## 다음 할 일
1. 바로 이어서 할 일부터 순서대로

## 꼭 필요한 정보
- 이어서 하려면 필요한 값: 조회한 ID·번호, URL, 사이트·도구 이름, 표의 열 이름, 중간 계산값

규칙: 기록에 없는 내용은 쓰지 말고 추측하지 마세요. 해당 내용이 없는 항목은 '- 없음'. 파일 목록은 따로 붙으니 경로를 길게 나열하지 않아도 됩니다. 3000자 이내."""

SUMMARY_HEAD = "[이전 대화 요약 - 대화가 길어져 앞부분을 자동으로 요약했습니다]"


def summary_message(summary: str, made: list[str], used: list[str], current: str | None = None) -> dict[str, Any]:
    """요약 메시지: 요약 본문 + 파일 목록(기계적으로 모은 것) + (잘려 나간) 현재 요청 원문."""
    text = SUMMARY_HEAD + "\n" + summary
    if made:
        text += "\n\n<받거나 만든 파일>\n" + "\n".join(made) + "\n</받거나 만든 파일>"
    if used:
        text += "\n\n<도구에 넘긴 파일>\n" + "\n".join(used) + "\n</도구에 넘긴 파일>"
    if current:
        text += "\n\n[지금 처리 중인 요청 원문]\n" + current
    return {"role": "user", "content": text, "_summary": True, "_summary_body": summary, "_files": made, "_used_files": used}


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
        """앞쪽 대화를 요약 한 개로 바꿈. 바꾼 내용(usage 포함)을 돌려주고, 줄일 게 없으면 None.
        - 이미 요약이 있으면 그 요약을 <이전 요약> 으로 따로 넘겨 갱신시킴 (요약을 다시 요약하며 잘리지 않게)
        - 파일 목록은 LLM 이 아니라 기록에서 기계적으로 모아 누적 (경로가 빠지거나 틀리지 않게)"""
        cut = self._tail_start(messages, keep_tokens)
        head = messages[1:cut]
        if not head:
            return None
        prompts = [i for i in range(1, len(messages)) if messages[i].get("role") == "user"
                   and not messages[i].get("_summary") and not messages[i].get("_auto")]
        current = prompts[-1] if prompts else None
        keep_prompt = current is not None and current < cut  # 현재 요청이 잘려 나가면 원문을 그대로 붙임
        prev = [m for m in head if m.get("_summary")]
        prev_text = "\n\n".join(str(m.get("_summary_body") or m.get("content") or "") for m in prev)
        records = [m for m in head if not m.get("_summary")]
        made, used = collect_files(head)
        transcript = "\n\n".join(_render(m) for m in records)
        max_chars = int(self.window * 0.45 * 2.5) - len(prev_text)
        if len(transcript) > max_chars > 0:
            transcript = transcript[: max_chars // 3] + "\n…(중략)…\n" + transcript[-(max_chars * 2) // 3:]
        body = (f"<이전 요약>\n{prev_text}\n</이전 요약>\n\n" if prev_text else "") + f"<기록>\n{transcript}\n</기록>"
        resp = await llm.chat([{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": body}], None)
        summary = (resp.content or "").strip()
        if not summary:
            return None
        cur_text = None
        if keep_prompt:
            c = messages[current].get("content")
            if isinstance(c, list):
                c = " ".join(str(p.get("text") or "") for p in c)
            cur_text = str(c)
        messages[:] = [messages[0], summary_message(summary, made, used, cur_text)] + messages[cut:]
        self.stats["summarized"] += len(head)
        return {"summarized": len(head), "updated": bool(prev), "files": len(made) + len(used), "usage": getattr(resp, "usage", None) or {}}

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
                info.update(r)  # summarized, updated(이전 요약 갱신), files(기록한 파일 수), usage
        info["after"] = self.tokens(messages, tools)
        if not info["masked"] and not info["summarized"]:
            return None
        return info
