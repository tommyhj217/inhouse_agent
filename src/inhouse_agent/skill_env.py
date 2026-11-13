"""스킬별 파이썬 실행 환경.

스킬 폴더를 보고 어느 파이썬으로 스크립트를 돌릴지 정한다.
  1) 파이썬 스크립트가 없는 스킬(.md 만)          → 환경을 만들지 않음 (지금 서버 환경)
  2) 스크립트는 있지만 requirements.txt·.python-version 이 없음
       → skills.shared_requirements 가 있으면 공유 가상환경(_shared), 없으면 지금 서버 환경
  3) requirements.txt 또는 .python-version 이 있음  → 그 스킬 전용 가상환경
       - .python-version 의 버전(예: 3.11)으로 만들고, requirements.txt 에 적힌 것만 설치 (명시적 의존성)
       - 설치 뒤 검증: pip check(의존성 충돌), 적힌 패키지가 모두 깔렸고 버전 조건을 만족하는지
       - 지문(파이썬 버전 + requirements 내용 + 설치 설정)이 같으면 다시 만들지 않음, 바뀌면 새로 만듦

환경은 skills.env_dir(기본 skill_envs/) 아래 <스킬 이름>/<지문>/ 또는 _shared/<지문>/ 에 만들고,
설치·검증에 성공하면 current.json 이 그 폴더를 가리키게 한다(설치 로그는 <지문>/install.log).
실패하면 지금 쓰던 환경은 그대로 두고(새로 만든 것만 지움) failed.log 를 남기며, 원인과 조치를 알려준다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHARED = "_shared"
REQ_FILES = ("requirements.txt",)
VERSION_FILES = (".python-version",)

# 설치 결과를 검증하는 스크립트 (가상환경 안에서 실행, 표준 라이브러리만 사용)
_VERIFY = r'''
import json, re, sys
from importlib import metadata

def norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()

def ver(v):
    out = []
    for p in re.split(r"[.+-]", v):
        out.append(int(p) if p.isdigit() else 0)
    return tuple(out + [0] * (4 - len(out)))

def ok(have, op, want):
    h, w = ver(have), ver(want.rstrip(".*"))
    if op == "==":
        return have == want or (want.endswith(".*") and have.startswith(want[:-2])) or h == w
    if op == "!=":
        return h != w
    if op == ">=":
        return h >= w
    if op == "<=":
        return h <= w
    if op == ">":
        return h > w
    if op == "<":
        return h < w
    if op == "~=":
        parts = want.split(".")
        upper = ver(".".join(parts[:-1]))
        return h >= w and h[: len(parts) - 1] == upper[: len(parts) - 1]
    return True

installed = {norm(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}
reqs = json.loads(sys.argv[1])
res = {"python": "%d.%d.%d" % sys.version_info[:3], "packages": {}, "missing": [], "mismatch": []}
for r in reqs:
    name, spec = r["name"], r["spec"]
    have = installed.get(norm(name))
    if have is None:
        res["missing"].append(name)
        continue
    res["packages"][name] = have
    for op, want in re.findall(r"(==|!=|>=|<=|~=|>|<)\s*([^,\s;]+)", spec):
        if not ok(have, op, want):
            res["mismatch"].append(f"{name} {have} (요구 {op}{want})")
print(json.dumps(res, ensure_ascii=False))
'''


class SkillEnvError(RuntimeError):
    pass


@dataclass
class EnvPlan:
    kind: str  # base | shared | skill
    python: str  # 실행할 파이썬 (가상환경이면 그 안의 python)
    reason: str
    env_dir: Path | None = None
    version: str | None = None  # .python-version 에 적힌 버전
    requirements: list[str] | None = None


def parse_requirements(text: str) -> list[dict[str, str]]:
    """requirements.txt → [{name, spec, line}]. -r/-e/옵션 줄과 주석은 이름 확인에서 뺌 (설치는 pip 가 원문으로 함)."""
    out = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-", "--")):
            continue
        line = line.split(";", 1)[0].strip()  # 환경 표시(; python_version …)는 pip 가 판단
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$", line)
        if not m or "://" in line or line.startswith((".", "/")):
            continue  # URL·경로 설치는 이름 확인 생략
        out.append({"name": m.group(1), "spec": m.group(3).strip(), "line": raw.strip()})
    return out


def _has_python(d: Path) -> bool:
    for p in d.rglob("*.py"):
        rel = p.relative_to(d).parts
        if not any(x.startswith(".") or x in ("__pycache__", "tests", "evals") for x in rel):
            return True
    return False


def venv_python(env_dir: Path) -> Path:
    return env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class SkillEnvManager:
    def __init__(self, root: Path, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.root = Path(root)
        self.enabled = cfg.get("python_env", True) is not False
        self.shared_requirements = [str(x) for x in (cfg.get("shared_requirements") or [])]
        self.python_paths = {str(k): str(v) for k, v in (cfg.get("python_paths") or {}).items()}
        self.pip_index_url = cfg.get("pip_index_url")
        self.pip_extra_index_url = cfg.get("pip_extra_index_url")
        self.pip_trusted_host = cfg.get("pip_trusted_host")
        self.pip_args = [str(x) for x in (cfg.get("pip_args") or [])]
        self.install_timeout = float(cfg.get("env_install_timeout") or 900)
        self.base_python = sys.executable
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ 어떤 환경을 쓸지
    def plan(self, name: str, skill_dir: Path) -> EnvPlan:
        d = Path(skill_dir)
        req = next((d / f for f in REQ_FILES if (d / f).is_file()), None)
        ver = next((d / f for f in VERSION_FILES if (d / f).is_file()), None)
        if not self.enabled:
            return EnvPlan("base", self.base_python, "스킬별 환경이 꺼져 있음 (skills.python_env: false)")
        if req or ver:
            version = ver.read_text(encoding="utf-8").strip().splitlines()[0].strip() if ver else None
            reqs = [r["line"] for r in parse_requirements(req.read_text(encoding="utf-8-sig"))] if req else []
            env = self.root / name
            return EnvPlan("skill", self._current_python(env), "requirements.txt·.python-version 이 있어 이 스킬 전용 가상환경", env, version, reqs)
        if not _has_python(d):
            return EnvPlan("base", self.base_python, "파이썬 스크립트가 없는 스킬 (지금 서버 환경)")
        if self.shared_requirements:
            env = self.root / SHARED
            return EnvPlan("shared", self._current_python(env), "의존성 파일이 없어 공유 가상환경", env, None, list(self.shared_requirements))
        return EnvPlan("base", self.base_python, "의존성 파일이 없고 공유 가상환경이 설정되지 않아 지금 서버 환경")

    @staticmethod
    def current(env: Path) -> dict[str, Any] | None:
        p = env / "current.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        except ValueError:
            return None

    def _current_python(self, env: Path) -> str:
        cur = self.current(env)
        return str(venv_python(env / cur["fingerprint"])) if cur else ""

    # ------------------------------------------------------------------ 파이썬 찾기
    def find_python(self, version: str | None) -> str:
        """.python-version 의 버전에 맞는 파이썬. 설정(python_paths) → 지금 파이썬 → pythonX.Y → py -X.Y → uv 가 아는 파이썬."""
        if not version:
            return self.base_python
        want = version.strip()
        mm = ".".join(want.split(".")[:2])
        for key in (want, mm):
            if key in self.python_paths:
                p = self.python_paths[key]
                if Path(p).is_file() or shutil.which(p):
                    return p
                raise SkillEnvError(f"skills.python_paths 의 Python {key} 경로가 없습니다: {p}")
        cur = "%d.%d.%d" % sys.version_info[:3]
        if cur == want or cur.startswith(mm + ".") and want.count(".") < 2:
            return self.base_python
        cands = [f"python{mm}", f"python{mm}.exe"]
        for c in cands:
            p = shutil.which(c)
            if p and self._python_version(p).startswith(want if want.count(".") >= 2 else mm + "."):
                return p
        if os.name == "nt" and shutil.which("py"):
            try:
                out = subprocess.run(["py", f"-{mm}", "-c", "import sys;print(sys.executable)"], capture_output=True, text=True, timeout=30)
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
        uv = shutil.which("uv")
        if uv:
            try:
                out = subprocess.run([uv, "python", "find", want], capture_output=True, text=True, timeout=60)
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.strip().splitlines()[-1]
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise SkillEnvError(f"스킬이 Python {want} 을(를) 요구하지만 서버에서 찾지 못했습니다. "
                            f"Python {mm} 을 설치하거나 config.yaml 의 skills.python_paths 에 경로를 적으세요 (예: {{\"{mm}\": \"/usr/bin/python{mm}\"}}).")

    @staticmethod
    def _python_version(exe: str) -> str:
        try:
            out = subprocess.run([exe, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"], capture_output=True, text=True, timeout=30)
            return out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    # ------------------------------------------------------------------ 만들기·검증
    def _pip_opts(self) -> list[str]:
        o = []
        if self.pip_index_url:
            o += ["--index-url", str(self.pip_index_url)]
        if self.pip_extra_index_url:
            o += ["--extra-index-url", str(self.pip_extra_index_url)]
        if self.pip_trusted_host:
            o += ["--trusted-host", str(self.pip_trusted_host)]
        return o + self.pip_args

    def fingerprint(self, plan: EnvPlan, python: str) -> str:
        h = hashlib.sha256()
        for part in (plan.kind, plan.version or "", self._python_version(python), "\n".join(plan.requirements or []), " ".join(self._pip_opts())):
            h.update(part.encode("utf-8") + b"\0")
        return h.hexdigest()[:16]

    def status(self, name: str, skill_dir: Path) -> dict[str, Any]:
        plan = self.plan(name, skill_dir)
        info: dict[str, Any] = {"kind": plan.kind, "reason": plan.reason, "python": plan.python, "version": plan.version,
                                "requirements": plan.requirements or []}
        if plan.env_dir is not None:
            info["env_dir"] = str(plan.env_dir)
            info["state"] = self.current(plan.env_dir)
            fail = plan.env_dir / "failed.log"
            if fail.is_file() and not info["state"]:
                info["last_error"] = fail.read_text(encoding="utf-8").strip().splitlines()[-1][:400]
        return info

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def ensure(self, name: str, skill_dir: Path, rebuild: bool = False) -> EnvPlan:
        """실행할 파이썬을 돌려줌. 가상환경이 필요하면 만들거나(지문이 바뀌었으면 다시) 그대로 씀."""
        plan = self.plan(name, skill_dir)
        if plan.kind == "base" or plan.env_dir is None:
            return plan
        with self._lock(str(plan.env_dir)):
            base = self.find_python(plan.version)
            fp = self.fingerprint(plan, base)
            cur = self.current(plan.env_dir) or {}
            if not (not rebuild and cur.get("ok") and cur.get("fingerprint") == fp and venv_python(plan.env_dir / fp).is_file()):
                self._build(plan, base, fp, skill_dir)
            plan.python = str(venv_python(plan.env_dir / fp))
        return plan

    def _run(self, cmd: list[str], log: list[str], timeout: float) -> subprocess.CompletedProcess:
        env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONIOENCODING="utf-8")
        log.append("$ " + " ".join(cmd))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as e:
            raise SkillEnvError(f"시간 초과({timeout:.0f}초): {' '.join(cmd[:4])} … - skills.env_install_timeout 을 늘리거나 사내 PyPI 미러를 설정하세요.") from e
        log += [r.stdout[-6000:], r.stderr[-6000:]]
        return r

    def _build(self, plan: EnvPlan, base: str, fp: str, skill_dir: Path) -> None:
        env_dir = plan.env_dir
        assert env_dir is not None
        tmp = env_dir / fp  # 지문별 폴더에 바로 만듦 (가상환경은 옮기면 안의 경로가 깨짐)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        log: list[str] = [f"# {time.strftime('%Y-%m-%d %H:%M:%S')} {plan.kind} 환경 만들기 (python {base})"]
        t0 = time.monotonic()
        try:
            r = self._run([base, "-m", "venv", *(["--system-site-packages"] if plan.kind == "shared" else []), str(tmp)], log, 300)
            if r.returncode != 0:
                hint = (f" - 이 파이썬에 venv 모듈이 없습니다. 리눅스면 'apt install python{'.'.join(self._python_version(base).split('.')[:2])}-venv' 로 설치하세요."
                        if "ensurepip" in (r.stdout + r.stderr) else "")
                raise SkillEnvError("가상환경을 만들지 못했습니다: " + _tail(r.stderr or r.stdout) + hint)
            py = str(venv_python(tmp))
            reqs = parse_requirements("\n".join(plan.requirements or []))
            if plan.requirements:
                if plan.kind == "skill":  # 원문 그대로 설치 (-r 로 같은 폴더의 다른 파일을 참조할 수 있게)
                    req_file = Path(skill_dir) / REQ_FILES[0]
                    target = ["-r", str(req_file)] if req_file.is_file() else list(plan.requirements)
                else:
                    target = list(plan.requirements)
                r = self._run([py, "-m", "pip", "install", "--no-input", *self._pip_opts(), *target], log, self.install_timeout)
                if r.returncode != 0:
                    raise SkillEnvError("의존성 설치에 실패했습니다: " + _tail(r.stderr or r.stdout)
                                        + " - 패키지 이름·버전과 사내 PyPI 설정(skills.pip_index_url)을 확인하세요.")
            # 검증 1: pip check (설치된 것끼리 버전 충돌)
            chk = self._run([py, "-m", "pip", "check"], log, 120)
            conflicts = [x for x in (chk.stdout or "").splitlines() if x.strip() and "No broken requirements" not in x] if chk.returncode != 0 else []
            # 검증 2: 적힌 패키지가 모두 깔렸고 버전 조건을 만족하는지 + 실제 파이썬 버전
            ver = self._run([py, "-c", _VERIFY, json.dumps(reqs)], log, 120)
            try:
                v = json.loads((ver.stdout or "").strip().splitlines()[-1])
            except (ValueError, IndexError) as e:
                raise SkillEnvError("설치 결과를 확인하지 못했습니다: " + _tail(ver.stderr or ver.stdout)) from e
            problems = [f"설치되지 않음: {', '.join(v['missing'])}"] if v["missing"] else []
            problems += [f"버전 조건 불일치: {', '.join(v['mismatch'])}"] if v["mismatch"] else []
            problems += [f"의존성 충돌: {'; '.join(conflicts[:5])}"] if conflicts else []
            if plan.version and not v["python"].startswith(plan.version if plan.version.count(".") >= 2 else plan.version + "."):
                problems.append(f"파이썬 버전이 다릅니다: 요구 {plan.version}, 실제 {v['python']}")
            if problems:
                raise SkillEnvError("설치 검증 실패 - " + " / ".join(problems))
            meta = {"ok": True, "kind": plan.kind, "fingerprint": fp, "python": v["python"], "base_python": base,
                    "requested_version": plan.version, "requirements": plan.requirements or [], "packages": v["packages"],
                    "built_at": time.time(), "seconds": round(time.monotonic() - t0, 1)}
            (tmp / "install.log").write_text("\n".join(x for x in log if x), encoding="utf-8")
            prev = self.current(env_dir) or {}
            cur_tmp = env_dir / "current.json.tmp"
            cur_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            cur_tmp.replace(env_dir / "current.json")  # 성공한 뒤에만 새 환경으로 바꿈
            (env_dir / "failed.log").unlink(missing_ok=True)
            if prev.get("fingerprint") and prev["fingerprint"] != fp:
                shutil.rmtree(env_dir / prev["fingerprint"], ignore_errors=True)  # 이전 환경 정리
        except SkillEnvError as e:
            fail = env_dir / "failed.log"
            fail.write_text("\n".join(x for x in log if x) + f"\n# 실패: {e}", encoding="utf-8")
            shutil.rmtree(tmp, ignore_errors=True)
            raise SkillEnvError(f"{e} (설치 로그: {fail})") from e
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise


def _tail(s: str, n: int = 400) -> str:
    lines = [x for x in (s or "").strip().splitlines() if x.strip()]
    err = [x for x in lines if re.search(r"error|오류|not find|no matching|could not", x, re.I)]
    return (" | ".join((err or lines)[-3:]))[:n] or "(출력 없음)"


def format_status(name: str, st: dict[str, Any]) -> str:
    kind = {"base": "지금 서버 환경", "shared": "공유 가상환경", "skill": "스킬 전용 가상환경"}[st["kind"]]
    lines = [f"## '{name}' 파이썬 환경: {kind}", f"- 이유: {st['reason']}"]
    if st.get("version"):
        lines.append(f"- 요구 파이썬: {st['version']}")
    if st.get("requirements"):
        lines.append(f"- 의존성 {len(st['requirements'])}개: {', '.join(st['requirements'][:10])}")
    s = st.get("state")
    if st["kind"] != "base":
        if not s:
            lines.append("- 상태: 아직 만들지 않음 (처음 실행할 때 또는 skill_env build 로 만듦)")
        else:
            lines.append(f"- 상태: 준비됨 (Python {s['python']}, {time.strftime('%m-%d %H:%M', time.localtime(s['built_at']))}, {s['seconds']}초)")
            if s.get("packages"):
                lines.append("- 설치·검증된 패키지: " + ", ".join(f"{k} {v}" for k, v in list(s["packages"].items())[:15]))
    return "\n".join(lines)
