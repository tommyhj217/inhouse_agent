"""사용자별 작업폴더와 대화 세션 저장.

- 사용자 ID 를 받으면 workspace/users/<ID>/ 아래에 downloads, uploads, files, results, sessions, memory.json 을 둡니다.
- 실행 중인 사용자는 ContextVar(current_user) 로 전달되고, 모든 도구는 WorkspaceProxy 를 통해
  '지금 사용자의' 작업폴더를 씁니다 (MCP 연결, 스킬, 동적 도구는 모두가 공유).
- 대화(세션)는 sessions/<id>.json 에 저장되어 다음에 그대로 이어서 할 수 있습니다.

※ 사용자 ID 는 인증이 아닙니다 (사내 식별용). 접근 통제가 필요하면 AGENT_API_TOKEN 과 함께 쓰세요.
"""

from __future__ import annotations

import contextvars
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .files import FileWorkspace

USER_RE = re.compile(r"^[\w][\w.@-]{0,63}$", re.UNICODE)
current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)


class UserError(ValueError):
    pass


def normalize_user(uid: str | None) -> str | None:
    if uid is None:
        return None
    uid = str(uid).strip()
    if not uid:
        return None
    if not USER_RE.match(uid) or ".." in uid:
        raise UserError("사용자 ID 는 한글/영문/숫자와 . _ - @ 만 쓸 수 있습니다 (최대 64자).")
    return uid


@dataclass
class UsersConfig:
    enabled: bool = True
    require: bool = False  # true 면 웹 화면에서 ID 없이는 사용 불가
    allowed: list[str] = field(default_factory=list)  # 허용 ID 패턴 (비우면 모두)
    claim_downloads: str = "move"  # 공용 다운로드 폴더에 생긴 파일을 사용자 폴더로: move | copy | none
    recent_sessions_in_prompt: int = 5

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "UsersConfig":
        d = d or {}
        claim = str(d.get("claim_downloads") or "move").lower()
        if claim not in ("move", "copy", "none"):
            raise ValueError("users.claim_downloads 는 move / copy / none 중 하나여야 합니다.")
        return cls(
            enabled=d.get("enabled", True) is not False,
            require=bool(d.get("require", False)),
            allowed=[str(x) for x in d.get("allowed") or []],
            claim_downloads=claim,
            recent_sessions_in_prompt=int(d.get("recent_sessions_in_prompt") or 5),
        )


class UserWorkspaces:
    """사용자 ID → FileWorkspace (캐시)."""

    def __init__(self, base: FileWorkspace, cfg: UsersConfig):
        self.base = base
        self.cfg = cfg
        self.root = base.root / "users"
        self._cache: dict[str, FileWorkspace] = {}
        self._lock = threading.Lock()

    def check(self, uid: str) -> str:
        import fnmatch

        uid = normalize_user(uid) or ""
        if not uid:
            raise UserError("사용자 ID 가 비어 있습니다.")
        if self.cfg.allowed and not any(fnmatch.fnmatch(uid, p) for p in self.cfg.allowed):
            raise UserError(f"허용되지 않은 사용자 ID 입니다: {uid}")
        return uid

    def get(self, uid: str) -> FileWorkspace:
        uid = self.check(uid)
        with self._lock:
            ws = self._cache.get(uid)
            if ws is None:
                root = self.root / uid
                # 공용 다운로드 폴더(MCP 가 저장하는 곳)와 설정의 감시 폴더도 계속 감시 → 생긴 파일은 사용자 폴더로 옮김
                watch = [self.base.downloads] + [d for d in self.base.watch_dirs if d != self.base.downloads]
                ws = FileWorkspace(root, watch_dirs=watch, extra_read_dirs=list(self.base.extra_read_dirs), max_file_mb=self.base.max_file_mb)
                (root / "results").mkdir(exist_ok=True)
                (root / "sessions").mkdir(exist_ok=True)
                self._cache[uid] = ws
            return ws

    def current(self) -> FileWorkspace:
        uid = current_user.get()
        return self.get(uid) if (uid and self.cfg.enabled) else self.base

    def list_users(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []


class WorkspaceProxy:
    """도구들이 쥐고 있는 workspace. 호출 시점의 사용자 작업폴더로 위임."""

    def __init__(self, users: UserWorkspaces):
        object.__setattr__(self, "_users", users)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._users.current(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._users.current(), name, value)

    @property
    def is_user(self) -> bool:
        return self._users.current() is not self._users.base

    @property
    def user(self) -> str | None:
        return current_user.get() if self.is_user else None

    def shared_dirs(self) -> list[Path]:
        """사용자 폴더 밖의 감시 폴더 (공용 다운로드 등)."""
        ws = self._users.current()
        return [d for d in ws.watch_dirs if d != ws.root and ws.root not in d.parents]

    def claim(self, path: Path) -> Path:
        """공용 폴더에 생긴 파일을 지금 사용자의 downloads 로 옮김/복사 (설정에 따름)."""
        mode = self._users.cfg.claim_downloads
        ws = self._users.current()
        if mode == "none" or not self.is_user:
            return path
        p = Path(path).resolve()
        if ws.root == p or ws.root in p.parents or not any(d == p.parent or d in p.parents for d in self.shared_dirs()):
            return path
        target = ws.downloads / p.name
        if target.exists():
            target = ws.downloads / f"{p.stem}_{datetime.now():%H%M%S}{p.suffix}"
        try:
            (shutil.move if mode == "move" else shutil.copy2)(str(p), str(target))
        except OSError:
            return path  # 다른 프로그램이 잡고 있으면 그대로 둠
        return target


# ---------------------------------------------------------------------------
# 세션(대화) 저장
# ---------------------------------------------------------------------------


def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """저장용: 이미지 data URL 은 파일 경로 참조로 바꿈 (용량/재사용)."""
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), list):
            paths = list(m.get("_images") or [])
            parts = []
            for part in m["content"]:
                if part.get("type") == "image_url":
                    parts.append({"type": "text", "text": f"[첨부 이미지: {paths.pop(0) if paths else '(경로 없음)'}]"})
                else:
                    parts.append(part)
            m["content"] = parts
        out.append(m)
    return out


