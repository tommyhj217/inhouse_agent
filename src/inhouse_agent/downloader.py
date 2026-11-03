"""MCP 가 알려준 다운로드 URL 을 직접 받아오는 모듈 (표준 라이브러리만 사용).

- 사내 프록시(HTTP(S)_PROXY / NO_PROXY), 사내 인증서(verify: CA 파일 경로) 지원
- 사이트별 헤더/쿠키 설정 + tool 인자로 받은 쿠키/헤더
- Content-Disposition 의 한글 파일명(filename*=UTF-8'', cp949/latin-1 깨짐) 복원
- .part 로 받다가 완료되면 이름 변경, 크기 제한, 재시도
- 받은 뒤 파일 상태 점검 → 로그인 페이지(HTML)가 받아졌으면 쿠키/브라우저 다운로드를 안내
"""

from __future__ import annotations

import fnmatch
import ipaddress
import mimetypes
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

URL_RE = re.compile(r"https?://[^\s\"'<>\]\)\}|,]+", re.I)
_DOWNLOADISH = re.compile(
    r"(\.(xlsx?|xlsm|csv|tsv|pdf|docx?|pptx?|hwpx?|zip|7z|json|xml|txt|png|jpe?g|gif)(\?|#|$))"
    r"|download|attach|export|filedown|getfile|fileid|file_id|/files?/",
    re.I,
)


class DownloadError(RuntimeError):
    pass


@dataclass
class SiteRule:
    match: str  # 호스트 패턴: "*.company.com", "erp.company.com"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadConfig:
    allowed_hosts: list[str] = field(default_factory=lambda: ["*"])
    blocked_hosts: list[str] = field(default_factory=list)
    verify: bool | str = True  # True | False | CA 번들 경로
    timeout: float = 120.0
    max_mb: int = 500
    retries: int = 2
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) inhouse-agent/1.0"
    auto_download: bool = False  # MCP 결과에 다운로드 링크가 있으면 자동으로 받을지
    sites: list[SiteRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "DownloadConfig":
        d = d or {}
        verify: bool | str = d.get("verify", True)
        if isinstance(verify, str) and verify.strip().lower() in ("true", "false", "1", "0", "yes", "no", ""):
            verify = verify.strip().lower() in ("true", "1", "yes", "")
        return cls(
            allowed_hosts=[str(h) for h in (d.get("allowed_hosts") or ["*"])],
            blocked_hosts=[str(h) for h in (d.get("blocked_hosts") or [])],
            verify=verify,
            timeout=float(d.get("timeout") or 120),
            max_mb=int(d.get("max_mb") or 500),
            retries=int(d.get("retries") if d.get("retries") is not None else 2),
            user_agent=str(d.get("user_agent") or cls.user_agent),
            auto_download=str(d.get("auto_download", False)).lower() in ("1", "true", "yes", "on"),
            sites=[
                SiteRule(
                    str(s.get("match")),
                    {k: str(v) for k, v in (s.get("headers") or {}).items() if v not in (None, "")},
                    {k: str(v) for k, v in (s.get("cookies") or {}).items() if v not in (None, "")},
                )
                for s in d.get("sites") or []
                if s.get("match")
            ],
        )


def find_urls(text: str, only_downloadish: bool = True) -> list[str]:
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".;:")
        if (not only_downloadish or _DOWNLOADISH.search(u)) and u not in out:
            out.append(u)
    return out[:10]


