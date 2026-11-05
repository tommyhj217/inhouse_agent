"""여러 사용자에게 서비스: 스킬 공유(작성자 표시), 편집 권한, 서버 주소, CLI / 스킬 메뉴."""

from __future__ import annotations

import builtins
import shutil
import sys
import types
from pathlib import Path

import pytest
from test_agent import write_config

from inhouse_agent.config import load_config
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.skills import SkillStore
from inhouse_agent.users import current_user

ROOT = Path(__file__).resolve().parents[1]
SK = "---\nname: {n}\ndescription: {d}\nsteps:\n  - tool: get_current_time\n---\n본문"


def _cfg(tmp_path: Path, extra: str = "", offline: bool = True) -> Path:
    p = write_config(tmp_path)
    text = p.read_text(encoding="utf-8")
    if offline:
        text = text.replace("llm:\n", "llm:\n  provider: none\n", 1)
    p.write_text(text + "\n" + extra, encoding="utf-8")
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    return p


def test_skill_author_metadata(tmp_path: Path) -> None:
    st = SkillStore(tmp_path / "skills")
    st.create("team_report", "팀 보고", user="kim")
    a = st.get("team_report")
    assert a.info["created_by"] == "kim" and a.info["updated_by"] == "kim"
    st.save("team_report", SK.format(n="team_report", d="수정본"), user="lee")
    b = st.get("team_report")
    assert b.info["created_by"] == "kim" and b.info["updated_by"] == "lee" and b.summary()["updated_by"] == "lee"
    # GitHub 로 받은 스킬(.meta.json 없음)도 시각은 표시
    (tmp_path / "skills" / "from_git").mkdir()
    (tmp_path / "skills" / "from_git" / "SKILL.md").write_text(SK.format(n="from_git", d="깃"), encoding="utf-8")
    assert st.get("from_git").info.get("updated") and st.get("from_git").info.get("created_by") is None


def test_shared_skills_and_editor_permission(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(_cfg(tmp_path, "skills:\n  editors: [kim, 'admin*']\n"))
    app = create_app(cfg, runtime=AgentRuntime(cfg), enable_scheduler=False)
    KIM, LEE = {"X-Agent-User": "kim"}, {"X-Agent-User": "lee"}
    with TestClient(app) as c:
        assert c.get("/api/info", headers=KIM).json()["can_edit_skills"] is True
        assert c.get("/api/info", headers=LEE).json()["can_edit_skills"] is False
        assert c.get("/api/info").json()["can_edit_skills"] is False  # 편집자를 정했으면 ID 없는 사용자도 편집 불가
        # kim 이 만든 스킬 → lee 의 / 목록에 바로 보임 (작성자, 새 스킬 표시)
        assert c.post("/api/skills", json={"name": "weekly_check", "content": SK.format(n="weekly_check", d="주간 점검")}, headers=KIM).status_code == 200
        items = {x["name"]: x for x in c.get("/api/commands", headers=LEE).json()["items"] if x["kind"] == "skill"}
        assert items["weekly_check"]["created_by"] == "kim" and items["weekly_check"]["new"] is True and items["weekly_check"]["offline"] is True
        assert {"monthly_sales_report", "notice_digest", "learn_rules", "product_analysis"} <= set(items)
        # lee 는 편집 불가, 실행은 가능
        assert c.put("/api/skills/weekly_check", json={"content": SK.format(n="weekly_check", d="x")}, headers=LEE).status_code == 403
        assert c.post("/api/skills", json={"name": "lee_skill"}, headers=LEE).status_code == 403
        assert c.delete("/api/skills/weekly_check", headers=LEE).status_code == 403
        assert c.put("/api/ctools/dbl", json={"description": "d", "code": "def run(args, ctx):\n    return 1\n"}, headers=LEE).status_code == 403
        run = c.post("/api/runs", json={"skill": "weekly_check", "inputs": {}, "new_session": True}, headers=LEE).json()
        with c.stream("GET", f"/api/runs/{run['id']}/events", headers=LEE) as s:
            body = "".join(s.iter_text())
        assert '"status": "completed"' in body
        assert c.put("/api/skills/weekly_check", json={"content": SK.format(n="weekly_check", d="관리자 수정")}, headers={"X-Agent-User": "admin01"}).status_code == 200
        html = c.get("/").text
        assert "renderWelcome" in html and "slash-more" in html and "applyEditPermission" in html


async def test_agent_create_tool_respects_editors(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path, "skills:\n  editors: [kim]\ndynamic_tools:\n  mode: auto\n"))
    async with AgentRuntime(cfg) as rt:
        args = {"name": "t1", "description": "d", "parameters": {}, "code": "def run(args, ctx):\n    return 1\n", "test_args": {}}
        tok = current_user.set("lee")
        out = await rt.hub.call("create_tool", args)
        current_user.reset(tok)
        assert out.is_error and "권한" in out.text
        tok = current_user.set("kim")
        out = await rt.hub.call("create_tool", args)
        current_user.reset(tok)
        assert not out.is_error, out.text


def test_serve_prints_lan_urls_and_sets_public_url(tmp_path: Path, monkeypatch, capsys) -> None:
    pytest.importorskip("fastapi")
    from inhouse_agent import cli

    cfg = load_config(_cfg(tmp_path))
    captured = {}
    fake = types.SimpleNamespace(run=lambda app, host, port, log_level: captured.update(app=app, host=host, port=port))
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setattr(cli, "lan_addresses", lambda: ["10.1.2.3"])
    cli.cmd_serve(cfg, types.SimpleNamespace(host="0.0.0.0", port=8123, no_scheduler=True))
    out = capsys.readouterr().out
    assert "다른 PC 에서:    http://10.1.2.3:8123" in out and "http://localhost:8123" in out
    assert captured["host"] == "0.0.0.0" and captured["app"].state.runtime.links.public_url == "http://10.1.2.3:8123"


def test_cli_chat_slash_skills_offline(tmp_path: Path, monkeypatch, capsys) -> None:
    import asyncio

    from inhouse_agent import cli

    cfg = load_config(_cfg(tmp_path))
    lines = iter(["/", "/monthly_sales_report month=2026-05", "/get_current_time", "아무 질문", "/없는명령", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(lines))
    rc = asyncio.run(cli.cmd_chat(cfg, types.SimpleNamespace(servers=None, skill=None, input=None, resume=None, verbose=False)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "[사용할 수 있는 스킬]" in out and "/monthly_sales_report month={last_month}" in out and "(토큰 없이 가능)" in out
    assert "2026-05 매출 집계" in out and "Asia/Seoul" in out
    assert "자유 질문에는 답할 수 없습니다" in out and "/ 를 입력하면 스킬 목록" in out
