"""사용자별 작업폴더 / 작업 이어서 하기 / 채팅 이미지 테스트."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.llm import LLMResponse, ToolCall, _api_messages
from inhouse_agent.runtime import AgentRuntime
from inhouse_agent.users import UserError, current_user, normalize_user


def _png(path: Path, size=(40, 30), color=(200, 30, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def _png_bytes(size=(40, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class as_user:
    def __init__(self, uid):
        self.uid = uid

    def __enter__(self):
        self.t = current_user.set(self.uid)

    def __exit__(self, *a):
        current_user.reset(self.t)


def test_normalize_user() -> None:
    assert normalize_user("kim.hj") == "kim.hj" and normalize_user("홍길동") == "홍길동" and normalize_user("a@b.com") == "a@b.com"
    assert normalize_user("  ") is None
    for bad in ("../x", "a/b", "a b", ".hidden", "x" * 65):
        with pytest.raises(UserError):
            normalize_user(bad)


async def test_user_isolation_memory_and_claim(tmp_path: Path) -> None:
    pytest.importorskip("openpyxl")
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        with as_user("kim"):
            out = await rt.hub.call("save_file", {"path": "memo.txt", "content": "김"})
            kim_file = Path(out.files[0]["path"])
            assert kim_file == (rt.workspace.root / "files" / "memo.txt") and "users/kim" in kim_file.as_posix()
            await rt.hub.call("remember", {"key": "선호", "content": "표는 부서순"})
            # 공용 다운로드 폴더(MCP 가 저장하는 곳)에 생긴 파일 → kim 의 downloads 로 이동
            out = await rt.hub.call("demo__download_sales_excel", {"month": "2026-10", "wait": True})
            f = [x for x in out.files if x["name"] == "매출현황_2026-10.xlsx"]
            assert f and "users/kim/downloads" in f[0]["path"].replace("\\", "/"), out.text
            assert "사용자 폴더로 옮김" in out.text
            assert not (rt.base_workspace.downloads / "매출현황_2026-10.xlsx").exists()
        with as_user("lee"):
            with pytest.raises(PermissionError):
                rt.workspace.resolve(kim_file)  # 다른 사용자 파일은 접근 불가
            assert "저장된 기억이 없습니다" in (await rt.hub.call("recall", {})).text
            assert rt.workspace.root.name == "lee"
        # 사용자 없이(관리자/예약작업) 는 전체 workspace 접근
        assert rt.workspace.resolve(kim_file) == kim_file
        assert sorted(rt.users.list_users()) == ["kim", "lee"]


async def test_images_in_messages_and_view_image(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    cfg = load_config(write_config(tmp_path))
    img = _png(tmp_path / "shot.png", size=(3000, 1000))
    llm = ScriptedLLM([
        LLMResponse("첫 답"),
        LLMResponse(None, [ToolCall("v1", "view_image", json.dumps({"path": "downloads/cap.png"}))]),
        LLMResponse("둘째 답"),
    ])
    async with AgentRuntime(cfg, llm=llm) as rt:
        with as_user("kim"):
            up = rt.workspace.uploads / "images" / "shot.png"
            up.parent.mkdir(parents=True, exist_ok=True)
            up.write_bytes(img.read_bytes())
            msgs = rt.agent.new_conversation()
            await rt.agent.run("이 화면 봐줘", messages=msgs, images=[str(up)])
            user_msg = llm.calls[0][-1]
            kinds = [p["type"] for p in user_msg["content"]]
            assert kinds == ["text", "text", "image_url"] and user_msg["_images"] == [str(up)]
            url = user_msg["content"][2]["image_url"]["url"]
            assert url.startswith("data:image/png;base64,")
            import base64

            from PIL import Image

            w, h = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size
            assert max(w, h) == 2048  # 큰 이미지는 축소

            _png(rt.workspace.downloads / "cap.png")
            await rt.agent.run("저장된 캡처도 봐", messages=msgs)
            last = llm.calls[-1]
            first_user = next(m for m in last if m["role"] == "user")
            assert not any(p.get("type") == "image_url" for p in first_user["content"])  # 이전 턴 이미지는 텍스트로
            injected = last[-1]
            assert injected["role"] == "user" and any(p.get("type") == "image_url" for p in injected["content"])
            # API 로 보낼 때 내부 키 제거 / 비전 미지원이면 이미지 → 안내 문구
            api = _api_messages(last, vision=True)
            assert all(not any(k.startswith("_") for k in m) for m in api)
            novision = _api_messages(last, vision=False)
            assert isinstance(novision[-1]["content"], str) and "이미지를 볼 수 없습니다" in novision[-1]["content"]


def test_web_users_sessions_images_resume(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("PIL")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    cfg = load_config(write_config(tmp_path))
    llm = ScriptedLLM([LLMResponse("빨간 사각형 이미지입니다"), LLMResponse("이어서: 아까 이미지 기준으로 정리했습니다")])

    def stream(c, rid, h):
        with c.stream("GET", f"/api/runs/{rid}/events", headers=h) as s:
            return [json.loads(x[6:]) for x in "".join(s.iter_text()).splitlines() if x.startswith("data: ")]

    KIM = {"X-Agent-User": "kim"}
    LEE = {"X-Agent-User": "lee"}
    app = create_app(cfg, runtime=AgentRuntime(cfg, llm=llm), enable_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/me", headers={"X-Agent-User": "../x"}).status_code == 400
        assert c.get("/api/me", headers={"X-Agent-User": "%ED%99%8D%EA%B8%B8%EB%8F%99"}).json()["user"] == "홍길동"  # 한글 ID
        up = c.post("/api/images/upload?name=캡처.png", content=_png_bytes(), headers=KIM).json()
        assert up["ok"] and "users/kim/uploads/images" in up["path"].replace("\\", "/")
        assert c.post("/api/images/upload?name=x.png", content=b"not image", headers=KIM).status_code == 400
        # lee 는 kim 의 이미지를 첨부할 수 없음
        assert c.post("/api/runs", json={"prompt": "x", "images": [up["path"]]}, headers=LEE).status_code == 400

        run = c.post("/api/runs", json={"prompt": "이 이미지 뭐야?", "images": [up["path"]]}, headers=KIM).json()
        ev = stream(c, run["id"], KIM)
        assert ev[0]["images"] == [up["path"]] and ev[-1]["output"] == "빨간 사각형 이미지입니다"
        assert any(p["type"] == "image_url" for p in llm.calls[0][-1]["content"])
        assert c.get(f"/api/runs/{run['id']}", headers=LEE).status_code == 404  # 다른 사용자의 실행
        sid = run["session_id"]
        items = c.get("/api/sessions", headers=KIM).json()
        assert [i["id"] for i in items] == [sid] and items[0]["title"] == "이 이미지 뭐야?"
        assert c.get("/api/sessions", headers=LEE).json() == []
        assert c.get(f"/api/sessions/{sid}", headers=LEE).status_code == 404
        d = c.get(f"/api/sessions/{sid}", headers=KIM).json()
        assert d["turns"][0]["images"] == [up["path"]] and "messages" not in d
        # 이미지 미리보기(inline)
        r = c.get("/api/files/download", params={"path": up["path"], "inline": "true", "user": "kim"})
        assert r.status_code == 200 and r.headers["content-disposition"].startswith("inline")
        saved = json.loads((Path(up["path"]).parents[2] / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))
        assert "base64" not in json.dumps(saved)  # 저장본에는 이미지 데이터 대신 경로 참조

    # 서버 재시작 후에도 같은 세션으로 이어서
    app2 = create_app(cfg, runtime=AgentRuntime(cfg, llm=llm), enable_scheduler=False)
    with TestClient(app2) as c:
        run2 = c.post("/api/runs", json={"prompt": "아까 거 이어서 정리해줘", "session_id": sid}, headers=KIM).json()
        assert run2["session_id"] == sid
        ev = stream(c, run2["id"], KIM)
        assert ev[-1]["status"] == "completed"
        sent = llm.calls[-1]
        texts = json.dumps(sent, ensure_ascii=False)
        assert "이 이미지 뭐야?" in texts and "빨간 사각형 이미지입니다" in texts  # 이전 대화가 그대로 이어짐
        assert "[첨부 이미지:" in texts  # 이전 이미지는 경로 참조로 (view_image 로 다시 볼 수 있음)
        assert "현재 사용자: kim" in sent[0]["content"]
        # 이어지는 대화: 시스템 프롬프트가 첫 턴과 글자 하나 다르지 않음 (서버를 다시 켜도) → 앞부분 전체가 프롬프트 캐시로
        assert sent[0]["content"] == llm.calls[0][0]["content"] and sent[0]["_cache_key"] == llm.calls[0][0]["_cache_key"]
        assert sent[-1]["_sent"] and "보낸 시각" not in sent[-1]["content"]  # 시각은 API 로 보낼 때만 붙임
        d = c.get(f"/api/sessions/{sid}", headers=KIM).json()
        assert len(d["turns"]) == 2
        assert c.patch(f"/api/sessions/{sid}", json={"title": "이미지 분석"}, headers=KIM).json()["ok"]
        assert c.get("/api/sessions", headers=KIM).json()[0]["title"] == "이미지 분석"
        assert c.delete(f"/api/sessions/{sid}", headers=KIM).json()["ok"]
        assert c.get("/api/sessions", headers=KIM).json() == []


def test_require_user_and_past_work_tool(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from inhouse_agent.server import create_app

    p = write_config(tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + "\nusers:\n  require: true\n  allowed: ['kim*']\n", encoding="utf-8")
    cfg = load_config(p)
    app = create_app(cfg, runtime=AgentRuntime(cfg, llm=ScriptedLLM([LLMResponse("결과 A")])), enable_scheduler=False)
    with TestClient(app) as c:
        assert c.get("/api/sessions").status_code == 401
        assert c.get("/api/info").json()["users"]["require"] is True  # 화면이 ID 를 물어볼 수 있게
        assert c.get("/api/me", headers={"X-Agent-User": "park"}).status_code == 400  # 허용 목록 밖
        run = c.post("/api/runs", json={"prompt": "매출 정리"}, headers={"X-Agent-User": "kim1"}).json()
        with c.stream("GET", f"/api/runs/{run['id']}/events", headers={"X-Agent-User": "kim1"}) as s:
            "".join(s.iter_text())
        rt = app.state.runtime
    with as_user("kim1"):
        import asyncio

        out = asyncio.run(rt.hub.call("list_past_work", {"query": "매출"}))
        assert run["session_id"] in out.text
        out = asyncio.run(rt.hub.call("read_past_work", {"session_id": run["session_id"]}))
        assert "매출 정리" in out.text and "결과 A" in out.text


async def test_schedule_runs_in_user_workspace(tmp_path: Path) -> None:
    p = write_config(tmp_path)
    p.write_text(p.read_text(encoding="utf-8").replace('    prompt: "{yesterday} 공지 정리"', '    prompt: "{yesterday} 공지 정리"\n    user: kim'), encoding="utf-8")
    cfg = load_config(p)
    async with AgentRuntime(cfg, llm=ScriptedLLM([LLMResponse("예약 결과")])) as rt:
        _, path = await rt.run_job(cfg.get_schedule("notice"))
    assert "users/kim/results/notice" in path.as_posix()
