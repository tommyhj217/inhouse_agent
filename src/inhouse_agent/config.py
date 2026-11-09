"""설정 파일(config.yaml) 로딩.

- `${VAR}` / `${VAR:-기본값}` 형식으로 환경변수를 치환합니다.
- 프로젝트 루트의 `.env` 파일이 있으면 먼저 읽어 환경변수로 올립니다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv 는 선택 사항
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """설정 오류."""


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            return os.environ.get(name, default if default is not None else "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _none_if_blank(v: Any) -> Any:
    return None if isinstance(v, str) and v.strip() == "" else v


def _to_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _to_float(v: Any, default: float | None) -> float | None:
    v = _none_if_blank(v)
    return default if v is None else float(v)


def _to_int(v: Any, default: int | None) -> int | None:
    v = _none_if_blank(v)
    return default if v is None else int(v)


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    endpoint: str | None = None
    """Azure OpenAI 리소스 엔드포인트. 예) https://my-aoai.openai.azure.com"""
    base_url: str | None = None
    """APIM 등 게이트웨이를 쓰는 경우 전체 base URL (endpoint 대신 사용)."""
    deployment: str = ""
    """Azure 에 배포한 모델 배포 이름 (deployment name)."""
    api_version: str = "2024-10-21"
    auth: str = "api_key"  # api_key | entra_id
    api_key: str | None = None
    entra_scope: str = "https://cognitiveservices.azure.com/.default"
    max_output_tokens: int | None = 4096
    max_tokens_param: str = "max_completion_tokens"  # 구형 api_version 이면 max_tokens
    temperature: float | None = None
    reasoning_effort: str | None = None  # low | medium | high (추론 모델용)
    timeout: float = 180.0
    max_retries: int = 3
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    vision: bool = True
    """배포 모델이 이미지 입력을 지원하는지 (미지원이면 이미지를 텍스트 안내로 바꿔 보냄)"""
    prompt_cache_key: bool = True
    """대화마다 prompt_cache_key 를 보내 프롬프트 캐시 적중률을 높임 (배포가 모르는 인자라고 거절하면 자동으로 끔)"""
    provider: str = "azure"
    """azure | none (none: LLM 없이 사용 - 스킬 워크플로/도구 직접 실행만)"""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LLMConfig":
        d = d or {}
        return cls(
            endpoint=_none_if_blank(d.get("endpoint")),
            base_url=_none_if_blank(d.get("base_url")),
            deployment=str(d.get("deployment") or ""),
            api_version=str(d.get("api_version") or "2024-10-21"),
            auth=str(d.get("auth") or "api_key").lower(),
            api_key=_none_if_blank(d.get("api_key")),
            entra_scope=str(d.get("entra_scope") or "https://cognitiveservices.azure.com/.default"),
            max_output_tokens=_to_int(d.get("max_output_tokens"), 4096),
            max_tokens_param=str(d.get("max_tokens_param") or "max_completion_tokens"),
            temperature=_to_float(d.get("temperature"), None),
            reasoning_effort=_none_if_blank(d.get("reasoning_effort")),
            timeout=_to_float(d.get("timeout"), 180.0) or 180.0,
            max_retries=_to_int(d.get("max_retries"), 3) or 0,
            extra_headers={k: str(v) for k, v in (d.get("extra_headers") or {}).items() if _none_if_blank(v) is not None},
            extra_body=dict(d.get("extra_body") or {}),
            vision=_to_bool(d.get("vision"), True),
            provider=str(d.get("provider") or "azure").lower(),
            prompt_cache_key=_to_bool(d.get("prompt_cache_key"), True),
        )

    def validate(self) -> None:
        if not self.deployment:
            raise ConfigError("llm.deployment(AZURE_OPENAI_DEPLOYMENT) 가 비어 있습니다.")
        if not (self.endpoint or self.base_url):
            raise ConfigError("llm.endpoint(AZURE_OPENAI_ENDPOINT) 또는 llm.base_url 이 필요합니다.")
        if self.auth not in ("api_key", "entra_id"):
            raise ConfigError("llm.auth 는 api_key 또는 entra_id 이어야 합니다.")
        if self.auth == "api_key" and not self.api_key:
            raise ConfigError("llm.api_key(AZURE_OPENAI_API_KEY) 가 비어 있습니다.")


