"""스킬 가져오기(zip/Git/폴더/SKILL.md), 다른 에이전트용 스킬 호환, 명령줄 실행, 화면 문서(view)."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest
from test_multiuser_skills import _cfg

from inhouse_agent.config import load_config
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.skill_install import SkillInstaller, parse_git_url
from inhouse_agent.skills import SkillError, SkillStore, parse_skill
from inhouse_agent.users import current_user

MENU_MD = """---
name: model-menu
description: Shows my models: which agents each model can use. Use when asked "내 모델".
allowed-tools: Bash(python3 .agents/skills/model-menu/scripts/view.py:*)
metadata:
  ui: dynamic-view
---
# Model menu
| Step | Command |
|---|---|
| 1 | `python3 <this skill>/scripts/view.py home` |
| 2 | `python3 <this skill>/scripts/view.py model <KEY>` |
Run views with `--format json` for your own host.
"""

VIEW_PY = r'''import json, os, sys
user = os.environ.get("MODEL_AGENTS_USER", "?")
a = sys.argv[1:]
fmt = a[a.index("--format") + 1] if "--format" in a else "md"
here = os.path.abspath(__file__).replace("\\", "/")
sets = dict(x.split("=", 1) for i, x in enumerate(a) if i and a[i - 1] == "--set")
if a[:1] == ["home"]:
    v = {"view": "home", "title": user + " 님 모델", "sections": [{"title": "모델", "items": [
        {"id": "M1", "title": "M1 모델", "badges": [{"text": "에이전트 1/2", "tone": "ok"}], "action": {"command": "python3 " + here + " model M1"}}]}],
        "actions": [{"id": "add", "label": "모델 등록", "action": {"command": "python3 " + here + " add <CODE>"}}], "prompt": "어떤 모델?"}
else:
    v = {"view": "agent", "title": a[1], "form": {"fields": [{"name": "level", "type": "select", "options": ["smoke", "full"]}],
         "values": {"level": sets.get("level", "smoke")}, "submit": {"command": "python3 " + here + " model " + a[1]}},
         "invoke": {"skill": "agent-b", "prompt": "/agent-b " + a[1] + " " + sets.get("level", "smoke")}}
print(json.dumps(v, ensure_ascii=False) if fmt == "json" else "# " + v["title"])
'''

AGENT_B_MD = """---
name: agent-b
description: Create a QA test plan for a product model: scope, test cases by area.
---
Run `python3 <skills dir>/model-menu/scripts/view.py model <MODEL>` then make the plan.
"""
UI_JSON = {"inputs": [{"name": "level", "label": "테스트 수준", "type": "select", "options": ["smoke", "full"], "default": "smoke"},
                      {"name": "regions", "type": "text", "default": "{regions}"}, {"name": "notes", "type": "textarea", "default": ""}]}


def _repo(root: Path) -> Path:
    """skill_test 저장소와 같은 모양: .agents/skills/<여러 스킬>, .claude/skills/* 는 링크 대용 텍스트 파일."""
    base = root / ".agents" / "skills"
    (base / "model-menu" / "scripts").mkdir(parents=True)
    (base / "model-menu" / "SKILL.md").write_text(MENU_MD, encoding="utf-8")
    (base / "model-menu" / "scripts" / "view.py").write_text(VIEW_PY, encoding="utf-8")
    (base / "model-menu" / "examples").mkdir()
    (base / "model-menu" / "examples" / "SKILL.md").write_text("---\nname: nested\ndescription: x\n---\n", encoding="utf-8")
    (base / "agent-b").mkdir()
    (base / "agent-b" / "SKILL.md").write_text(AGENT_B_MD, encoding="utf-8")
    (base / "agent-b" / "ui.json").write_text(json.dumps(UI_JSON, ensure_ascii=False), encoding="utf-8")
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".claude" / "skills" / "model-menu").write_text("../../.agents/skills/model-menu", encoding="utf-8")  # git symlink stub
    (root / "README.md").write_text("# test", encoding="utf-8")
    return root


def _zip(root: Path, top: str = "skill_test-main") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in root.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                z.write(p, f"{top}/{p.relative_to(root).as_posix()}")
        z.writestr(f"{top}/../../evil.py", "x")
    return buf.getvalue()


def test_other_runner_skill_format(tmp_path: Path) -> None:
    r = _repo(tmp_path / "repo")
    b = parse_skill((r / ".agents/skills/agent-b/SKILL.md").read_text(encoding="utf-8"), r / ".agents/skills/agent-b")
    assert b.description.startswith("Create a QA test plan for a product model: scope")  # 값 안 ': ' 도 읽음
    assert [(i.name, i.type, i.default) for i in b.inputs] == [("level", "select", "smoke"), ("regions", "string", None), ("notes", "text", "")]
    m = parse_skill(MENU_MD, r / ".agents/skills/model-menu")
    here = (r / ".agents/skills/model-menu").resolve().as_posix()
    assert f"`python3 {here}/scripts/view.py home`" in m.render({}, "Asia/Seoul")
    assert m.entry_commands() == [f"python3 {here}/scripts/view.py home"]  # <KEY> 자리표시가 있는 명령은 제외
    with pytest.raises(SkillError):
        parse_skill("---\nname: x\ndescription: [broken\n---\n", tmp_path)


def test_zip_multi_skill_install_and_overwrite_keeps_data(tmp_path: Path) -> None:
    st = SkillStore(tmp_path / "skills")
    inst = SkillInstaller(st)
    data = _zip(_repo(tmp_path / "repo"))
    sid = inst.stage_zip(data, "skill_test-main.zip")
    found = {f.name: f for f in inst.scan(sid)}
    assert set(found) == {"model-menu", "agent-b"}  # 중첩 examples/SKILL.md, .claude 링크 파일은 제외
    assert found["model-menu"].scripts == ["scripts/view.py"] and found["agent-b"].inputs == 3
    res = inst.install(sid, ["agent-b"], user="kim")
    assert [x["name"] for x in res["installed"]] == ["agent-b"] and not (tmp_path / "evil.py").exists()
    assert st.get("agent-b").info["source"] == {"type": "zip", "filename": "skill_test-main.zip"}
    res = inst.install(sid, None, user="kim")
    assert [x["name"] for x in res["installed"]] == ["model-menu"] and res["skipped"] == ["agent-b"]
    # 덮어쓰기: 스킬이 만든 data/ 와 DB 는 유지, 만든 사람 유지
    d = st.root / "model-menu"
    (d / "data").mkdir()
    (d / "data" / "models.db").write_text("db", encoding="utf-8")
    (d / "cache.sqlite").write_text("c", encoding="utf-8")
    st.import_zip(data, user="lee", overwrite=True)
    assert (d / "data" / "models.db").read_text(encoding="utf-8") == "db" and (d / "cache.sqlite").is_file()
    info = st.get("model-menu").info
    assert info["created_by"] == "kim" and info["updated_by"] == "lee"
    with pytest.raises(SkillError, match="이미 있습니다"):
        st.import_zip(data, user="lee")
    # SKILL.md 하나만
    sid = inst.stage_text("---\nname: 메모 정리\ndescription: 메모를 정리\n---\n본문")
    f = inst.scan(sid)[0]
    assert f.renamed_from == "메모 정리" and f.name != "메모 정리"
    assert inst.install(sid)["installed"][0]["name"] == f.name


@pytest.mark.skipif(shutil.which("git") is None, reason="git 없음")
def test_git_and_folder_install_then_update(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "dev/v1")
    run("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    st = SkillStore(tmp_path / "skills")
    inst = SkillInstaller(st)
    sid = inst.stage_git(f"file://{repo.as_posix()}", "dev/v1", ".agents/skills")
    assert {f.name for f in inst.scan(sid)} == {"model-menu", "agent-b"}
    inst.install(sid, user="kim")
    src = st.get("model-menu").info["source"]
    assert src["type"] == "git" and src["ref"] == "dev/v1" and src["path"] == ".agents/skills" and len(src["commit"]) == 40
    assert inst.update("model-menu")["updated"] is False  # 원본이 그대로면 최신
    (repo / ".agents/skills/model-menu/SKILL.md").write_text(MENU_MD.replace("Shows my models", "Shows MY models v2"), encoding="utf-8")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2")
    assert inst.update("model-menu")["updated"] is True and "v2" in st.get("model-menu").description
    with pytest.raises(SkillError, match="있는 브랜치"):
        inst.stage_git(f"file://{repo.as_posix()}", "main")
    # 서버 폴더에서 가져오기 → 폴더 원본으로 갱신
    sid = inst.stage_folder(str(repo / ".agents/skills"))
    inst.install(sid, ["agent-b"], overwrite=True)
    (repo / ".agents/skills/agent-b/extra.md").write_text("new", encoding="utf-8")
    assert inst.update("agent-b")["updated"] is True and (st.root / "agent-b" / "extra.md").is_file()
    # 주소 해석
    g = parse_git_url("https://github.com/tommyhj217/skill_test/tree/ccr-9b4d8682-bbdfwr/.agents/skills/model-agents")
    assert g["clone"] == "https://github.com/tommyhj217/skill_test.git" and g["ref_path"][0] == "ccr-9b4d8682-bbdfwr"
    assert parse_git_url("https://git.company.com/team/skills.git")["repo"] == "skills"


async def test_command_lines_env_and_view_documents(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path))
    async with AgentRuntime(cfg) as rt:
        inst = rt.skill_installer
        sid = inst.stage_zip(_zip(_repo(tmp_path / "repo")))
        inst.install(sid)
        tok = current_user.set("jiwoo")
        try:
            # Claude Code 용 스킬 문서에 적힌 명령줄 그대로 (다른 PC 경로여도 스킬 이름으로 찾음)
            for cmd in ("python3 .agents/skills/model-menu/scripts/view.py home --format json",
                        'python "C:/work/skill_test/.agents/skills/model-menu/scripts/view.py" home --format json'):
                out = await rt.hub.call("run_skill_script", {"command": cmd})
                assert not out.is_error, out.text
                assert out.data["view"] == "home" and out.data["title"] == "jiwoo 님 모델"  # MODEL_AGENTS_USER = 현재 사용자
                assert "[화면]" in out.text and "1. M1 모델" in out.text and "--format json" not in out.text.split("[화면]")[1][:10]
            nxt = out.data["sections"][0]["items"][0]["action"]["command"]
            out = await rt.hub.call("run_skill_script", {"command": nxt + ' --set "level=full" --format json'})
            assert out.data["form"]["values"] == {"level": "full"} and out.data["invoke"]["prompt"] == "/agent-b M1 full"
            bad = await rt.hub.call("run_skill_script", {"command": "python3 view.py home && rm -rf /"})
            assert bad.is_error and "하나씩" in bad.text
        finally:
            current_user.reset(tok)
        # steps 없는 외부 스킬도 토큰 없이 시작 (문서의 첫 명령, 화면 JSON)
        res, _ = await rt.run_skill("model-menu", {})
        ev = res.tool_events[0]
        assert res.success and ev.view and ev.view["view"] == "home", res.output
        cmds = {x.name: x for x in rt.skills.list()[0]}
        assert cmds["model-menu"].summary()["entry"].endswith("view.py home") and cmds["agent-b"].summary()["entry"] is None


def test_api_stage_install_update_and_version(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import API_VERSION, create_app

    cfg = load_config(_cfg(tmp_path, "skills:\n  editors: [kim]\n"))
    app = create_app(cfg, runtime=AgentRuntime(cfg), enable_scheduler=False)
    KIM, LEE = {"X-Agent-User": "kim"}, {"X-Agent-User": "lee"}
    data = _zip(_repo(tmp_path / "repo"))
    with TestClient(app) as c:
        # 화면과 서버 버전이 맞는지 (git pull 후 서버를 재시작하지 않으면 405 → 화면에 경고)
        assert c.get("/api/info").json()["api"] == API_VERSION and f"const UI_API = {API_VERSION};" in c.get("/").text
        assert c.post("/api/skills/stage?filename=a.zip", content=data, headers=LEE).status_code == 403
        st = c.post("/api/skills/stage?filename=skill_test-main.zip", content=data, headers=KIM).json()
        assert {s["name"] for s in st["skills"]} == {"model-menu", "agent-b"} and st["source"]["type"] == "zip"
        r = c.post(f"/api/skills/stage/{st['id']}/install", json={"names": ["model-menu", "agent-b"]}, headers=KIM).json()
        assert len(r["installed"]) == 2
        assert c.post(f"/api/skills/stage/{st['id']}/install", json={}, headers=KIM).status_code == 400  # 설치하면 스테이지 정리
        items = {x["name"]: x for x in c.get("/api/commands", headers=LEE).json()["items"] if x["kind"] == "skill"}
        assert items["model-menu"]["offline"] is True and items["agent-b"]["offline"] is False
        assert [i["name"] for i in items["agent-b"]["inputs"]] == ["level", "regions", "notes"]
        # 붙여넣은 SKILL.md, 폴더(브라우저 업로드)
        st2 = c.post("/api/skills/stage", json={"content": "---\nname: pasted\ndescription: 붙여넣기\n---\n본문"}, headers=KIM).json()
        assert st2["skills"][0]["name"] == "pasted"
        import base64

        files = [{"path": "mydir/" + p.relative_to(tmp_path / "repo").as_posix(), "data": base64.b64encode(p.read_bytes()).decode()}
                 for p in (tmp_path / "repo").rglob("*") if p.is_file()]
        st3 = c.post("/api/skills/stage", json={"files": files, "label": "mydir"}, headers=KIM).json()
        assert {s["name"]: s["exists"] for s in st3["skills"]} == {"model-menu": True, "agent-b": True}
        assert c.post("/api/skills/stage", json={"folder": str(tmp_path / "nope")}, headers=KIM).status_code == 400
        assert "자동 갱신할 수 없습니다" in c.post("/api/skills/model-menu/update", headers=KIM).json()["detail"]
        # 예전 방식 /api/skills/import 도 여러 스킬 설치
        r = c.post("/api/skills/import?overwrite=true", content=data, headers=KIM).json()
        assert {x["name"] for x in r["installed"]} == {"model-menu", "agent-b"}
        html = c.get("/").text
        assert "importDialog" in html and "renderView" in html and "staleBanner" in html


def test_cli_skills_import_list(tmp_path: Path, capsys) -> None:
    import asyncio

    from inhouse_agent import cli

    cfg = load_config(_cfg(tmp_path))
    repo = _repo(tmp_path / "repo")
    ns = types.SimpleNamespace(skills_cmd="import", source=str(repo), only=None, overwrite=False, ref=None, path=None, list=True)
    assert asyncio.run(cli.cmd_skills(cfg, ns)) == 0
    assert "- model-menu" in capsys.readouterr().out and not (tmp_path / "skills" / "model-menu").exists()
    ns.list, ns.only = False, "agent-b"
    assert asyncio.run(cli.cmd_skills(cfg, ns)) == 0
    assert "설치: agent-b" in capsys.readouterr().out and (tmp_path / "skills" / "agent-b" / "ui.json").is_file()
    assert sys.platform  # (Windows 경로는 parse_command 테스트에서 확인)
