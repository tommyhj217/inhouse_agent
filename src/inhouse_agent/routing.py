"""비용 기준 모델 자동 선택 (sol / terra / luna 처럼 배포가 여러 개일 때).

동작
1) 요청마다 난이도 판별 → 그 난이도에 맞는 가장 싼 모델로 시작
     classifier: rules (기본, 비용 0) - 첨부·이미지 수, 요청 길이, '분석/비교/검증/보고서' 같은 단어
                 llm   - 가장 싼 모델에게 쉬움/보통/어려움을 물어봄 (판별 비용 조금, Gemini CLI 방식)
2) 막히면 한 단계 위 모델로 승격 (Goose lead/worker 방식: 도구 오류 연속, 같은 호출 되풀이, 진전 없음, 빈 답변)
3) 모델이 응답하지 못하면(요청 한도·배포 없음·서버 오류) 다른 모델로 넘어감
4) 잡일은 싼 모델: 대화 압축 요약(summary_model), 하위 에이전트(subagent_max_tier 이하)
5) 모델별 토큰(캐시된 입력 포함)·비용을 기록하고, 가장 비싼 모델만 썼을 때와 비교해 아낀 비용을 보여줌

config.yaml
llm:
  deployment: sol                 # 기본 모델 (routing.mode: fixed 일 때 항상 이 모델)
  models:
    - {name: sol,   deployment: sol,   tier: high, cost_in: 2.0, cost_out: 10.0, cost_cached: 0.1,  context_window: 272000}
    - {name: terra, deployment: terra, tier: mid,  cost_in: 1.0, cost_out: 5.0,  cost_cached: 0.05, context_window: 272000}
    - {name: luna,  deployment: luna,  tier: low,  cost_in: 0.1, cost_out: 0.5,  cost_cached: 0.01, context_window: 272000}
  routing: {mode: auto, classifier: rules, escalate: true, summary_model: luna, subagent_max_tier: mid, currency: "$"}
각 모델에 endpoint / api_key / api_version / max_output_tokens / reasoning_effort / temperature 를 따로 줄 수도 있습니다.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TIERS = {"low": 1, "mid": 2, "high": 3}
TIER_NAME = {1: "low", 2: "mid", 3: "high"}
TIER_KO = {1: "쉬움", 2: "보통", 3: "어려움"}

HEAVY = ("분석", "비교", "검증", "검토", "규칙", "보고서", "작성", "전략", "원인", "설계", "평가", "예측", "왜", "어떻게",
         "추천", "개선", "기획", "요약해서 보고", "정리해서 보고", "pptx", "ppt", "슬라이드")
LIGHT = ("조회", "알려줘", "몇 ", "몇개", "몇 개", "언제", "날짜", "시간", "목록", "찾아", "다운로드", "받아", "열어", "확인해", "있어?", "뭐야")
CORRECTION = ("틀렸", "틀린", "잘못", "아니야", "아니라", "다시 해", "다시해", "그게 아니", "제대로")
CONTINUE = ("계속", "이어서", "continue")

# 모델마다 따로 줄 수 있는 연결 설정 (예: luna 는 api: chat, sol 은 api: responses)
OVERRIDE_KEYS = ("endpoint", "base_url", "api_key", "api_version", "max_output_tokens", "max_output_tokens_cap", "reasoning_effort", "temperature",
                 "max_tokens_param", "extra_headers", "api", "responses_api_version", "responses_base_url", "reasoning_replay")

AVAILABILITY_CODES = {"rate_limit", "deployment", "server", "timeout", "network", "permission"}

CLASSIFY_PROMPT = """당신은 업무 요청의 난이도를 고르는 분류기입니다. 아래 기준으로 low / mid / high 중 하나를 고르세요.
- high: 도구를 4번 이상 이어서 써야 하는 일, 여러 자료 비교·분석·검증, 보고서·PPT 작성, 원인 분석, 전략·설계 판단
- mid: 도구 2~3번이면 되는 일, 자료 한 개를 정리·요약, 간단한 집계
- low: 한 번의 조회·확인, 날짜·목록·상태 같은 짧은 답
JSON 한 줄로만 답하세요: {"level": "low|mid|high", "reason": "짧은 이유"}"""


@dataclass
class ModelSpec:
    name: str
    deployment: str
    tier: int = 2
    cost_in: float | None = None  # 입력 100만 토큰당 비용
    cost_out: float | None = None
    cost_cached: float | None = None  # 캐시된 입력 100만 토큰당 비용 (없으면 cost_in 과 같다고 봄 - 할인 없이 보수적으로)
    context_window: int | None = None
    vision: bool | None = None
    description: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def price(self) -> float:
        """정렬용 대표 가격 (입력 3 : 출력 1 비율 가정). 값이 없으면 등급으로."""
        if self.cost_in is None and self.cost_out is None:
            return float(self.tier) * 1000
        return 0.75 * (self.cost_in or 0) + 0.25 * (self.cost_out or 0)

    def cost(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float | None:
        """prompt_tokens 는 캐시된 입력을 포함한 전체 입력 (API usage 그대로), cached_tokens 는 그중 캐시에서 읽은 양."""
        if self.cost_in is None and self.cost_out is None:
            return None
        cached = max(0, min(int(cached_tokens or 0), int(prompt_tokens or 0)))
        rate_cached = self.cost_cached if self.cost_cached is not None else (self.cost_in or 0)
        return ((prompt_tokens - cached) * (self.cost_in or 0) + cached * rate_cached + completion_tokens * (self.cost_out or 0)) / 1_000_000


@dataclass
class Decision:
    model: str
    reason: str
    tier: int


def _to_tier(v: Any) -> int:
    if isinstance(v, int):
        return min(3, max(1, v))
    return TIERS.get(str(v or "").strip().lower(), 2)


class ModelRouter:
    def __init__(self, llm_raw: dict[str, Any], default_deployment: str, default_vision: bool = True):
        llm_raw = llm_raw or {}
        r = llm_raw.get("routing") or {}
        self.mode = str(r.get("mode") or "auto").lower()
        self.classifier = str(r.get("classifier") or "rules").lower()
        self.escalation = r.get("escalate", True) is not False
        self.currency = str(r.get("currency") or "")
        specs = []
        for m in llm_raw.get("models") or []:
            if isinstance(m, str):
                m = {"name": m, "deployment": m}
            if not isinstance(m, dict) or not (m.get("deployment") or m.get("name")):
                continue
            extra = {k: m[k] for k in OVERRIDE_KEYS if k in m}
            specs.append(ModelSpec(
                name=str(m.get("name") or m["deployment"]), deployment=str(m.get("deployment") or m["name"]), tier=_to_tier(m.get("tier")),
                cost_in=_num(m.get("cost_in")), cost_out=_num(m.get("cost_out")), cost_cached=_num(m.get("cost_cached")),
                context_window=int(m["context_window"]) if m.get("context_window") else None,
                vision=None if m.get("vision") is None else bool(m.get("vision")), description=str(m.get("description") or ""), overrides=extra))
        if not any(s.deployment == default_deployment or s.name == default_deployment for s in specs) and default_deployment:
            specs.append(ModelSpec(name=default_deployment, deployment=default_deployment, tier=3, vision=default_vision))
        self.specs = sorted(specs, key=lambda s: (s.price, s.tier))
        self.by_name = {s.name: s for s in self.specs}
        self.default = next((s for s in self.specs if default_deployment in (s.deployment, s.name)), self.specs[-1] if self.specs else None)
        self.summary_model = str(r.get("summary_model") or (self.specs[0].name if self.specs else ""))
        self.subagent_max_tier = _to_tier(r.get("subagent_max_tier") or "mid")
        for s in self.specs:
            if s.vision is None:
                s.vision = default_vision

    @property
    def enabled(self) -> bool:
        return self.mode == "auto" and len(self.specs) > 1

    def get(self, name: str | None) -> ModelSpec | None:
        return self.by_name.get(str(name)) if name else None

    # ------------------------------------------------------------ 판별
    def complexity(self, prompt: str, attachments: int = 0, images: int = 0) -> tuple[int, list[str]]:
        p = prompt or ""
        low = p.lower()
        score, why = 0, []
        heavy = [w for w in HEAVY if w in low]
        if heavy:
            score += 2 if len(heavy) >= 2 else 1
            why.append("작업: " + ", ".join(heavy[:3]))
        light = [w for w in LIGHT if w in low]
        if light and not heavy:
            score -= 1
            why.append("단순 조회")
        if attachments >= 2:
            score += 2
            why.append(f"첨부 {attachments}개")
        elif attachments == 1:
            score += 1
            why.append("첨부 1개")
        if len(p) > 800:
            score += 2
            why.append("긴 요청")
        elif len(p) > 250:
            score += 1
            why.append("요청이 김")
        if re.search(r"(그리고|또한|그 다음|다음에|\n\s*\d+[.)])", p) and len(p) > 80:
            score += 1
            why.append("여러 단계")
        tier = 1 if score <= 0 else 2 if score <= 2 else 3
        return tier, why or ["짧은 요청"]

    def cheapest(self, tier: int, need_vision: bool = False, max_tier: int = 3, min_context: int = 0) -> ModelSpec | None:
        ok = [s for s in self.specs if (s.vision or not need_vision) and (s.context_window or 10**9) >= min_context]
        for t in range(tier, max_tier + 1):
            cand = [s for s in ok if s.tier == t]
            if cand:
                return cand[0]  # 이미 가격순
        cand = [s for s in ok if s.tier <= max_tier]
        return cand[-1] if cand else (ok[-1] if ok else None)

    async def choose(self, pool: Any, prompt: str, *, attachments: int = 0, images: int = 0, override: str | None = None,
                     skill_meta: dict[str, Any] | None = None, last_model: str | None = None, max_tier: int = 3) -> Decision | None:
        if not self.specs:
            return None
        if override and override != "auto":
            s = self.get(override)
            if s:
                return Decision(s.name, "사용자가 선택", s.tier)
        if not self.enabled:
            return Decision(self.default.name, "고정 모델 (routing.mode: fixed)", self.default.tier) if self.default else None
        meta = skill_meta or {}
        if meta.get("model") and self.get(meta["model"]):
            s = self.get(meta["model"])
            return Decision(s.name, "스킬에 지정된 모델", s.tier)
        p = (prompt or "").strip()
        last = self.get(last_model)
        if last and len(p) <= 20 and any(w in p.lower() for w in CONTINUE):
            return Decision(last.name, "이어서 하기 - 직전 모델 유지", last.tier)
        if last and any(w in p for w in CORRECTION):
            up = self.escalate(last, images > 0) or last
            return Decision(up.name, "사용자가 결과를 고치라고 함 - 한 단계 위 모델", up.tier)
        if meta.get("model_tier"):
            tier, why = _to_tier(meta["model_tier"]), ["스킬 난이도"]
        elif self.classifier == "llm" and pool is not None:
            tier, why = await self._classify_llm(pool, p, attachments, images)
        else:
            tier, why = self.complexity(p, attachments, images)
        s = self.cheapest(min(tier, max_tier), images > 0, max_tier)
        if s is None:
            return None
        return Decision(s.name, f"{TIER_KO[tier]} ({', '.join(why)})" + (" · 이미지 지원 모델" if images else ""), s.tier)

    async def _classify_llm(self, pool: Any, prompt: str, attachments: int, images: int) -> tuple[int, list[str]]:
        cheap = self.specs[0]
        try:
            r = await pool.chat([{"role": "system", "content": CLASSIFY_PROMPT},
                                 {"role": "user", "content": f"요청: {prompt[:2000]}\n첨부 문서 {attachments}개, 이미지 {images}개"}],
                                None, model=cheap.name)
            m = re.search(r"\{.*\}", r.content or "", re.S)
            d = json.loads(m.group(0)) if m else {}
            level = _to_tier(d.get("level"))
            return level, [f"{cheap.name} 판별: {str(d.get('reason') or '')[:60]}"]
        except Exception as e:  # noqa: BLE001 - 판별이 실패해도 규칙으로
            log.warning("모델 난이도 판별 실패(규칙으로 대체): %s", e)
            return self.complexity(prompt, attachments, images)

    # ------------------------------------------------------------ 바꾸기
    def escalate(self, current: ModelSpec | str | None, need_vision: bool = False) -> ModelSpec | None:
        cur = self.get(current) if isinstance(current, str) else current
        if cur is None or not self.escalation:
            return None
        ups = [s for s in self.specs if s.price > cur.price and (s.vision or not need_vision)]
        higher_tier = [s for s in ups if s.tier > cur.tier]
        return (higher_tier or ups or [None])[0]

    def fallback(self, current: str, tried: set[str], need_vision: bool = False) -> ModelSpec | None:
        """모델이 응답하지 못할 때 다른 모델 (가까운 가격부터, 위 → 아래)."""
        cur = self.get(current)
        rest = [s for s in self.specs if s.name not in tried and (s.vision or not need_vision)]
        if not rest:
            return None
        base = cur.price if cur else 0
        up = [s for s in rest if s.price >= base]
        return (up or sorted(rest, key=lambda s: -s.price))[0]

    def bigger_context(self, current: str, need: int) -> ModelSpec | None:
        cur = self.get(current)
        cands = [s for s in self.specs if (s.context_window or 0) > ((cur.context_window or 0) if cur else 0) and (s.context_window or 0) >= need]
        return cands[0] if cands else None

    # ------------------------------------------------------------ 비용
    def cost_of(self, by_model: dict[str, dict[str, int]]) -> tuple[float | None, float | None]:
        """(실제 비용, 가장 비싼 모델만 썼을 때 비용). 가격이 없으면 None."""
        if not self.specs:
            return None, None
        top = max(self.specs, key=lambda s: s.price)
        total = base = 0.0
        known = False
        for name, u in by_model.items():
            s = self.get(name)
            p, c, k = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0), int(u.get("cached_tokens") or 0)
            v = s.cost(p, c, k) if s else None
            b = top.cost(p, c, k)  # 비교 기준도 같은 캐시 적중을 가정 (모델 선택으로 아낀 몫만 보이게)
            if v is not None:
                known = True
                total += v
            if b is not None:
                base += b
        return (round(total, 6) if known else None), (round(base, 6) if known else None)

    def catalog(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "deployment": s.deployment, "tier": TIER_NAME[s.tier], "cost_in": s.cost_in, "cost_out": s.cost_out,
                 "cost_cached": s.cost_cached, "context_window": s.context_window, "vision": s.vision, "description": s.description, "default": s is self.default}
                for s in self.specs]


def _num(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


class ModelPool:
    """배포마다 AzureChatModel 하나 (처음 쓸 때 만듦). chat(..., model=이름)."""

    def __init__(self, base_cfg: Any, router: ModelRouter):
        self.base = base_cfg
        self.router = router
        self._clients: dict[str, Any] = {}
        self.cfg = base_cfg  # describe_error 등에서 deployment 표시용

    def _client(self, name: str | None) -> tuple[str, Any]:
        from .llm import make_chat_model

        spec = self.router.get(name) or self.router.default
        key = spec.name if spec else "_default"
        if key not in self._clients:
            cfg = self.base
            if spec is not None:
                ov = dict(spec.overrides)
                cfg = dataclasses.replace(cfg, deployment=spec.deployment, vision=bool(spec.vision), **{
                    k: ov[k] for k in OVERRIDE_KEYS if k in ov})
            self._clients[key] = make_chat_model(cfg)
        return key, self._clients[key]

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *, model: str | None = None, **kw: Any) -> Any:
        name, client = self._client(model)
        r = await client.chat(messages, tools, **kw)
        r.model = name
        return r

    async def aclose(self) -> None:
        for c in self._clients.values():
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass
