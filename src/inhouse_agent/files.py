"""파일 작업공간: 다운로드 감지, 파일 상태 점검(완료/진행중/손상/의심), 다운로드 대기.

웹컨트롤 MCP 가 파일을 내려받는 방식은 보통 셋 중 하나입니다.
  1) 브라우저가 다운로드 폴더에 저장 (비동기 - tool 이 끝나도 아직 받는 중일 수 있음)
  2) tool 결과 텍스트에 저장 경로를 알려줌
  3) tool 결과에 파일 내용을 바이너리(EmbeddedResource blob / image)로 담아 보냄
에이전트는 세 경우 모두 파일을 찾아 상태를 점검한 뒤 LLM 에 알려줍니다.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import mimetypes
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 브라우저/다운로드 도구가 받는 중에 쓰는 임시 확장자
TEMP_SUFFIXES = {".crdownload", ".part", ".partial", ".download", ".opdownload", ".tmp", ".filepart"}
_SNIFF = 4096


class FileAccessError(PermissionError):
    pass


@dataclass
class FileWorkspace:
    root: Path
    watch_dirs: list[Path] = field(default_factory=list)
    extra_read_dirs: list[Path] = field(default_factory=list)
    max_file_mb: int = 200

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        for sub in ("downloads", "uploads", "files"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        dirs = [self.downloads] + [Path(d).expanduser().resolve() for d in self.watch_dirs]
        seen: list[Path] = []
        for d in dirs:
            if d not in seen:
                seen.append(d)
        self.watch_dirs = seen
        self.extra_read_dirs = [Path(d).expanduser().resolve() for d in self.extra_read_dirs]

    # ------------------------------------------------------------------
    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def files(self) -> Path:
        return self.root / "files"

    @property
    def readable_roots(self) -> list[Path]:
        return [self.root, *self.watch_dirs, *self.extra_read_dirs]

    def resolve(self, path: str | os.PathLike[str], *, write: bool = False) -> Path:
        """사용자/LLM 이 준 경로 → 허용된 폴더 안의 절대경로. 상대경로는 workspace 기준."""
        raw = str(path).strip().strip('"').strip("'")
        if raw.startswith("file://"):
            raw = raw[7:]
            if re.match(r"^/[A-Za-z]:/", raw):  # file:///C:/...
                raw = raw[1:]
        p = Path(raw).expanduser()
        if not p.is_absolute():
            # "downloads/x.xlsx" 처럼 workspace 기준, 없으면 다운로드 폴더 기준으로도 찾아봄
            cand = (self.root / p).resolve()
            if not cand.exists() and not write:
                for d in self.watch_dirs:
                    alt = (d / p).resolve()
                    if alt.exists():
                        cand = alt
                        break
            p = cand
        else:
            p = p.resolve()
        roots = [self.root] if write else self.readable_roots
        if not any(p == r or r in p.parents for r in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise FileAccessError(f"허용되지 않은 경로입니다: {p} (허용 폴더: {allowed})")
        return p

    def display(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(p)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, tuple[int, float]]:
        """감시 폴더(최상위 + 1단계 하위)의 파일 목록 스냅샷 {경로: (크기, 수정시각)}."""
        snap: dict[str, tuple[int, float]] = {}
        for d in self.watch_dirs:
            if not d.is_dir():
                continue
            stack = [(d, 0)]
            while stack:
                cur, depth = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for e in it:
                            try:
                                if e.is_file():
                                    if e.name.startswith((".", "~$")):  # 숨김/Office 잠금 파일 제외
                                        continue
                                    st = e.stat()
                                    snap[e.path] = (st.st_size, st.st_mtime)
                                elif e.is_dir() and depth < 1 and not e.name.startswith("."):
                                    stack.append((Path(e.path), depth + 1))
                            except OSError:
                                continue
                except OSError:
                    continue
        return snap

    @staticmethod
    def diff(before: dict[str, tuple[int, float]], after: dict[str, tuple[int, float]]) -> list[Path]:
        return [Path(p) for p, v in after.items() if before.get(p) != v]

    def find_paths_in_text(self, text: str) -> list[Path]:
        """tool 결과 텍스트에서 허용 폴더 안의 실제 파일 경로를 찾아냄."""
        found: list[Path] = []
        pattern = re.compile(r"(?:[A-Za-z]:[\\/]|/|\./|downloads/|files/|uploads/)[^\s\"'<>|*?\]\[,;)]+\.[A-Za-z0-9]{1,6}")
        for m in pattern.finditer(text):
            try:
                p = self.resolve(m.group(0).rstrip(".:"))
            except (FileAccessError, OSError, ValueError):
                continue
            if p.is_file() and p not in found:
                found.append(p)
        return found[:20]

    def save_blob(self, data: bytes, name_hint: str, mime: str | None, sub: str = "") -> Path:
        name = Path(name_hint.split("?")[0]).name or "download"
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        if not Path(name).suffix:
            name += mimetypes.guess_extension(mime or "") or ".bin"
        folder = self.downloads / sub if sub else self.downloads
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        if target.exists():
            target = folder / f"{target.stem}_{datetime.now():%H%M%S}{target.suffix}"
        target.write_bytes(data)
        return target

    def list_files(self, pattern: str = "*", recent_minutes: float | None = None, where: str = "all") -> list[Path]:
        roots = {"downloads": self.watch_dirs, "uploads": [self.uploads], "files": [self.files]}.get(
            where, [*self.watch_dirs, self.uploads, self.files]
        )
        cutoff = time.time() - recent_minutes * 60 if recent_minutes else None
        out: list[Path] = []
        for r in roots:
            if not r.is_dir():
                continue
            for p in r.rglob("*"):
                try:
                    if not p.is_file() or not fnmatch.fnmatch(p.name.lower(), pattern.lower()):
                        continue
                    if cutoff and p.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                if p not in out:
                    out.append(p)
        out.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return out[:300]


# ---------------------------------------------------------------------------
# 파일 형식 판별 / 상태 점검
# ---------------------------------------------------------------------------


def sniff_type(path: Path, head: bytes | None = None) -> str:
    """내용(매직 바이트) 기준 실제 파일 형식."""
    if head is None:
        with open(path, "rb") as f:
            head = f.read(_SNIFF)
    if not head:
        return "empty"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            return "zip(손상)"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"  # xls / doc / ppt / hwp(구)
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"HWP ":
        return "hwp"
    text = _decode(head)
    if text is None:
        return "binary"
    low = text.lstrip("\ufeff \r\n\t").lower()
    if low.startswith("<!doctype html") or low.startswith("<html") or "<html" in low[:500] or "<table" in low[:2000]:
        return "html"
    if low.startswith("<?xml"):
        return "xml"
    if low[:1] in ("{", "["):
        return "json"
    return "text"


def _decode(data: bytes) -> str | None:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            # 잘린 멀티바이트 문자 때문에 실패할 수 있으니 끝 몇 바이트를 버리고 재시도
            try:
                return data[:-3].decode(enc)
            except UnicodeDecodeError:
                continue
    return None


_EXPECT = {
    ".xlsx": {"xlsx"}, ".xlsm": {"xlsx"}, ".docx": {"docx"}, ".pptx": {"pptx"}, ".pdf": {"pdf"},
    ".xls": {"ole", "html", "xml", "text"}, ".doc": {"ole"}, ".ppt": {"ole"}, ".hwp": {"ole", "hwp"},
    ".zip": {"zip", "xlsx", "docx", "pptx"}, ".png": {"png"}, ".jpg": {"jpg"}, ".jpeg": {"jpg"}, ".gif": {"gif"},
    ".csv": {"text"}, ".tsv": {"text"}, ".txt": {"text", "json", "xml", "html"}, ".json": {"json", "text"},
    ".xml": {"xml", "text"}, ".html": {"html", "text", "xml"}, ".htm": {"html", "text"},
}

_ERROR_PAGE_HINTS = ("로그인", "login", "세션이 만료", "session expired", "권한이 없", "access denied", "error", "오류", "404", "500")


async def file_status(path: Path, ws: FileWorkspace | None = None, stable_wait: float = 1.0) -> dict[str, Any]:
    """파일 1개의 상태 점검.

    state: complete(정상) | downloading(받는 중) | empty | missing | corrupt(손상) | suspicious(내용이 확장자와 다름) | too_large
    """
    info: dict[str, Any] = {"path": str(path), "name": path.name}
    if ws:
        info["display"] = ws.display(path)
    issues: list[str] = []

    if not path.exists():
        # 받는 중인 임시 파일이 있는지
        temps = [path.with_name(path.name + s) for s in TEMP_SUFFIXES]
        temp = next((t for t in temps if t.exists()), None)
        if temp:
            info.update(state="downloading", temp_file=str(temp), size=temp.stat().st_size)
            info["issues"] = ["임시 파일이 존재 - 아직 다운로드 중"]
        else:
            info.update(state="missing", issues=["파일이 없습니다"])
        return info
    if path.is_dir():
        entries = list(path.iterdir())
        info.update(state="directory", entries=len(entries), sample=[e.name for e in entries[:20]])
        return info

    st = path.stat()
    info.update(size=st.st_size, size_h=_human(st.st_size), modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"))
    info["age_sec"] = round(time.time() - st.st_mtime, 1)
    ext = path.suffix.lower()
    info["extension"] = ext

    if ext in TEMP_SUFFIXES:
        info.update(state="downloading", issues=["다운로드 임시 파일입니다 (브라우저가 아직 받는 중이거나 실패 후 남은 파일)"])
        return info
    sibling = next((path.with_name(path.name + s) for s in TEMP_SUFFIXES if path.with_name(path.name + s).exists()), None)
    if sibling is not None:
        try:
            recent = time.time() - sibling.stat().st_mtime < 120
        except OSError:
            recent = False
        if recent:
            # 같은 파일을 다시 받는 중 → 지금 있는 파일은 이전 버전
            info.update(state="downloading", temp_file=str(sibling))
            info["issues"] = ["같은 이름으로 다시 다운로드 중 (현재 파일은 이전 버전일 수 있음)"]
            return info
        issues.append("같은 이름의 오래된 임시 다운로드 파일이 남아 있음 (이전 다운로드 실패 흔적)")

    # 크기 변화 확인 (아직 쓰는 중인지)
    if stable_wait and info["age_sec"] < 30:
        await asyncio.sleep(stable_wait)
        try:
            st2 = path.stat()
        except OSError:
            st2 = st
        if st2.st_size != st.st_size or st2.st_mtime != st.st_mtime:
            info.update(state="downloading", size=st2.st_size, size_h=_human(st2.st_size))
            info["issues"] = issues + [f"{stable_wait}초 사이에 크기가 {st.st_size}→{st2.st_size} 로 변함 - 쓰는 중"]
            return info

    if st.st_size == 0:
        info.update(state="empty", issues=issues + ["0 바이트 파일"])
        return info
    if ws and st.st_size > ws.max_file_mb * 1024 * 1024:
        info.update(state="too_large", issues=issues + [f"{ws.max_file_mb}MB 초과 - 분석 생략"])
        return info

    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF)
    except OSError as e:
        info.update(state="locked", issues=issues + [f"파일을 열 수 없음(다른 프로그램이 사용 중?): {e}"])
        return info

    detected = await asyncio.to_thread(sniff_type, path, head)
    info["detected_type"] = detected
    info["mime"] = mimetypes.guess_type(path.name)[0]
    state = "complete"

    expected = _EXPECT.get(ext)
    if detected.endswith("(손상)"):
        state = "corrupt"
        issues.append("ZIP 구조가 손상됨 - 다운로드가 중간에 끊겼을 가능성")
    elif expected and detected not in expected:
        state = "suspicious"
        if detected == "html":
            text = (_decode(head) or "").lower()
            hint = next((h for h in _ERROR_PAGE_HINTS if h in text), None)
            issues.append(
                f"확장자는 {ext} 인데 실제 내용은 HTML 입니다"
                + (f" ('{hint}' 문구 포함 - 로그인/오류 페이지가 저장된 것으로 보임)" if hint and ext not in (".xls",) else "")
            )
        else:
            issues.append(f"확장자({ext})와 실제 내용 형식({detected})이 다릅니다")

    # 형식별 무결성
    if state == "complete":
        integrity = await asyncio.to_thread(_integrity, path, detected, st.st_size)
        if integrity:
            state = "corrupt"
            issues.append(integrity)
    if detected == "html" and ext == ".xls":
        issues.append("HTML 표 형식의 xls (사내 시스템 엑셀 내보내기에 흔함) - analyze_file 로 표 분석 가능")

    info["state"] = state
    info["issues"] = issues
    if st.st_size <= 50 * 1024 * 1024:
        info["sha256"] = await asyncio.to_thread(_sha256, path)
    return info


def _integrity(path: Path, detected: str, size: int) -> str | None:
    try:
        if detected == "pdf":
            with open(path, "rb") as f:
                f.seek(max(0, size - 2048))
                if b"%%EOF" not in f.read():
                    return "PDF 끝(%%EOF)이 없음 - 다운로드가 잘렸을 가능성"
        elif detected in ("xlsx", "docx", "pptx", "zip") and size <= 100 * 1024 * 1024:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    return f"ZIP 내부 파일 손상: {bad}"
    except Exception as e:  # noqa: BLE001
        return f"무결성 검사 실패: {type(e).__name__}: {e}"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


async def wait_for_download(
    ws: FileWorkspace,
    pattern: str = "*",
    timeout: float = 300.0,
    newer_than: float | None = None,
    directory: Path | None = None,
    poll: float = 1.0,
) -> dict[str, Any]:
    """감시 폴더에 pattern 에 맞는 새 파일이 '완료' 상태가 될 때까지 대기."""
    start = time.time()
    since = newer_than if newer_than is not None else start - 600
    dirs = [directory] if directory else ws.watch_dirs
    last_seen: dict[str, Any] = {}
    while True:
        candidates: list[Path] = []
        in_progress: list[Path] = []
        for d in dirs:
            if not d.is_dir():
                continue
            for p in d.rglob("*"):
                try:
                    if not p.is_file() or p.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                if p.suffix.lower() in TEMP_SUFFIXES:
                    real = p.with_suffix("")
                    if fnmatch.fnmatch(real.name.lower(), pattern.lower()) or pattern == "*":
                        in_progress.append(p)
                elif fnmatch.fnmatch(p.name.lower(), pattern.lower()):
                    candidates.append(p)
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for c in candidates[:5]:
            st = await file_status(c, ws, stable_wait=min(poll, 1.0))
            if st["state"] != "downloading":
                st["waited_sec"] = round(time.time() - start, 1)
                return st
            last_seen = st
        if in_progress:
            last_seen = {"state": "downloading", "temp_file": str(in_progress[0]), "size": in_progress[0].stat().st_size}
        if time.time() - start >= timeout:
            return {
                "state": "timeout",
                "waited_sec": round(time.time() - start, 1),
                "pattern": pattern,
                "watch_dirs": [str(d) for d in dirs],
                "last_seen": last_seen or None,
                "issues": [f"{timeout}초 동안 '{pattern}' 에 맞는 완료된 파일이 나타나지 않았습니다"],
            }
        await asyncio.sleep(poll)


def format_status(st: dict[str, Any]) -> str:
    label = {
        "complete": "✅ 정상", "downloading": "⏳ 다운로드 중", "empty": "⚠️ 빈 파일", "missing": "❌ 없음",
        "corrupt": "❌ 손상", "suspicious": "⚠️ 의심", "too_large": "⚠️ 너무 큼", "locked": "⚠️ 잠김",
        "timeout": "❌ 시간 초과", "directory": "📁 폴더",
    }.get(st.get("state", ""), st.get("state", "?"))
    parts = [f"{label} {st.get('display') or st.get('path', '')}"]
    if "size_h" in st:
        parts.append(st["size_h"])
    if st.get("detected_type"):
        parts.append(f"형식={st['detected_type']}")
    line = " | ".join(parts)
    for i in st.get("issues") or []:
        line += f"\n    - {i}"
    return line


def decode_b64(data: str) -> bytes:
    return base64.b64decode(data + "=" * (-len(data) % 4))
