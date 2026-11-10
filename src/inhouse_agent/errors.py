"""실행이 끝난 상태를 사용자가 바로 알아볼 수 있게: 상태 이름, 오류 원인 분류, 조치 안내.

종료 상태 (AgentResult.stopped_reason)
  completed       완료
  max_iterations  반복 한도 도달 → 마지막 정리로 중간 결과를 답함 ('계속' 으로 이어서)
  time_limit      시간 한도 도달 → 중간 결과
  loop            같은 작업을 되풀이해 멈춤 → 중간 결과
  tool_errors     도구 오류가 이어져 멈춤
  error           오류 (LLM 연결·인증·한도, 워크플로 단계 실패, 내부 오류)
  cancelled       사용자가 중단
"""

from __future__ import annotations

from typing import Any

STATUS: dict[str, tuple[str, str]] = {
    "completed": ("완료", "ok"),
    "max_iterations": ("반복 한도에 도달해 중간 결과로 마쳤습니다", "warn"),
    "time_limit": ("시간 한도에 도달해 중간 결과로 마쳤습니다", "warn"),
    "loop": ("같은 작업을 되풀이해 멈췄습니다", "warn"),
    "tool_errors": ("도구 오류가 이어져 멈췄습니다", "error"),
    "error": ("오류로 실행을 마치지 못했습니다", "error"),
    "cancelled": ("사용자가 중단했습니다", "muted"),
}


def status_label(status: str) -> str:
    return STATUS.get(status, (status, "muted"))[0]


def status_level(status: str) -> str:
    return STATUS.get(status, (status, "muted"))[1]


def _body_code(e: BaseException) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        return str(err.get("code") or err.get("type") or "")
    return ""


def describe_error(e: BaseException, deployment: str = "") -> dict[str, Any]:
    """예외 → {code, title, detail, hint, retryable}. openai SDK 를 import 하지 않고 이름·상태코드로 판단."""
    from .context import is_overflow_error

    name = type(e).__name__
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    code = _body_code(e)
    msg = str(e)
    low = f"{name} {code} {msg}".lower()
    detail = msg if len(msg) <= 400 else msg[:400] + "…"

    def r(c: str, title: str, hint: str, retry: bool = False) -> dict[str, Any]:
        return {"code": c, "title": title, "detail": detail, "hint": hint, "retryable": retry, "type": name, "status": status}

    if "max_output_tokens" in low and ("incomplete" in low or "완료" in low or "length" in low):
        return r("output_limit", "모델이 출력 한도(max_output_tokens)에 닿아 답을 끝내지 못했습니다",
                 "요청을 더 작게 나누거나 llm.reasoning_effort 를 낮추세요. llm.max_output_tokens_cap 을 늘릴 수도 있습니다.", True)
    if is_overflow_error(e):
        return r("context", "대화가 모델의 입력 한도를 넘었습니다 (자동 압축 후에도)",
                 "새 대화로 시작하거나, 파일을 나눠 분석하세요(하위 에이전트). 배포 모델의 한도에 맞게 agent.context_window 를 확인하세요.")
    if "content_filter" in low or "responsibleaipolicyviolation" in low or "content management policy" in low:
        return r("content_filter", "Azure 콘텐츠 필터에 걸려 답을 받지 못했습니다",
                 "요청 표현을 바꾸거나, 첨부·도구 결과에 민감한 내용이 있는지 확인하세요.")
    if status == 401 or name == "AuthenticationError":
        return r("auth", "Azure OpenAI 인증에 실패했습니다", "API 키(AZURE_OPENAI_API_KEY) 또는 Entra ID 인증 설정을 확인하세요.")
    if status == 403 or name == "PermissionDeniedError":
        return r("permission", "Azure OpenAI 접근 권한이 없습니다", "키의 리소스·배포 권한과 사내 게이트웨이(APIM) 구독 키를 확인하세요.")
    if status == 404 or name == "NotFoundError" or "deploymentnotfound" in low:
        return r("deployment", f"모델 배포를 찾을 수 없습니다 ({deployment or '배포 이름 미설정'})",
                 "config.yaml 의 llm.deployment(배포 이름)와 llm.api_version 을 확인하세요.")
    if status == 429 or name == "RateLimitError":
        return r("rate_limit", "LLM 요청 한도를 넘었습니다 (자동 재시도 후에도)", "1~2분 뒤 다시 시도하세요. 자주 나면 배포의 TPM 한도를 늘려야 합니다.", True)
    if "timeout" in low or isinstance(e, TimeoutError):
        return r("timeout", "LLM 응답이 시간 안에 오지 않았습니다", "잠시 후 다시 시도하세요. 자주 나면 llm.timeout 을 늘리세요.", True)
    if name in ("APIConnectionError", "ConnectError", "ConnectionError") or isinstance(e, ConnectionError):
        return r("network", "LLM 서버에 연결하지 못했습니다", "llm.endpoint 주소, 사내 프록시·방화벽 설정을 확인하세요.", True)
    if isinstance(status, int) and status >= 500:
        return r("server", f"Azure OpenAI 서버 오류입니다 ({status})", "잠시 후 다시 시도하세요.", True)
    if status == 400 or name == "BadRequestError":
        return r("bad_request", "LLM 이 요청을 거절했습니다", "같은 오류가 계속되면 logs/agent.log 와 함께 관리자에게 알려 주세요.")
    return r("internal", f"에이전트 내부 오류 ({name})", "다시 시도해 보고, 계속되면 logs/agent.log 를 확인하세요.", True)


def progress_lines(events: list[Any], files: list[dict[str, Any]]) -> list[str]:
    ok = [e for e in events if not e.is_error]
    bad = [e for e in events if e.is_error]
    lines = []
    if ok or bad:
        lines.append(f"- 실행한 도구: 성공 {len(ok)}회" + (f", 오류 {len(bad)}회" if bad else "")
                     + (f" ({', '.join(dict.fromkeys(e.function for e in ok))[:200]})" if ok else ""))
    if files:
        lines.append("- 받거나 만든 파일: " + ", ".join(str(f.get("path")) for f in files[:8]))
    return lines


def failure_text(info: dict[str, Any], events: list[Any] | None = None, files: list[dict[str, Any]] | None = None) -> str:
    """오류로 끝났을 때 사용자에게 보여줄 글."""
    out = [f"❌ **{info['title']}**", ""]
    if info.get("detail"):
        out.append(f"- 원인: {info['detail']}")
    out.append(f"- 조치: {info['hint']}")
    if info.get("retryable"):
        out.append("- 잠시 후 **다시 시도**하면 될 수 있습니다.")
    prog = progress_lines(events or [], files or [])
    if prog:
        out += ["", "멈추기 전까지 진행한 내용:", *prog]
    return "\n".join(out)


def stop_note(status: str, detail: str = "") -> str:
    """중간 결과로 마친 경우 답변 끝에 붙이는 안내."""
    if status in ("completed", "error", "cancelled"):
        return ""
    return (f"\n\n---\n⚠️ **{status_label(status)}**" + (f" ({detail})" if detail else "")
            + "\n이어서 하려면 **계속** 이라고 입력하세요 (지금까지 대화와 파일을 기억한 채로 이어집니다).")