@dataclass
class AgentConfig:
    system_prompt: str = ""
    max_iterations: int = 30  # 기본 반복 예산 (진전이 있으면 자동으로 늘어남)
    max_iterations_cap: int = 100  # 늘어날 수 있는 상한
    extend_iterations: int = 10  # 한 번에 늘리는 횟수
    max_minutes: float = 30.0  # 한 요청의 최대 실행 시간 (분)
    loop_threshold: int = 3  # 같은 도구를 같은 인자로 이만큼 부르면 되풀이로 보고 멈춤
    max_tool_error_streak: int = 3  # 도구가 모두 실패한 차례가 이만큼 이어지면 멈춤
    max_tool_result_chars: int = 30000
    max_history_messages: int = 200  # 넘으면 앞부분을 요약 (토큰 기준 자동 압축과 함께 동작)
    context_window: int = 128000  # 배포 모델의 입력 한도(토큰). 자동 압축 기준
    compact_at: float = 0.75  # 한도의 이 비율을 넘으면 자동 압축
    subagents: bool = True  # run_subagent (여러 파일·사이트를 나눠 별도 대화에서 처리)
    tool_batch: bool = True  # run_tool_batch (여러 도구 호출을 LLM 왕복 한 번으로)
    subagent_max_iterations: int = 12
    subagent_max_minutes: float = 10.0
    parallel_tool_calls: bool = True
    output_dir: str = "outputs"
    log_dir: str = "logs"
    timezone: str = "Asia/Seoul"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        d = d or {}
        return cls(
            system_prompt=str(d.get("system_prompt") or ""),
            max_iterations=_to_int(d.get("max_iterations"), 30) or 30,
            max_iterations_cap=_to_int(d.get("max_iterations_cap"), 100) or 100,
            extend_iterations=_to_int(d.get("extend_iterations"), 10) or 10,
            max_minutes=_to_float(d.get("max_minutes"), 30.0) or 30.0,
            loop_threshold=max(2, _to_int(d.get("loop_threshold"), 3) or 3),
            max_tool_error_streak=max(1, _to_int(d.get("max_tool_error_streak"), 3) or 3),
            max_tool_result_chars=_to_int(d.get("max_tool_result_chars"), 30000) or 30000,
            max_history_messages=_to_int(d.get("max_history_messages"), 200) or 200,
            context_window=_to_int(d.get("context_window"), 128000) or 128000,
            compact_at=min(0.95, max(0.3, _to_float(d.get("compact_at"), 0.75) or 0.75)),
            subagents=_to_bool(d.get("subagents"), True),
            tool_batch=_to_bool(d.get("tool_batch"), True),
            subagent_max_iterations=_to_int(d.get("subagent_max_iterations"), 12) or 12,
            subagent_max_minutes=_to_float(d.get("subagent_max_minutes"), 10.0) or 10.0,
            parallel_tool_calls=_to_bool(d.get("parallel_tool_calls"), True),
            output_dir=str(d.get("output_dir") or "outputs"),
            log_dir=str(d.get("log_dir") or "logs"),
            timezone=str(d.get("timezone") or "Asia/Seoul"),
        )


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"  # stdio | http(streamable-http) | sse
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""
    include_tools: list[str] = field(default_factory=list)
    exclude_tools: list[str] = field(default_factory=list)
    connect_timeout: float = 60.0
    tool_timeout: float = 300.0
    retry_on_failure: bool = True
    remote: Any = "auto"
    """MCP 서버가 다른 PC 인지: auto(URL 호스트로 판단) | true | false"""
    remote_files: dict[str, Any] = field(default_factory=dict)
    """원격 서버 파일 가져오기 설정 (path_map / fetch_tool / use_resources / rewrite_localhost)"""

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "MCPServerConfig":
        d = d or {}
        transport = str(d.get("transport") or ("http" if d.get("url") else "stdio")).lower()
        if transport in ("streamable-http", "streamable_http", "streamablehttp"):
            transport = "http"
        cfg = cls(
            name=name,
            transport=transport,
            command=_none_if_blank(d.get("command")),
            args=[str(a) for a in (d.get("args") or [])],
            env={k: str(v) for k, v in (d.get("env") or {}).items()},
            cwd=_none_if_blank(d.get("cwd")),
            url=_none_if_blank(d.get("url")),
            headers={k: str(v) for k, v in (d.get("headers") or {}).items()},
            enabled=_to_bool(d.get("enabled"), True),
            description=str(d.get("description") or ""),
            include_tools=list(d.get("include_tools") or []),
            exclude_tools=list(d.get("exclude_tools") or []),
            connect_timeout=_to_float(d.get("connect_timeout"), 60.0) or 60.0,
            tool_timeout=_to_float(d.get("tool_timeout"), 300.0) or 300.0,
            retry_on_failure=_to_bool(d.get("retry_on_failure"), True),
            remote=d.get("remote", "auto"),
            remote_files=dict(d.get("remote_files") or {}),
        )
        if cfg.transport not in ("stdio", "http", "sse"):
            raise ConfigError(f"mcp_servers.{name}.transport 는 stdio/http/sse 중 하나여야 합니다: {cfg.transport}")
        if cfg.transport == "stdio" and not cfg.command:
            raise ConfigError(f"mcp_servers.{name}: stdio 방식은 command 가 필요합니다.")
        if cfg.transport in ("http", "sse") and not cfg.url:
            raise ConfigError(f"mcp_servers.{name}: {cfg.transport} 방식은 url 이 필요합니다.")
        return cfg