def parse_cookie_string(s: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and k.strip().lower() not in ("path", "domain", "expires", "max-age", "secure", "httponly", "samesite"):
                out[k.strip()] = v.strip()
    return out


def _fix_mojibake(name: str) -> str:
    """http.client 는 헤더를 latin-1 로 디코딩 → 원래 UTF-8/CP949 바이트로 되돌려 재해석."""
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def filename_from_headers(cd: str | None, url: str, content_type: str | None) -> str:
    name = ""
    if cd:
        m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", cd, re.I)
        if m:
            enc = (m.group(1) or "utf-8").strip() or "utf-8"
            try:
                name = urllib.parse.unquote(m.group(2).strip().strip('"'), encoding=enc)
            except LookupError:
                name = urllib.parse.unquote(m.group(2).strip().strip('"'))
        if not name:
            m = re.search(r'filename\s*=\s*"([^"]*)"', cd, re.I) or re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
            if m:
                name = _fix_mojibake(m.group(1).strip())
                if "%" in name:
                    name = urllib.parse.unquote(name)
    if not name:
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(path.rstrip("/").split("/")[-1]) if path else ""
        if not Path(name).suffix:
            # ?filename=x.xlsx / ?file=... 같은 쿼리에서 찾기
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            for key in ("filename", "fileName", "file_name", "name", "file", "fname"):
                if q.get(key) and Path(q[key][0]).suffix:
                    name = Path(q[key][0]).name
                    break
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .") or "download"
    if not Path(name).suffix:
        ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
        if ext in (".htm", ".html") and "download" in name:
            ext = ".html"
        name += ext or ".bin"
    return name[:180]


class Downloader:
    def __init__(self, cfg: DownloadConfig, workspace: Any):
        self.cfg = cfg
        self.ws = workspace

    # ------------------------------------------------------------------
    def check_url(self, url: str) -> urllib.parse.ParseResult:
        u = urllib.parse.urlparse(url.strip())
        if u.scheme not in ("http", "https"):
            raise DownloadError(f"http/https URL 만 받을 수 있습니다: {url}")
        host = (u.hostname or "").lower()
        if not host:
            raise DownloadError(f"호스트가 없는 URL 입니다: {url}")
        if any(fnmatch.fnmatch(host, p.lower()) for p in self.cfg.blocked_hosts):
            raise DownloadError(f"차단된 호스트입니다: {host}")
        if not any(fnmatch.fnmatch(host, p.lower()) for p in self.cfg.allowed_hosts):
            raise DownloadError(f"허용되지 않은 호스트입니다: {host} (downloads.allowed_hosts 설정 확인)")
        # 클라우드 메타데이터 주소 등 링크로컬 차단
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_link_local:
                    raise DownloadError(f"링크로컬 주소는 받을 수 없습니다: {host}")
        except socket.gaierror:
            pass  # DNS 실패는 실제 요청에서 오류로 처리
        return u

    def _ssl_context(self) -> ssl.SSLContext:
        v = self.cfg.verify
        if v is False:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if isinstance(v, str) and v:
            return ssl.create_default_context(cafile=v)
        return ssl.create_default_context()

    def _site_rules(self, host: str) -> tuple[dict[str, str], dict[str, str]]:
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        for r in self.cfg.sites:
            if fnmatch.fnmatch(host, r.match.lower()):
                headers.update(r.headers)
                cookies.update(r.cookies)
        return headers, cookies

    # ------------------------------------------------------------------
    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | str | None = None,
        json_body: Any = None,
        form: dict[str, Any] | None = None,
        body: str | None = None,
        timeout: float | None = None,
        max_bytes: int = 5 * 1024 * 1024,
    ) -> dict[str, Any]:
        """일반 HTTP 요청 (http_request / web_fetch / web_search 용). 응답 본문은 max_bytes 까지만 읽음.

        반환: {status, reason, headers, body(bytes), truncated, final_url, elapsed}
        HTTP 오류 응답(4xx/5xx)도 예외 없이 status 로 돌려줌.
        """
        import json as _json

        if params:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        u = self.check_url(url)
        site_headers, site_cookies = self._site_rules((u.hostname or "").lower())
        hdrs = {"User-Agent": self.cfg.user_agent, "Accept": "*/*", **site_headers, **(headers or {})}
        ck = dict(site_cookies)
        ck.update(parse_cookie_string(cookies) if isinstance(cookies, str) else (cookies or {}))
        if ck:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())
        data: bytes | None = None
        if json_body is not None:
            data = _json.dumps(json_body, ensure_ascii=False).encode()
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif body is not None:
            data = str(body).encode()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context()), urllib.request.HTTPCookieProcessor()
        )
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        t0 = time.monotonic()
        try:
            resp = opener.open(req, timeout=timeout or self.cfg.timeout)
        except urllib.error.HTTPError as e:
            resp = e  # 오류 응답도 본문을 읽어서 돌려줌
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DownloadError(f"연결 실패: {getattr(e, 'reason', e)}") from e
        with resp:
            raw = resp.read(max_bytes + 1)
            return {
                "status": getattr(resp, "status", None) or resp.getcode(),
                "reason": getattr(resp, "reason", ""),
                "headers": {k: v for k, v in resp.headers.items()},
                "body": raw[:max_bytes],
                "truncated": len(raw) > max_bytes,
                "final_url": resp.geturl(),
                "elapsed": round(time.monotonic() - t0, 2),
            }

    def download(
        self,
        url: str,
        *,
        filename: str | None = None,
        subdir: str = "",
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | str | None = None,
        method: str = "GET",
        data: dict[str, Any] | str | None = None,
        referer: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """동기 다운로드 (asyncio.to_thread 로 호출). 결과: {path, size, content_type, status, final_url, elapsed}"""
        u = self.check_url(url)
        host = (u.hostname or "").lower()
        site_headers, site_cookies = self._site_rules(host)
        hdrs = {"User-Agent": self.cfg.user_agent, "Accept": "*/*", **site_headers, **(headers or {})}
        ck = dict(site_cookies)
        ck.update(parse_cookie_string(cookies) if isinstance(cookies, str) else (cookies or {}))
        if ck:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())
        if referer:
            hdrs["Referer"] = referer
        body: bytes | None = None
        if data is not None:
            if isinstance(data, dict):
                body = urllib.parse.urlencode(data).encode()
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            else:
                body = str(data).encode()
            method = "POST" if method.upper() == "GET" else method.upper()

        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context()),
            urllib.request.HTTPCookieProcessor(),  # 리다이렉트 중 받은 쿠키 유지
        )
        last: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            t0 = time.monotonic()
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
            try:
                with opener.open(req, timeout=self.cfg.timeout) as resp:
                    ctype = resp.headers.get("Content-Type")
                    name = filename or filename_from_headers(resp.headers.get("Content-Disposition"), resp.geturl(), ctype)
                    folder = self.ws.downloads / subdir if subdir else self.ws.downloads
                    folder = self.ws.resolve(folder, write=True)
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / Path(name).name
                    if target.exists() and not overwrite:
                        target = folder / f"{target.stem}_{datetime.now():%H%M%S}{target.suffix}"
                    part = target.with_name(target.name + ".part")
                    limit = self.cfg.max_mb * 1024 * 1024
                    expected = int(resp.headers.get("Content-Length") or 0)
                    size = 0
                    try:
                        with open(part, "wb") as f:
                            while True:
                                chunk = resp.read(1 << 16)
                                if not chunk:
                                    break
                                size += len(chunk)
                                if size > limit:
                                    raise DownloadError(f"{self.cfg.max_mb}MB 를 넘어 중단했습니다.")
                                f.write(chunk)
                        if expected and size < expected:
                            raise DownloadError(f"응답이 중간에 끊겼습니다 ({size}/{expected} bytes)")
                        part.replace(target)
                    finally:
                        if part.exists():
                            part.unlink(missing_ok=True)
                    return {
                        "path": str(target),
                        "size": size,
                        "content_type": ctype,
                        "status": resp.status,
                        "final_url": resp.geturl(),
                        "redirected": resp.geturl() != url,
                        "elapsed": round(time.monotonic() - t0, 2),
                    }
            except urllib.error.HTTPError as e:
                hint = {401: "인증 필요 - 쿠키/토큰을 확인하세요", 403: "권한 없음 - 쿠키/권한 확인", 404: "파일 없음 - URL 만료 가능성"}.get(e.code, "")
                last = DownloadError(f"HTTP {e.code} {e.reason}" + (f" ({hint})" if hint else ""))
                if e.code < 500:
                    break
            except DownloadError as e:
                last = e
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = DownloadError(f"연결 실패: {getattr(e, 'reason', e)}")
            if attempt < self.cfg.retries:
                time.sleep(1.5 * (attempt + 1))
        raise last or DownloadError("다운로드 실패")
