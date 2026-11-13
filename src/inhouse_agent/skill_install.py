"""스킬 가져오기: zip / GitHub(또는 사내 Git) 주소 / 서버 폴더 / 브라우저 폴더 업로드 / SKILL.md 한 개.

1) stage_*  : 원본을 임시 폴더(skills/.staging/<id>)에 받아서
2) scan     : 그 안의 모든 SKILL.md 를 찾음 (.agents/skills, .claude/skills, skills/ … 어디에 있든, 여러 개도)
3) install  : 고른 스킬만 skills/<이름>/ 에 설치 (출처를 .meta.json 에 기록)
4) update   : 기록한 출처(git/폴더)에서 다시 받아 갱신 - 스킬이 쓰던 data/ 폴더·DB 파일·편집 기록은 유지

Claude Code / Codex / Copilot / Gemini 용으로 만든 스킬(Agent Skills 형식)도 그대로 가져옵니다.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skills import NAME_RE, SkillError, SkillStore, parse_skill

KEEP_DOTFILES = {".python-version"}  # 숨김 파일이지만 스킬 실행에 필요 (스킬별 파이썬 버전)
SKIP_DIRS = {".git", "node_modules", "__pycache__", "_trash", ".history", ".venv", "venv", ".staging", ".mypy_cache", ".pytest_cache"}
KEEP_ON_UPDATE = ("data",)  # 스킬이 실행 중에 만든 상태(예: SQLite DB)는 업데이트해도 유지
KEEP_EXT = (".db", ".sqlite", ".sqlite3")
MAX_TOTAL = 200 * 1024 * 1024
MAX_FILES = 5000
STAGE_TTL = 2 * 3600


@dataclass
class FoundSkill:
    key: str  # 스테이지 안 상대 폴더 (고를 때 쓰는 값)
    name: str
    description: str = ""
    files: int = 0
    scripts: list[str] = field(default_factory=list)
    inputs: int = 0
    exists: bool = False
    error: str | None = None
    renamed_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def normalize_name(name: str, folder: str = "") -> str:
    """스킬 이름을 폴더 이름 규칙(영문/숫자/_/-)에 맞춤. 한글 이름이면 폴더 이름, 그것도 안 되면 skill-<해시>."""
    import hashlib

    if NAME_RE.match(name):
        return name
    for cand in (name, folder):
        n = re.sub(r"[^A-Za-z0-9_-]+", "-", cand).strip("-_").lower()[:64]
        if n and NAME_RE.match(n) and len(n) >= 2:
            return n
    return "skill-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:6]


def parse_git_url(url: str) -> dict[str, Any]:
    """GitHub/사내 Git 주소 해석: 저장소 · 브랜치(ref) · 하위 경로.
    https://github.com/o/r(.git) | …/tree/<ref>/<path> | …/blob/<ref>/<path>/SKILL.md | git@host:o/r.git"""
    url = url.strip()
    if url.startswith(("git@", "ssh://", "file://")):  # SSH, 공유 폴더의 저장소(file://) → git 으로만
        return {"clone": url, "host": None, "owner": None, "repo": None, "ref_path": []}
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SkillError(f"Git 저장소 주소가 아닙니다: {url}")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise SkillError(f"저장소 주소에 소유자/저장소 이름이 없습니다: {url}")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    rest = parts[2:]
    ref_path: list[str] = []
    if len(rest) >= 2 and rest[0] in ("tree", "blob"):
        ref_path = rest[1:]
        if ref_path and ref_path[-1] == "SKILL.md":
            ref_path = ref_path[:-1]
    base = f"{u.scheme}://{u.netloc}"
    return {"clone": f"{base}/{owner}/{repo}.git", "host": u.hostname, "base": base, "owner": owner, "repo": repo, "ref_path": ref_path}