@dataclass
class ScheduleConfig:
    name: str
    prompt: str
    cron: str
    timezone: str | None = None
    servers: list[str] | None = None
    enabled: bool = True
    output_format: str = "md"  # md | txt | json
    webhook_url: str | None = None
    description: str = ""
    skill: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    user: str | None = None
    """이 사용자의 작업폴더에서 실행 (생략 시 공용 workspace)"""
    business_days_only: bool = False
    """true 면 휴일(주말·공휴일·회사 휴무일)에는 건너뜀"""
    business_day: str | None = None
    """그 달의 n번째 영업일에만 실행: first | last | 3 | -2 (cron 은 매일로 두고 함께 사용)"""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScheduleConfig":
        if not d.get("name"):
            raise ConfigError("schedules 항목에 name 이 없습니다.")
        if not d.get("prompt") and not d.get("skill"):
            raise ConfigError(f"schedules.{d['name']}: prompt 또는 skill 이 필요합니다.")
        if not d.get("cron"):
            raise ConfigError(f"schedules.{d['name']}: cron 이 없습니다.")
        servers = d.get("servers")
        return cls(
            name=str(d["name"]),
            prompt=str(d.get("prompt") or ""),
            cron=str(d["cron"]),
            timezone=_none_if_blank(d.get("timezone")),
            servers=[str(s) for s in servers] if servers else None,
            enabled=_to_bool(d.get("enabled"), True),
            output_format=str(d.get("output_format") or "md").lower(),
            webhook_url=_none_if_blank(d.get("webhook_url")),
            description=str(d.get("description") or ""),
            skill=_none_if_blank(d.get("skill")),
            inputs=dict(d.get("inputs") or {}),
            user=_none_if_blank(d.get("user")),
            business_days_only=_to_bool(d.get("business_days_only"), False),
            business_day=_none_if_blank(d.get("business_day")),
        )


