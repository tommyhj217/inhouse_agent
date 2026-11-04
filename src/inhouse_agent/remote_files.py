"""원격 MCP 서버의 파일을 에이전트 PC 로 가져오기.

MCP 서버가 다른 PC 에서 돌면 그 서버가 알려주는 경로(C:\\mcp\\downloads\\a.xlsx)나
localhost 링크(http://localhost:9000/f/1)는 에이전트 PC 에서 그대로 쓸 수 없습니다.
아래 순서로 파일을 가져와 workspace/downloads/<서버이름>/ 에 저장합니다.

  1) path_map   : 원격 경로 접두어 → 에이전트 PC 의 공유폴더 경로 또는 URL
  2) fetch_tool : 원격 MCP 의 '파일 내용 돌려주기' tool 호출 (blob / base64)
  3) resources  : MCP resources/read (file:///경로)
  결과 텍스트의 localhost 링크는 MCP 서버 주소로 바꿉니다.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import socket
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

log = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
_URL_RE = re.compile(r"https?://[^\s\"'<>\]\)\}|,]+", re.I)
_BOUND = r"(?=$|[\s\"'<>|,;)\]}、。:!?]|[가-힣]|\.(?:\s|$))"
# 원격 경로: Windows(C:\..), UNC(\\서버\..), POSIX(/a/b/파일.ext — 폴더가 하나 이상 있어야 함), file:// URI
_REMOTE_PATH_RE = re.compile(
    r"file:///?[^\s\"'<>|]+"
    r"|(?:[A-Za-z]:[\\/]|\\\\[^\s\\/]+[\\/])(?:[^\s\"'<>|*?\\/]+[\\/])*[^\"'<>|*?\\/\n]*?\.[A-Za-z0-9]{1,6}" + _BOUND +
    r"|/(?:[^\s\"'<>|*?:/]+/)+[^\"'<>|*?:/\n]*?\.[A-Za-z0-9]{1,6}" + _BOUND
)


@dataclass
class RemoteFilesConfig:
    path_map: dict[str, str] = field(default_factory=dict)
    fetch_tool: str | None = None
    fetch_arg: str = "path"
    use_resources: bool = True
    rewrite_localhost: bool = True
    auto_fetch: bool = True
    max_files: int = 5

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RemoteFilesConfig":
        d = d or {}
        return cls(
            path_map={str(k): str(v) for k, v in (d.get("path_map") or {}).items() if k and v},
            fetch_tool=(str(d["fetch_tool"]).strip() or None) if d.get("fetch_tool") else None,
            fetch_arg=str(d.get("fetch_arg") or "path"),
            use_resources=d.get("use_resources", True) is not False,
            rewrite_localhost=d.get("rewrite_localhost", True) is not False,
            auto_fetch=d.get("auto_fetch", True) is not False,
            max_files=int(d.get("max_files") or 5),
        )

    def local_targets(self) -> list[Path]:
        """path_map 중 URL 이 아닌 대상 (에이전트 PC 의 공유폴더) - 다운로드 감시 폴더에 추가."""
        return [Path(v) for v in self.path_map.values() if not re.match(r"^https?://", v, re.I)]


def is_remote(cfg: Any) -> bool:
    """MCP 서버가 다른 PC 인지. remote: true/false 로 지정하거나, auto 면 URL 호스트로 판단."""
    mode = getattr(cfg, "remote", "auto")
    if mode is True or str(mode).lower() in ("true", "yes", "1", "on"):
        return True
    if mode is False or str(mode).lower() in ("false", "no", "0", "off"):
        return False
    if cfg.transport == "stdio":
        return False
    host = (urllib.parse.urlparse(cfg.url or "").hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return False
    try:
        if host in (socket.gethostname().lower(), socket.getfqdn().lower()):
            return False
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def rewrite_localhost(text: str, server_url: str) -> tuple[str, list[tuple[str, str]]]:
    """'http://localhost:9000/x' → 'http://<MCP서버호스트>:9000/x'. (바뀐 텍스트, [(원래, 바꾼)])"""
    host = urllib.parse.urlparse(server_url).hostname
    if not host or host.lower() in _LOCAL_HOSTS:
        return text, []
    changes: list[tuple[str, str]] = []

    def repl(m: re.Match[str]) -> str:
        u = urllib.parse.urlparse(m.group(0))
        if (u.hostname or "").lower() not in _LOCAL_HOSTS:
            return m.group(0)
        netloc = host + (f":{u.port}" if u.port else "")
        new = urllib.parse.urlunparse(u._replace(netloc=netloc))
        changes.append((m.group(0), new))
        return new

    return _URL_RE.sub(repl, text), changes


def find_remote_paths(text: str) -> list[str]:
    stripped = _URL_RE.sub(" ", text)  # http 링크 안의 /경로 는 제외
    out: list[str] = []
    for m in _REMOTE_PATH_RE.finditer(stripped):
        p = m.group(0).rstrip(".,;:)")
        if p not in out:
            out.append(p)
    return out


def _norm(p: str) -> str:
    if p.lower().startswith("file:"):
        u = urllib.parse.urlparse(p)
        p = urllib.parse.unquote(u.path)
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
        if u.netloc:  # file://server/share → UNC
            p = f"//{u.netloc}{p}"
    return p.replace("\\", "/")


def map_path(remote: str, path_map: dict[str, str]) -> str | None:
    """가장 긴 접두어가 맞는 path_map 항목으로 변환 (Windows 경로는 대소문자 무시)."""
    r = _norm(remote)
    best = None
    for src, dst in path_map.items():
        s = _norm(src).rstrip("/")
        win = bool(re.match(r"^[A-Za-z]:|^//", s))
        if (r.lower() if win else r).startswith((s.lower() if win else s) + "/") and (best is None or len(s) > len(best[0])):
            best = (s, dst)
    if best is None:
        return None
    rest = r[len(best[0]) + 1 :]
    dst = best[1]
    if re.match(r"^https?://", dst, re.I):
        return dst.rstrip("/") + "/" + "/".join(urllib.parse.quote(x) for x in rest.split("/"))
    return str(Path(dst) / Path(*rest.split("/")))


def remote_name(remote: str) -> str:
    r = _norm(remote)
    return (PureWindowsPath(r) if re.match(r"^[A-Za-z]:", r) else PurePosixPath(r)).name or "remote_file"


class RemoteFileFetcher:
    """원격 경로 → 에이전트 PC 의 파일. hub 가 MCP tool 결과를 받을 때 자동 호출."""

    def __init__(self, hub: Any):
        self.hub = hub

    async def fetch(self, conn: Any, remote: str, *, explicit: bool = False) -> tuple[Path | None, str]:
        """(저장된 로컬 경로 또는 None, 설명)"""
        rcfg: RemoteFilesConfig = conn.remote_files
        ws = self.hub.workspace
        name = remote_name(remote)
        notes: list[str] = []

        # 1) path_map
        mapped = map_path(remote, rcfg.path_map)
        if mapped:
            if re.match(r"^https?://", mapped, re.I):
                if self.hub.downloader is None:
                    notes.append("다운로드 기능이 꺼져 있음")
                else:
                    from .downloader import DownloadError

                    try:
                        info = await asyncio.to_thread(self.hub.downloader.download, mapped, subdir=conn.name, filename=name, overwrite=True)
                        return Path(info["path"]), f"path_map URL 로 받음: {mapped}"
                    except DownloadError as e:
                        notes.append(f"path_map URL 실패({mapped}): {e}")
            else:
                lp = Path(mapped)
                try:
                    lp = ws.resolve(lp)
                except PermissionError as e:
                    notes.append(f"path_map 경로가 허용 폴더 밖입니다({lp}): {e}")
                else:
                    if lp.exists():
                        return lp, f"공유폴더 경로로 변환: {lp}"
                    notes.append(f"공유폴더에 아직 없음: {lp}")

        # 2) fetch_tool
        if rcfg.fetch_tool:
            saved: list[Path] = []
            text, is_err, saved = await conn.call(
                rcfg.fetch_tool, {rcfg.fetch_arg: remote}, lambda data, nm, mime: ws.save_blob(data, name, mime, conn.name)
            )
            if saved:
                return saved[0], f"원격 MCP '{rcfg.fetch_tool}' 로 내용을 받아 저장"
            data = conn.last_data if not is_err else None
            # 구조화 결과의 base64 필드 우선, 없으면 결과 전체가 긴 base64 문자열인 경우만 인정
            raw = _b64_from(data) or (_b64_from(text) if not is_err and len(text.strip()) >= 64 else None)
            if raw is not None:
                return ws.save_blob(raw, name, None, conn.name), f"원격 MCP '{rcfg.fetch_tool}' 로 받음(base64)"
            notes.append(f"{rcfg.fetch_tool} 실패: {text[:200]}")

        # 3) MCP resources/read
        if rcfg.use_resources:
            uri = remote if remote.lower().startswith("file:") else Path(_norm(remote) if _norm(remote).startswith("/") else "/" + _norm(remote)).as_uri()
            try:
                p = await conn.read_resource_to_file(uri, lambda data, nm, mime: ws.save_blob(data, name, mime, conn.name))
                if p is not None:
                    return p, "MCP resource 로 받음"
                notes.append("resource 에 내용 없음")
            except Exception as e:  # noqa: BLE001 - 서버가 resource 를 지원하지 않는 경우가 많음
                notes.append(f"resource 없음({type(e).__name__})")

        hint = (
            "원격 MCP 서버 PC 의 경로라 에이전트가 직접 열 수 없습니다. "
            f"config.yaml 의 mcp_servers.{conn.name}.remote_files 에 path_map(공유폴더/URL) 또는 fetch_tool 을 설정하세요."
        )
        return None, hint + (" [" + "; ".join(notes) + "]" if notes else "")


def _b64_from(obj: Any) -> bytes | None:
    """{"base64": "..."} / {"content_base64": ...} / {"data": ...} 또는 순수 base64 문자열."""
    if isinstance(obj, dict):
        for k in ("base64", "content_base64", "data", "content", "blob", "file"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) > 8:
                b = _b64_from(v)
                if b is not None:
                    return b
        return None
    if isinstance(obj, str):
        s = obj.strip()
        if len(s) < 8 or not re.fullmatch(r"[A-Za-z0-9+/=\s_-]+", s):
            return None
        try:
            return base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_" if ("-" in s or "_" in s) else None, validate=False)
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            return None
    return None