class SkillInstaller:
    def __init__(self, store: SkillStore, cfg: dict[str, Any] | None = None, ssl_context: Any = None):
        self.store = store
        self.cfg = cfg or {}
        self.ssl_context = ssl_context
        self.stage_root = store.root / ".staging"

    # ------------------------------------------------------------------ 스테이지
    def _new_stage(self, source: dict[str, Any]) -> tuple[str, Path]:
        self._gc()
        sid = uuid.uuid4().hex[:12]
        d = self.stage_root / sid
        (d / "src").mkdir(parents=True)
        (d / "stage.json").write_text(json.dumps({"source": source, "created": time.time()}, ensure_ascii=False), encoding="utf-8")
        return sid, d / "src"

    def _gc(self) -> None:
        if not self.stage_root.is_dir():
            return
        for d in self.stage_root.iterdir():
            try:
                if time.time() - d.stat().st_mtime > STAGE_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    def stage_dir(self, sid: str) -> tuple[Path, dict[str, Any]]:
        if not re.fullmatch(r"[0-9a-f]{12}", sid or ""):
            raise SkillError("잘못된 가져오기 ID")
        d = self.stage_root / sid
        if not (d / "stage.json").is_file():
            raise SkillError("가져오기 정보가 없거나 만료되었습니다 (2시간). 다시 가져오세요.")
        return d / "src", json.loads((d / "stage.json").read_text(encoding="utf-8"))

    def drop(self, sid: str) -> None:
        try:
            src, _ = self.stage_dir(sid)
            shutil.rmtree(src.parent, ignore_errors=True)
        except SkillError:
            pass

    def stage_zip(self, data: bytes, filename: str = "") -> str:
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise SkillError("zip 파일이 아닙니다. (GitHub 에서는 Code → Download ZIP 으로 받은 파일을 올리세요)") from e
        sid, dest = self._new_stage({"type": "zip", "filename": filename})
        self._extract(z, dest)
        self._expand_skill_zips(dest)
        return sid

    def stage_files(self, files: list[dict[str, Any]], label: str = "") -> str:
        """브라우저 폴더 업로드: [{path: "skill_test/.agents/skills/x/SKILL.md", data: base64}]"""
        sid, dest = self._new_stage({"type": "upload", "label": label})
        total = 0
        for f in files[:MAX_FILES]:
            rel = str(f.get("path") or "").replace("\\", "/").lstrip("/")
            raw = base64.b64decode(f.get("data") or "")
            total += len(raw)
            if total > MAX_TOTAL:
                raise SkillError("올린 파일이 너무 큽니다 (200MB 초과).")
            p = self._safe(dest, rel)
            if p is None:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        self._expand_skill_zips(dest)
        return sid

    def stage_text(self, content: str, name: str | None = None) -> str:
        """SKILL.md 한 개(파일 또는 붙여넣은 내용)."""
        sid, dest = self._new_stage({"type": "text"})
        m = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", content)
        folder = normalize_name((name or (m.group(1) if m else "") or "new-skill").strip())
        (dest / folder).mkdir(parents=True)
        (dest / folder / "SKILL.md").write_text(content, encoding="utf-8")
        return sid

    def stage_folder(self, path: str) -> str:
        """스킬 서버 PC 의 폴더 (예: git 으로 받아 둔 사내 스킬 저장소). 갱신하면 다시 복사."""
        src = Path(path).expanduser()
        if not src.is_dir():
            raise SkillError(f"폴더가 없습니다: {path}")
        sid, dest = self._new_stage({"type": "folder", "path": str(src.resolve())})
        self._copy_tree(src, dest)
        return sid

    def stage_git(self, url: str, ref: str | None = None, subpath: str | None = None) -> str:
        info = parse_git_url(url)
        sid, dest = self._new_stage({"type": "git", "url": url.strip(), "ref": ref, "path": subpath})
        try:
            ref_used, sub, commit, how = self._fetch_git(info, ref, dest)
        except Exception:
            self.drop(sid)
            raise
        sub = subpath or sub
        meta_p = self.stage_root / sid / "stage.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["source"].update(ref=ref_used, path=sub, commit=commit, via=how)
        meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if sub:  # 하위 폴더만 보도록 (예: …/tree/main/.agents/skills/model-agents)
            keep = self._safe(dest, sub)
            if keep is None or not keep.is_dir():
                self.drop(sid)
                raise SkillError(f"저장소에 '{sub}' 폴더가 없습니다.")
            full = dest.parent / "full"
            dest.rename(full)
            shutil.move(str(full / keep.relative_to(dest)), str(dest))
            shutil.rmtree(full, ignore_errors=True)
        return sid

    # ------------------------------------------------------------------ git
    def _token(self, host: str | None) -> str | None:
        tok = self.cfg.get("git_token") or os.environ.get("AGENT_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        tokens = self.cfg.get("git_tokens") or {}
        if host and isinstance(tokens, dict) and tokens.get(host):
            tok = tokens[host]
        return str(tok) if tok else None

    def _fetch_git(self, info: dict[str, Any], ref: str | None, dest: Path) -> tuple[str | None, str | None, str | None, str]:
        """(사용한 ref, 하위 경로, 커밋, 방법). git 이 있으면 clone, 없으면 GitHub 압축 파일."""
        ref_path = list(info.get("ref_path") or [])
        cands: list[tuple[str | None, str | None]] = []
        if ref:
            cands.append((ref, "/".join(ref_path) or None))
        elif ref_path:  # tree/<ref>/<path> - ref 에 / 가 있을 수 있어 앞에서부터 늘려 봄
            for i in range(1, min(len(ref_path), 4) + 1):
                cands.append(("/".join(ref_path[:i]), "/".join(ref_path[i:]) or None))
        else:
            cands.append((None, None))
        errors = []
        git = shutil.which("git")
        if git:
            tok = self._token(info.get("host"))
            refs = self._remote_refs(git, info, tok)
            if refs:  # 실제 브랜치/태그 이름으로 ref 와 하위 경로를 정확히 나눔
                joined = "/".join(ref_path)
                if ref and ref not in refs and not re.fullmatch(r"[0-9a-f]{7,40}", ref):
                    raise SkillError(f"브랜치/태그 '{ref}' 가 없습니다. 있는 브랜치: {', '.join(sorted(refs)[:10])}")
                if not ref and ref_path:
                    match = max((r for r in refs if joined == r or joined.startswith(r + "/")), key=len, default=None)
                    if match is None:
                        raise SkillError(f"주소의 브랜치를 찾을 수 없습니다 ({joined}). 있는 브랜치: {', '.join(sorted(refs)[:10])}")
                    cands = [(match, joined[len(match) + 1:] or None)]
            tries = [(r, sub, t) for r, sub in cands for t in ([None, tok] if tok and info.get("host") else [None])]
            for r, sub, tok in tries:  # 토큰 없이 먼저 (공개 저장소), 실패하면 토큰으로
                shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir(parents=True)
                cmd = [git, "-c", "credential.helper="]
                if tok:
                    basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
                    cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
                cmd += ["clone", "--depth", "1", *(["--branch", r] if r else []), info["clone"], str(dest)]
                env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=int(self.cfg.get("git_timeout") or 300), env=env)
                except subprocess.TimeoutExpired:
                    errors.append("git clone 시간 초과")
                    continue
                if p.returncode == 0:
                    commit = subprocess.run([git, "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
                    shutil.rmtree(dest / ".git", ignore_errors=True)
                    return r, sub, commit, "git"
                errors.append((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else f"git 종료코드 {p.returncode}")
        if info.get("owner"):  # git 이 없거나 실패 → 압축 파일 (github.com / GitHub Enterprise)
            for r, sub in cands:
                try:
                    data = self._http_zip(info, r or "HEAD")
                except SkillError as e:
                    errors.append(str(e))
                    continue
                shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir(parents=True)
                self._extract(zipfile.ZipFile(io.BytesIO(data)), dest, strip_root=True)
                return r, sub, None, "zip"
        raise SkillError("저장소를 받지 못했습니다: " + " / ".join(dict.fromkeys(errors)) +
                         (" · 비공개 저장소면 skills.git_token(또는 GITHUB_TOKEN 환경변수)을 설정하세요." if errors else ""))

    def _remote_refs(self, git: str, info: dict[str, Any], tok: str | None) -> set[str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for t in [None, tok] if tok and info.get("host") else [None]:
            cmd = [git, "-c", "credential.helper="]
            if t:
                cmd += ["-c", "http.extraHeader=Authorization: Basic " + base64.b64encode(f"x-access-token:{t}".encode()).decode()]
            try:
                p = subprocess.run([*cmd, "ls-remote", "--heads", "--tags", info["clone"]], capture_output=True, text=True, timeout=60, env=env)
            except subprocess.TimeoutExpired:
                return set()
            if p.returncode == 0:
                return {ln.split("\t", 1)[1].removeprefix("refs/heads/").removeprefix("refs/tags/").removesuffix("^{}")
                        for ln in p.stdout.splitlines() if "\t" in ln}
        return set()

    def _http_zip(self, info: dict[str, Any], ref: str) -> bytes:
        host = info["host"]
        if host == "github.com":
            url = f"https://codeload.github.com/{info['owner']}/{info['repo']}/zip/{urllib.parse.quote(ref)}"
        else:  # GitHub Enterprise
            url = f"{info['base']}/{info['owner']}/{info['repo']}/archive/{urllib.parse.quote(ref)}.zip"
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ssl_context)) if self.ssl_context else urllib.request.build_opener()
        tok = self._token(host)
        last: Exception | None = None
        for t in [None, tok] if tok else [None]:
            hdrs = {"User-Agent": "inhouse-agent", **({"Authorization": f"token {t}"} if t else {})}
            try:
                with opener.open(urllib.request.Request(url, headers=hdrs), timeout=120) as r:
                    data = r.read(MAX_TOTAL + 1)
                break
            except urllib.error.HTTPError as e:
                last = SkillError(f"{url} → HTTP {e.code}" + (" (비공개 저장소면 토큰 필요)" if e.code in (401, 403, 404) else ""))
            except (urllib.error.URLError, OSError) as e:
                last = SkillError(f"{url} 연결 실패: {getattr(e, 'reason', e)} (사내 프록시/CA 설정 확인)")
        else:
            raise last or SkillError(f"{url} 받기 실패")
        if len(data) > MAX_TOTAL:
            raise SkillError("저장소 압축 파일이 너무 큽니다.")
        return data

    # ------------------------------------------------------------------ 파일 처리
    @staticmethod
    def _safe(root: Path, rel: str) -> Path | None:
        rel = rel.replace("\\", "/").lstrip("/")
        if not rel or any(x in ("..", "") for x in rel.split("/")):
            return None
        p = (root / rel).resolve()
        return p if root.resolve() in p.parents else None

    def _extract(self, z: zipfile.ZipFile, dest: Path, strip_root: bool = False) -> None:
        names = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
        if len(names) > MAX_FILES:
            raise SkillError(f"zip 안 파일이 너무 많습니다 ({len(names)}개).")
        if sum(i.file_size for i in names) > MAX_TOTAL:
            raise SkillError("zip 압축 해제 크기가 200MB 를 넘습니다.")
        prefix = ""
        if strip_root:
            tops = {i.filename.split("/", 1)[0] for i in names}
            prefix = (tops.pop() + "/") if len(tops) == 1 else ""
        for i in names:
            rel = i.filename[len(prefix):] if prefix and i.filename.startswith(prefix) else i.filename
            p = self._safe(dest, rel)
            if p is None:
                continue  # 경로 탈출 무시
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # 심볼릭 링크(.claude/skills/* → .agents/skills/*) 는 건너뜀 - 같은 스킬이 이미 있음
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(z.read(i))

    def _expand_skill_zips(self, dest: Path, depth: int = 3) -> list[str]:
        """올린 zip(또는 폴더) 안에 스킬 zip 이 들어 있으면 풀어 둠 (예: 스킬마다 zip 하나씩 묶어 올린 경우).
        - 풀기 대상: 안에 SKILL.md · .claude/commands/*.md · 또 다른 zip 이 있는 zip 만 → `<zip 이름>/` 폴더로 풀고 zip 은 지움
        - 스킬 폴더 안의 zip(템플릿·예시 등 스킬이 쓰는 파일)과 스킬과 무관한 zip 은 그대로 둠 → 스크립트가 그 zip 을 그대로 씀
        - 압축 폭탄 방지: 모두 합쳐 MAX_FILES·MAX_TOTAL, 깊이 3단계까지"""
        expanded: list[str] = []
        for _ in range(depth):
            skill_dirs = {Path(r) for r, _, fs in os.walk(dest) if "SKILL.md" in fs}
            zips = [
                p for p in sorted(dest.rglob("*.zip"))
                if p.is_file() and not p.is_symlink()
                and not (set(p.relative_to(dest).parts[:-1]) & SKIP_DIRS)
                and not any(d == p.parent or d in p.parents for d in skill_dirs)
            ]
            done = False
            for zp in zips:
                try:
                    z = zipfile.ZipFile(zp)
                    entries = [i.filename for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
                except (zipfile.BadZipFile, OSError):
                    continue
                if not any(
                    n.rsplit("/", 1)[-1] == "SKILL.md" or n.lower().endswith(".zip")
                    or ("/.claude/commands/" in "/" + n and n.endswith(".md"))
                    for n in entries
                ):
                    continue  # 스킬과 무관한 zip → 그대로 둠
                used = sum(1 for f in dest.rglob("*") if f.is_file())
                size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
                if used + len(entries) > MAX_FILES or size + sum(i.file_size for i in z.infolist()) > MAX_TOTAL:
                    raise SkillError(f"zip 안의 zip({zp.name})까지 풀면 너무 큽니다 (200MB 또는 5000개 초과).")
                out = zp.with_suffix("")
                if out.exists():
                    out = zp.with_name(zp.stem + "_zip")
                tops = {n.split("/", 1)[0] for n in entries}
                strip = len(tops) == 1 and all("/" in n for n in entries) and not next(iter(tops)).startswith(".")
                with z:
                    self._extract(z, out, strip_root=strip)
                zp.unlink()
                expanded.append(zp.relative_to(dest).as_posix())
                done = True
            if not done:
                break
        return expanded

    def _copy_tree(self, src: Path, dest: Path) -> None:
        total, count = 0, 0
        for root, dirs, files in os.walk(src, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not Path(root, d).is_symlink()]
            for fn in files:
                fp = Path(root, fn)
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
                count += 1
                if total > MAX_TOTAL or count > MAX_FILES:
                    raise SkillError("폴더가 너무 큽니다 (200MB 또는 5000개 초과).")
                out = dest / fp.relative_to(src)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, out)

    # ------------------------------------------------------------------ 찾기 / 설치
    def scan(self, sid: str) -> list[FoundSkill]:
        from .skill_scripts import skill_scripts

        src, _ = self.stage_dir(sid)
        self._convert_commands(src)
        dirs = []
        for root, sub, files in os.walk(src):
            sub[:] = sorted(d for d in sub if d not in SKIP_DIRS)
            if "SKILL.md" in files and (Path(root) / "SKILL.md").is_file():
                dirs.append(Path(root))
        # 다른 스킬 폴더 안에 있는 SKILL.md(예시 등)는 제외
        dirs = [d for d in dirs if not any(o != d and o in d.parents for o in dirs)]

        def prio(d: Path) -> tuple[int, int]:
            s = d.relative_to(src).as_posix()
            order = 0 if "/.agents/skills/" in "/" + s or s.startswith(".agents/") else 1 if "skills/" in s else 2
            return (order, s.count("/"))

        found: dict[str, FoundSkill] = {}
        for d in sorted(dirs, key=prio):
            key = d.relative_to(src).as_posix() or "."
            text = (d / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")
            m = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text)
            raw_name = m.group(1).strip() if m else (d.name if d != src else "imported-skill")
            new_name = normalize_name(raw_name, d.name)
            if new_name != raw_name:  # 이름 규칙에 맞게 바꾼 뒤 검사
                text = re.sub(r"(?m)^name:\s*.*$", f"name: {new_name}", text, count=1) if m else text
            try:
                sk = parse_skill(text, d if m else d.with_name(new_name))
                fs = FoundSkill(key, sk.name, sk.description, inputs=len(sk.inputs))
            except SkillError as e:
                fs = FoundSkill(key, new_name, error=str(e))
            fs.name = new_name
            if fs.name != raw_name:
                fs.renamed_from = raw_name
            if fs.name in found:
                continue  # .claude/skills 등 같은 스킬의 복사본
            fs.files = sum(1 for p in d.rglob("*") if p.is_file())
            fs.scripts = skill_scripts(d)
            fs.exists = (self.store.root / fs.name / "SKILL.md").is_file()
            found[fs.name] = fs
        return list(found.values())

    @staticmethod
    def _convert_commands(src: Path) -> None:
        """Claude Code 의 .claude/commands/**/*.md (슬래시 명령) → 스킬 폴더로 바꿔 둠 (_commands/<이름>/SKILL.md).
        머리말이 없거나 name/description 이 없어도 됨: 이름은 파일 이름, 설명은 본문 첫 문단."""
        out = src / "_commands"
        if out.exists():
            return
        for cdir in [d for d in src.rglob("commands") if d.is_dir() and d.parent.name == ".claude" and ".git" not in d.parts]:
            for f in sorted(cdir.rglob("*.md")):
                text = f.read_text(encoding="utf-8-sig", errors="replace")
                name = normalize_name("-".join(f.relative_to(cdir).with_suffix("").parts), f.stem)
                m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.S)
                head, body = (m.group(1), m.group(2)) if m else ("", text)
                if not re.search(r"(?m)^name:", head):
                    head = f"name: {name}\n" + head
                d = out / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(f"---\n{head.strip()}\n---\n{body}", encoding="utf-8")

    def sync(self, sources: list[dict[str, Any]], user: str | None = None) -> list[dict[str, Any]]:
        """설정한 Git 저장소(skills.sources)의 스킬·스크립트를 받아 설치·갱신 (러너의 '가져오기' 단계).
        - 새 스킬은 설치, 같은 저장소에서 온 스킬은 커밋이 바뀌었을 때만 갱신 (실행 중 만든 data/·DB 는 보존)
        - 이름이 같지만 다른 곳에서 온(직접 만든) 스킬은 덮어쓰지 않고 알려줌"""
        report = []
        for src in sources:
            if isinstance(src, str):
                src = {"url": src}
            url = str(src.get("url") or "").strip()
            row: dict[str, Any] = {"url": url, "installed": [], "updated": [], "unchanged": [], "skipped": [], "errors": {}}
            report.append(row)
            if not url:
                row["errors"]["(설정)"] = "url 이 없습니다"
                continue
            try:
                sid = self.stage_git(url, src.get("ref"), src.get("path")) if re.match(r"^(https?://|git@|ssh://|file://)", url) else self.stage_folder(url)
            except (SkillError, OSError) as e:
                row["errors"]["(가져오기)"] = str(e)
                continue
            try:
                _, meta = self.stage_dir(sid)
                commit = (meta.get("source") or {}).get("commit")
                row["commit"] = commit
                only = set(src.get("only") or [])
                todo = []
                for f in self.scan(sid):
                    if only and f.name not in only:
                        continue
                    if f.error:
                        row["errors"][f.name] = f.error
                    elif not f.exists:
                        todo.append((f, "installed"))
                    else:
                        old = (self.store.get(f.name).info.get("source") or {})
                        if old.get("url") != url:
                            row["skipped"].append(f"{f.name} (다른 곳에서 만든 같은 이름의 스킬이 있어 덮어쓰지 않음)")
                        elif commit and old.get("commit") == commit:
                            row["unchanged"].append(f.name)
                        else:
                            todo.append((f, "updated"))
                if todo:
                    res = self.install(sid, [f.key for f, _ in todo], overwrite=True, user=user)
                    done = {x["name"] for x in res["installed"]}
                    for f, kind in todo:
                        if f.name in done:
                            row[kind].append(f.name)
                    row["errors"].update(res["errors"])
            except (SkillError, OSError) as e:
                row["errors"]["(설치)"] = str(e)
            finally:
                self.drop(sid)
            for k in ("installed", "updated", "unchanged", "skipped"):
                row[k].sort()
        return report

    def install(self, sid: str, names: list[str] | None = None, overwrite: bool = False, user: str | None = None) -> dict[str, Any]:
        src, meta = self.stage_dir(sid)
        found = self.scan(sid)
        if not found:
            raise SkillError("가져온 내용에 SKILL.md 가 없습니다. (스킬 폴더마다 SKILL.md 가 있어야 합니다)")
        want = set(names) if names else None
        installed, skipped, errors = [], [], {}
        for fs in found:
            if want is not None and fs.name not in want and fs.key not in want:
                continue
            if fs.error:
                errors[fs.name] = fs.error
                continue
            if fs.exists and not overwrite:
                skipped.append(fs.name)
                continue
            try:
                sk = self._install_one(src / fs.key if fs.key != "." else src, fs, meta.get("source") or {}, user)
                installed.append(sk.summary())
            except (SkillError, OSError) as e:
                errors[fs.name] = str(e)
        return {"installed": installed, "skipped": skipped, "errors": errors, "source": meta.get("source")}

    def _install_one(self, folder: Path, fs: FoundSkill, source: dict[str, Any], user: str | None) -> Any:
        target = self.store.root / fs.name
        keep = target.parent / ".staging" / f"keep_{fs.name}_{uuid.uuid4().hex[:6]}"
        old_info: dict[str, Any] = {}
        if (target / "SKILL.md").is_file():  # 업데이트: 실행 중 만든 데이터·편집 기록은 보존
            old_info = self.store._info(target)
            keep.mkdir(parents=True)
            for item in [*KEEP_ON_UPDATE, ".history"]:
                if (target / item).exists():
                    shutil.move(str(target / item), str(keep / item))
            for f in target.rglob("*"):
                if f.is_file() and f.suffix.lower() in KEEP_EXT:
                    dst = keep / "_db" / f.relative_to(target)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dst))
            self.store.delete(fs.name)
        tmp = Path(tempfile.mkdtemp(dir=self.store.root / ".staging"))
        try:
            for p in folder.rglob("*"):
                rel = p.relative_to(folder)
                if not p.is_file() or any((x.startswith(".") and x not in KEEP_DOTFILES) or x in SKIP_DIRS for x in rel.parts):
                    continue
                out = tmp / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, out)
            text = (tmp / "SKILL.md").read_text(encoding="utf-8-sig")
            if fs.renamed_from:
                if re.search(r"(?m)^name:", text):
                    text = re.sub(r"(?m)^name:\s*.*$", f"name: {fs.name}", text, count=1)
                else:
                    text = re.sub(r"\A(\ufeff?---\s*\n)", f"\\1name: {fs.name}\n", text, count=1)
            (tmp / "SKILL.md").unlink()
            tmp.rename(target)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if keep.exists():
            for item in keep.iterdir():
                if item.name == "_db":
                    for f in item.rglob("*"):
                        if f.is_file() and not (target / f.relative_to(item)).exists():
                            (target / f.relative_to(item)).parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(f), str(target / f.relative_to(item)))
                elif not (target / item.name).exists():
                    shutil.move(str(item), str(target / item.name))
            shutil.rmtree(keep, ignore_errors=True)
        if old_info:  # 처음 만든 사람 정보 유지
            (target / ".meta.json").write_text(json.dumps({k: old_info[k] for k in ("created", "created_by") if k in old_info}, ensure_ascii=False), encoding="utf-8")
        sk = self.store.save(fs.name, text, user)
        src_info = {k: v for k, v in source.items() if v not in (None, "")}
        if source.get("type") in ("git", "folder"):
            src_info["skill_path"] = fs.key
        self.store.set_info(fs.name, source=src_info, installed=time.time(), installed_by=user)
        return self.store.get(fs.name)

    # ------------------------------------------------------------------ 갱신
    def update(self, name: str, user: str | None = None) -> dict[str, Any]:
        sk = self.store.get(name)
        src = sk.info.get("source") or {}
        typ = src.get("type")
        if typ == "git":
            sid = self.stage_git(src["url"], src.get("ref"), src.get("path"))
        elif typ == "folder":
            sid = self.stage_folder(src["path"])
        else:
            raise SkillError(f"스킬 '{name}' 은(는) {('zip/업로드' if typ else '직접 만든')} 스킬이라 원본에서 자동 갱신할 수 없습니다. 다시 가져오기를 하세요.")
        try:
            _, meta = self.stage_dir(sid)
            new_commit = (meta.get("source") or {}).get("commit")
            if new_commit and new_commit == src.get("commit"):
                return {"updated": False, "reason": "이미 최신입니다", "commit": new_commit}
            found = {f.key: f for f in self.scan(sid)}
            key = src.get("skill_path")
            pick = found.get(key) if key in found else next((f for f in found.values() if f.name == name), None)
            if pick is None:
                raise SkillError(f"원본에서 스킬 '{name}' 을(를) 찾지 못했습니다 (경로 {key}).")
            res = self.install(sid, [pick.key], overwrite=True, user=user)
            if res["errors"]:
                raise SkillError("; ".join(f"{k}: {v}" for k, v in res["errors"].items()))
            return {"updated": True, "commit": new_commit, "skill": res["installed"][0] if res["installed"] else None}
        finally:
            self.drop(sid)
