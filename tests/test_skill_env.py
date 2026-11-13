"""스킬별 파이썬 환경: .md 만 → 지금 환경, 의존성 파일 없음 → 공유 가상환경, requirements.txt·.python-version → 전용 가상환경(설치·검증).
네트워크 없이 돌도록 직접 만든 휠(wheel)과 pip --no-index --find-links 를 씀."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.skill_env import _VERIFY, SkillEnvError, SkillEnvManager, parse_requirements

PY311 = shutil.which("python3.11")


def make_wheel(folder: Path, name: str = "tinypkg", version: str = "1.2.0") -> Path:
    """설치 가능한 최소 휠 (모듈 하나)."""
    folder.mkdir(parents=True, exist_ok=True)
    whl = folder / f"{name}-{version}-py3-none-any.whl"
    files = {f"{name}/__init__.py": f"VERSION = '{version}'\n",
             f"{name}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
             f"{name}-{version}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = []
    with zipfile.ZipFile(whl, "w") as z:
        for path, text in files.items():
            data = text.encode()
            z.writestr(path, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{path},sha256={digest},{len(data)}")
        record.append(f"{name}-{version}.dist-info/RECORD,,")
        z.writestr(f"{name}-{version}.dist-info/RECORD", "\n".join(record) + "\n")
    return whl


SCRIPT = "import sys, json\ntry:\n    import tinypkg\n    v = tinypkg.VERSION\nexcept ImportError:\n    v = None\nprint(json.dumps({'python': '%d.%d' % sys.version_info[:2], 'tinypkg': v, 'exe': sys.executable}))\n"


def skill(root: Path, name: str, files: dict[str, str]) -> Path:
    d = root / name
    for rel, text in {"SKILL.md": f"---\nname: {name}\ndescription: 환경 테스트 {name}\n---\n본문\n", **files}.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text, encoding="utf-8")
    return d


def cfg_with(tmp_path: Path, wheels: Path, **skills_cfg) -> object:
    cfg = load_config(write_config(tmp_path))
    cfg.raw.setdefault("skills", {}).update({"pip_args": ["--no-index", "--find-links", str(wheels)], **skills_cfg})
    return cfg


def test_plan_by_folder_contents(tmp_path: Path) -> None:
    m = SkillEnvManager(tmp_path / "envs")
    md = skill(tmp_path, "md-only", {"reference/a.md": "x"})
    assert m.plan("md-only", md).kind == "base" and "파이썬 스크립트가 없는" in m.plan("md-only", md).reason
    py = skill(tmp_path, "py-only", {"scripts/x.py": "print(1)"})
    assert m.plan("py-only", py).kind == "base"  # 공유 환경 설정이 없으면 지금 환경
    assert SkillEnvManager(tmp_path / "envs", {"shared_requirements": ["tinypkg"]}).plan("py-only", py).kind == "shared"
    req = skill(tmp_path, "with-req", {"scripts/x.py": "print(1)", "requirements.txt": "tinypkg>=1.0  # 주석\n-r other.txt\n"})
    p = m.plan("with-req", req)
    assert p.kind == "skill" and p.requirements == ["tinypkg>=1.0  # 주석"] and p.version is None
    ver = skill(tmp_path, "with-ver", {".python-version": "3.11\n"})
    assert m.plan("with-ver", ver).kind == "skill" and m.plan("with-ver", ver).version == "3.11"  # .md 만 있어도 버전을 적었으면 전용
    assert SkillEnvManager(tmp_path / "e", {"python_env": False}).plan("with-req", req).kind == "base"
    assert [r["name"] for r in parse_requirements("a==1\nb[x]>=2; python_version>'3'\nhttps://x/y.whl\n./local\n--index-url z\n")] == ["a", "b"]


@pytest.mark.skipif(PY311 is None, reason="python3.11 없음")
async def test_dedicated_env_built_verified_and_used(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    make_wheel(wheels, version="1.2.0")
    make_wheel(wheels, version="2.0.0")
    cfg = cfg_with(tmp_path, wheels)
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        root = Path(rt.skills.root)
        skill(root, "bom-311", {".python-version": "3.11\n", "requirements.txt": "tinypkg==1.2.0\n", "scripts/run.py": SCRIPT})
        skill(root, "docs-only", {"guide.md": "x"})
        skill(root, "plain-py", {"scripts/run.py": SCRIPT})
        call = rt.hub.call
        # 1) 전용 가상환경: 처음 실행할 때 3.11 로 만들고 요구한 버전만 설치·검증
        r = await call("run_skill_script", {"name": "bom-311", "command": "python scripts/run.py"})
        assert not r.is_error, r.text
        assert r.data["python"] == "3.11" and r.data["tinypkg"] == "1.2.0" and "skill_envs" in r.data["exe"] and "[전용 가상환경]" in r.text
        st = rt.skill_envs.status("bom-311", root / "bom-311")["state"]
        assert st["ok"] and st["python"].startswith("3.11.") and st["packages"] == {"tinypkg": "1.2.0"} and st["requested_version"] == "3.11"
        built = st["built_at"]
        # 2) 다시 실행해도 다시 만들지 않음
        await call("run_skill_script", {"name": "bom-311", "command": "python scripts/run.py"})
        assert rt.skill_envs.status("bom-311", root / "bom-311")["state"]["built_at"] == built
        # 3) .md 만 있는 스킬·의존성 없는 스크립트는 지금 서버 환경
        r = await call("run_skill_script", {"name": "plain-py", "command": "python scripts/run.py"})
        assert r.data["exe"] == sys.executable and "가상환경" not in r.text
        st_doc = await call("skill_env", {"name": "docs-only"})
        assert "지금 서버 환경" in st_doc.text and "파이썬 스크립트가 없는" in st_doc.text
        # 4) requirements 가 바뀌면 새로 만들고 이전 환경은 정리
        old_fp = st["fingerprint"]
        (root / "bom-311" / "requirements.txt").write_text("tinypkg==2.0.0\n", encoding="utf-8")
        r = await call("run_skill_script", {"name": "bom-311", "command": "python scripts/run.py"})
        assert r.data["tinypkg"] == "2.0.0"
        env_dir = Path(rt.skill_envs.root) / "bom-311"
        assert not (env_dir / old_fp).exists() and (env_dir / rt.skill_envs.current(env_dir)["fingerprint"]).is_dir()
        # 5) 설치 실패: 원인·로그를 알리고 지금 쓰던 환경은 그대로
        (root / "bom-311" / "requirements.txt").write_text("tinypkg==2.0.0\nnot-a-real-pkg==9.9\n", encoding="utf-8")
        r = await call("run_skill_script", {"name": "bom-311", "command": "python scripts/run.py"})
        assert r.is_error and "의존성 설치에 실패" in r.text and "not-a-real-pkg" in r.text and "failed.log" in r.text
        assert rt.skill_envs.current(env_dir)["packages"] == {"tinypkg": "2.0.0"}  # 이전 환경 유지
        st_txt = (await call("skill_env", {"name": "bom-311"})).text
        assert "스킬 전용 가상환경" in st_txt and "tinypkg 2.0.0" in st_txt


async def test_shared_env_and_missing_python(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    make_wheel(wheels)
    cfg = cfg_with(tmp_path, wheels, shared_requirements=["tinypkg==1.2.0"])
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        root = Path(rt.skills.root)
        skill(root, "a", {"scripts/run.py": SCRIPT})
        skill(root, "b", {"run.py": SCRIPT})
        ra = await rt.hub.call("run_skill_script", {"name": "a", "command": "python scripts/run.py"})
        rb = await rt.hub.call("run_skill_script", {"name": "b", "command": "python run.py"})
        assert ra.data["tinypkg"] == rb.data["tinypkg"] == "1.2.0" and ra.data["exe"] == rb.data["exe"]  # 두 스킬이 같은 공유 환경
        assert "_shared" in ra.data["exe"] and "[공유 가상환경]" in ra.text
        skill(root, "old", {".python-version": "3.99", "scripts/run.py": SCRIPT})
        r = await rt.hub.call("run_skill_script", {"name": "old", "command": "python scripts/run.py"})
        assert r.is_error and "Python 3.99 을(를) 요구하지만 서버에서 찾지 못했습니다" in r.text and "skills.python_paths" in r.text


def test_verification_catches_version_mismatch(tmp_path: Path) -> None:
    """설치 검증 스크립트: 깔린 버전이 조건에 안 맞으면 잡음 (pip 이 다른 경로로 깔았거나 수동으로 바꾼 경우)."""
    wheels = tmp_path / "wheels"
    make_wheel(wheels, version="1.2.0")
    env = tmp_path / "v"
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    py = env / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--no-index", "--find-links", str(wheels), "tinypkg"], check=True)
    reqs = parse_requirements("tinypkg>=2,<3\nmissingpkg\ntinypkg~=1.1\n")
    out = json.loads(subprocess.run([str(py), "-c", _VERIFY, json.dumps(reqs)], capture_output=True, text=True, check=True).stdout)
    assert out["missing"] == ["missingpkg"] and out["mismatch"] == ["tinypkg 1.2.0 (요구 >=2)"]
    with pytest.raises(SkillEnvError, match="python_paths 의 Python 3.11 경로가 없습니다"):
        SkillEnvManager(tmp_path, {"python_paths": {"3.11": "/no/such/python"}}).find_python("3.11")


@pytest.mark.skipif(PY311 is None, reason="python3.11 없음")
async def test_sync_prepares_env_and_test_runner_reports_env_failure(tmp_path: Path) -> None:
    """Git 에서 받을 때 전용 환경을 바로 만들고 검증, 테스트 러너는 환경 실패를 먼저 알려줌."""
    wheels = tmp_path / "wheels"
    make_wheel(wheels)
    repo = tmp_path / "repo"
    skill(repo / ".claude" / "skills", "calc", {".python-version": "3.11", "requirements.txt": "tinypkg==1.2.0\n", "scripts/run.py": SCRIPT,
                                                "tests/cases.yaml": "cases:\n  - name: 3.11 과 tinypkg\n    command: python scripts/run.py\n"
                                                                    "    expect: {json: {python: '3.11', tinypkg: 1.2.0}}\n"})
    skill(repo / ".claude" / "skills", "notes", {"guide.md": "문서만"})
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("add", "-A")
    run("commit", "-qm", "v1")
    cfg = cfg_with(tmp_path, wheels)
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        rep = rt.sync_skills([{"url": f"file://{repo.as_posix()}"}])[0]
        assert rep["env"]["calc"].startswith("전용 가상환경 준비됨 (Python 3.11.") and rep["env"]["notes"] == "지금 서버 환경"
        out = await rt.hub.call("test_skill", {"name": "calc"})
        assert not out.is_error and "통과 1" in out.text and "전용 가상환경" in out.text, out.text
        (Path(rt.skills.root) / "calc" / "requirements.txt").write_text("tinypkg==7.0\n", encoding="utf-8")
        out = await rt.hub.call("test_skill", {"name": "calc"})
        assert out.is_error and "(파이썬 환경 준비)" in out.text and "의존성 설치에 실패" in out.text
