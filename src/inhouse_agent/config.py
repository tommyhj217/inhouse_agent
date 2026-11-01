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
    max_iterations: int = 25
    max_tool_result_chars: int = 30000
    max_history_messages: int = 60
    parallel_tool_calls: bool = True
    output_dir: str = "outputs"
    log_dir: str = "logs"
    timezone: str = "Asia/Seoul"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        d = d or {}
        return cls(
            system_prompt=str(d.get("system_prompt") or ""),
            max_iterations=_to_int(d.get("max_iterations"), 25) or 25,
            max_tool_result_chars=_to_int(d.get("max_tool_result_chars"), 30000) or 30000,
            max_history_messages=_to_int(d.get("max_history_messages"), 60) or 60,
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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScheduleConfig":
        if not d.get("name"):
            raise ConfigError("schedules 항목에 name 이 없습니다.")
        if not d.get("prompt"):
            raise ConfigError(f"schedules.{d['name']}: prompt 가 없습니다.")
        if not d.get("cron"):
            raise ConfigError(f"schedules.{d['name']}: cron 이 없습니다.")
        servers = d.get("servers")
        return cls(
            name=str(d["name"]),
            prompt=str(d["prompt"]),
            cron=str(d["cron"]),
            timezone=_none_if_blank(d.get("timezone")),
            servers=[str(s) for s in servers] if servers else None,
            enabled=_to_bool(d.get("enabled"), True),
            output_format=str(d.get("output_format") or "md").lower(),
            webhook_url=_none_if_blank(d.get("webhook_url")),
            description=str(d.get("description") or ""),
        )


@dataclass
class AppConfig:
    llm: LLMConfig
    agent: AgentConfig
    mcp_servers: dict[str, MCPServerConfig]
    schedules: list[ScheduleConfig]
    base_dir: Path

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

    return AppConfig(
        llm=LLMConfig.from_dict(raw.get("llm") or {}),
        agent=AgentConfig.from_dict(raw.get("agent") or {}),
        mcp_servers=servers,
        schedules=schedules,
        base_dir=base_dir,
    )