@dataclass
class FilesConfig:
    workspace: str = "workspace"
    """다운로드/업로드/저장 파일 작업 폴더 (하위: downloads, uploads, files)."""
    watch_dirs: list[str] = field(default_factory=list)
    """MCP(브라우저)가 파일을 내려받는 폴더. workspace/downloads 는 항상 포함."""
    extra_read_dirs: list[str] = field(default_factory=list)
    """읽기(분석)만 허용할 추가 폴더."""
    max_file_mb: int = 200
    inject_env: bool = True
    """stdio MCP 서버에 AGENT_DOWNLOAD_DIR / AGENT_WORKSPACE 환경변수를 넘겨줄지."""
    auto_detect: bool = True
    """MCP tool 호출 전후로 다운로드 폴더를 비교해 새 파일을 자동 감지·점검할지."""
    wait_timeout: float = 300.0
    """wait_for_download 기본 대기 시간(초)."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FilesConfig":
        d = d or {}
        return cls(
            workspace=str(d.get("workspace") or "workspace"),
            watch_dirs=[str(x) for x in d.get("watch_dirs") or [] if _none_if_blank(x)],
            extra_read_dirs=[str(x) for x in d.get("extra_read_dirs") or [] if _none_if_blank(x)],
            max_file_mb=_to_int(d.get("max_file_mb"), 200) or 200,
            inject_env=_to_bool(d.get("inject_env"), True),
            auto_detect=_to_bool(d.get("auto_detect"), True),
            wait_timeout=_to_float(d.get("wait_timeout"), 300.0) or 300.0,
        )


@dataclass
class AppConfig:
    llm: LLMConfig
    agent: AgentConfig
    mcp_servers: dict[str, MCPServerConfig]
    schedules: list[ScheduleConfig]
    base_dir: Path
    files: FilesConfig = field(default_factory=FilesConfig)
    skills_dir: str = "skills"
    downloads: dict[str, Any] = field(default_factory=dict)
    dynamic_tools: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # server / web_search / http / notify 등 원본 섹션

    @property
    def workspace_path(self) -> Path:
        return self.resolve_path(self.files.workspace)

    @property
    def skills_path(self) -> Path:
        return self.resolve_path(self.skills_dir)

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)

    @property
    def output_path(self) -> Path:
        return self.resolve_path(self.agent.output_dir)

    @property
    def log_path(self) -> Path:
        return self.resolve_path(self.agent.log_dir)

    def get_schedule(self, name: str) -> ScheduleConfig:
        for s in self.schedules:
            if s.name == name:
                return s
        raise ConfigError(f"예약작업 '{name}' 을(를) 찾을 수 없습니다.")


def find_config_path(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("INHOUSE_AGENT_CONFIG"):
        candidates.append(Path(os.environ["INHOUSE_AGENT_CONFIG"]))
    candidates += [Path.cwd() / "config" / "config.yaml", Path.cwd() / "config.yaml"]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise ConfigError(
        "설정 파일을 찾을 수 없습니다. config/config.example.yaml 을 config/config.yaml 로 복사하거나 "
        "--config 옵션으로 경로를 지정하세요."
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    cfg_path = find_config_path(str(path) if path else None)
    # config/config.yaml 이면 프로젝트 루트는 config 폴더의 상위
    base_dir = cfg_path.parent.parent if cfg_path.parent.name == "config" else cfg_path.parent

    if load_dotenv is not None:
        for env_file in (base_dir / ".env", Path.cwd() / ".env"):
            if env_file.is_file():
                load_dotenv(env_file, override=False)

    with open(cfg_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw = _expand_env(raw)

    servers_raw = raw.get("mcp_servers") or {}
    if not isinstance(servers_raw, dict):
        raise ConfigError("mcp_servers 는 '이름: 설정' 형태의 맵이어야 합니다.")
    servers = {name: MCPServerConfig.from_dict(name, d) for name, d in servers_raw.items()}
    for srv in servers.values():
        # stdio 서버의 작업 폴더: 지정 안 하면 프로젝트 루트, 상대경로면 프로젝트 루트 기준
        if srv.transport == "stdio":
            cwd = Path(srv.cwd) if srv.cwd else base_dir
            srv.cwd = str(cwd if cwd.is_absolute() else (base_dir / cwd))

    schedules = [ScheduleConfig.from_dict(d) for d in (raw.get("schedules") or [])]
    names = [s.name for s in schedules]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ConfigError(f"예약작업 이름이 중복됩니다: {', '.join(sorted(dup))}")
    for s in schedules:
        for srv in s.servers or []:
            if srv not in servers and srv != "builtin":
                raise ConfigError(f"schedules.{s.name}: 알 수 없는 MCP 서버 '{srv}'")

    files = FilesConfig.from_dict(raw.get("files") or {})
    app = AppConfig(
        llm=LLMConfig.from_dict(raw.get("llm") or {}),
        agent=AgentConfig.from_dict(raw.get("agent") or {}),
        mcp_servers=servers,
        schedules=schedules,
        base_dir=base_dir,
        files=files,
        skills_dir=str((raw.get("skills") or {}).get("dir") or "skills"),
        downloads=dict(raw.get("downloads") or {}),
        dynamic_tools=dict(raw.get("dynamic_tools") or {}),
        raw=raw,
    )
    try:  # 형식 오류를 시작 시점에 알림
        from .downloader import DownloadConfig
        from .dynamic_tools import DynamicToolsConfig

        DownloadConfig.from_dict(app.downloads)
        DynamicToolsConfig.from_dict(app.dynamic_tools)
    except (ValueError, TypeError) as e:
        raise ConfigError(f"downloads/dynamic_tools 설정 오류: {e}") from e
    if files.inject_env:
        ws = app.workspace_path.resolve()
        for srv in servers.values():
            if srv.transport == "stdio":
                srv.env.setdefault("AGENT_DOWNLOAD_DIR", str(ws / "downloads"))
                srv.env.setdefault("AGENT_WORKSPACE", str(ws))
    return app