class SessionStore:
    """사용자 작업폴더의 sessions/<id>.json"""

    def __init__(self, ws: Any):
        self.ws = ws

    @property
    def dir(self) -> Path:
        d = Path(self.ws.root) / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, sid: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", sid):
            raise UserError("잘못된 세션 ID")
        return self.dir / f"{sid}.json"

    def exists(self, sid: str) -> bool:
        try:
            return self._path(sid).is_file()
        except UserError:
            return False

    def load(self, sid: str) -> dict[str, Any]:
        p = self._path(sid)
        if not p.is_file():
            raise UserError(f"세션이 없습니다: {sid}")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        *,
        turn: dict[str, Any] | None = None,
        skill: str | None = None,
        inputs: dict[str, Any] | None = None,
        servers: list[str] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        p = self._path(sid)
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"id": sid, "created": time.time(), "turns": []}
        if turn is not None:
            data["turns"].append(turn)
            if not data.get("title"):
                data["title"] = (turn.get("prompt") or (f"[스킬] {skill}" if skill else "대화"))[:60]
        if title:
            data["title"] = title[:80]
        data.update(updated=time.time(), skill=skill, inputs=inputs, servers=servers, messages=_clean_messages(messages))
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
        return data

    def rename(self, sid: str, title: str) -> None:
        data = self.load(sid)
        data["title"] = title[:80]
        self._path(sid).write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    def delete(self, sid: str) -> None:
        p = self._path(sid)
        if p.is_file():
            trash = self.dir / ".trash"
            trash.mkdir(exist_ok=True)
            p.replace(trash / p.name)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        items = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            files = [f for t in d.get("turns", []) for f in t.get("files", [])]
            items.append(
                {
                    "id": d.get("id", p.stem),
                    "title": d.get("title") or "(제목 없음)",
                    "created": d.get("created"),
                    "updated": d.get("updated"),
                    "turns": len(d.get("turns", [])),
                    "skill": d.get("skill"),
                    "last_output": (d.get("turns") or [{}])[-1].get("output", "")[:200],
                    "files": list(dict.fromkeys(files))[-10:],
                }
            )
        return items

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:16]

    def recent_summary(self, n: int) -> str:
        """시스템 프롬프트용: 최근 작업 목록."""
        lines = []
        for s in self.list(n):
            when = datetime.fromtimestamp(s["updated"] or 0).strftime("%m-%d %H:%M")
            files = ", ".join(Path(f).name for f in s["files"][-3:])
            lines.append(f"- [{s['id']}] {when} {s['title']}" + (f" (파일: {files})" if files else ""))
        return "\n".join(lines)
