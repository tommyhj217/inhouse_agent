"""파일 다운로드 링크.

- 웹 화면: 답변/tool 결과에 나온 파일 경로를 클릭하면 바로 내려받기
- 공유용 서명 링크: /d/<토큰>/<파일명>  (API 토큰 없이 열림, 기한 있음, 위조 불가)
  메일·메신저·웹훅으로 보낼 때는 server.public_url 을 설정하면 절대 URL 이 만들어집니다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any


class LinkError(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class LinkSigner:
    def __init__(self, workspace: Any, public_url: str | None = None, expire_hours: float = 168):
        self.ws = workspace
        self.public_url = (public_url or "").rstrip("/")
        self.expire_hours = expire_hours
        self._secret = self._load_secret()

    def _load_secret(self) -> bytes:
        env = os.environ.get("AGENT_LINK_SECRET")
        if env:
            return env.encode()
        f = Path(self.ws.root) / ".link_secret"
        if f.is_file():
            return f.read_bytes()
        key = secrets.token_bytes(32)
        f.write_bytes(key)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return key

    def sign(self, path: Path | str, expire_hours: float | None = None) -> str:
        """서명 토큰 생성 (경로는 허용 폴더 안이어야 함)."""
        p = self.ws.resolve(path)
        if not p.exists():
            raise LinkError(f"파일이 없습니다: {p}")
        hours = self.expire_hours if expire_hours is None else expire_hours
        payload = _b64(json.dumps({"p": str(p), "e": int(time.time() + hours * 3600)}, ensure_ascii=False).encode())
        sig = _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()[:18])
        return f"{payload}.{sig}"

    def verify(self, token: str) -> Path:
        try:
            payload, sig = token.split(".", 1)
        except ValueError as e:
            raise LinkError("잘못된 링크입니다.") from e
        good = _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(sig, good):
            raise LinkError("잘못된 링크입니다.")
        data = json.loads(_unb64(payload))
        if time.time() > data["e"]:
            raise LinkError("만료된 링크입니다. 다시 링크를 받으세요.")
        p = self.ws.resolve(data["p"])  # 그 사이 설정이 바뀌어도 허용 폴더 밖은 막음
        if not p.exists():
            raise LinkError("파일이 더 이상 없습니다.")
        return p

    def url(self, path: Path | str, expire_hours: float | None = None, absolute: bool = True) -> str:
        token = self.sign(path, expire_hours)
        name = Path(path).name
        rel = f"/d/{token}/{urllib.parse.quote(name)}"
        return (self.public_url + rel) if (absolute and self.public_url) else rel

    def describe(self, path: Path | str, expire_hours: float | None = None) -> dict[str, Any]:
        p = self.ws.resolve(path)
        hours = self.expire_hours if expire_hours is None else expire_hours
        return {
            "path": str(p),
            "name": p.name,
            "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else None,
            "url": self.url(p, hours),
            "absolute": bool(self.public_url),
            "expires_in_hours": hours,
        }
