"""제품 개발 규칙 학습: 베이스 → 개발 모델 표와 레벨 BOM 에서 규칙 후보 → 담당자 답 → 스킬 반영 → 채점 → 자료 추가 후 반복."""

from __future__ import annotations

from pathlib import Path

import openpyxl
from test_agent import ScriptedLLM, write_config

from inhouse_agent.config import load_config
from inhouse_agent.product_rules import _level, parse_model, read_boms
from inhouse_agent.runtime import AgentRuntime

PATTERN = r"(?P<series>[A-Z]{2}\d{2})(?P<capacity>\d{2})(?P<color>[WBS])\.(?P<suffix>\w+)"
DOOR = {"FW24": {"W": "D24-W01", "B": "D24-B01", "S": "D24-S01"}, "FW27": {"W": "D27-W01", "B": "D27-B01"}}
DECO = {"W": "PD-W", "B": "PD-B", "S": "PD-S"}
MOTOR = {"09": "MT-090", "12": "MT-120"}


def bom_rows(model: str, deco_color: str | None = None) -> list[list]:
    """레벨 BOM: 세탁기 > 도어 Assy > 도어 커버(색상별), 컨트롤 패널 > 패널 데코(색상별), 모터(용량별), 드럼(공통), 라벨(모델마다 다름)."""
    a = parse_model(model, PATTERN)
    return [
        [model, 0, f"SET-{model}", "세탁기"],
        [model, 1, f"DA-{a['series']}", "도어 Assy"],
        [model, 2, DOOR[a["series"]][a["color"]], "도어 커버"],
        [model, 2, "GL-001", "도어 글라스"],
        [model, 1, "CP-001", "컨트롤 패널"],
        [model, 2, DECO[deco_color or a["color"]], "패널 데코"],
        [model, 1, MOTOR[a["capacity"]], "모터"],
        [model, 1, "DR-001", "드럼"],
        [model, 1, f"LB-{model}", "라벨"],
    ]


PROJECTS = [  # 베이스, 개발, 툴, 향지
    ("FW2409W.AKOR", "FW2409B.AKOR", "T24", "KR"), ("FW2409W.AKOR", "FW2409S.AKOR", "T24", "KR"),
    ("FW2409W.AKOR", "FW2412W.AEUR", "T24", "EU"), ("FW2412W.AEUR", "FW2412B.AEUR", "T24", "EU"),
    ("FW2709W.AKOR", "FW2709B.AKOR", "T27", "KR"), ("FW2709W.AKOR", "FW2712W.AUSA", "T27", "US"),
]
EXTRA = [("FW2712W.AUSA", "FW2712B.AUSA", "T27", "US")]


def write_files(root: Path, projects: list[tuple], name: str = "", deco_exception: str | None = None) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["개발 건 목록"])  # 실제 사내 엑셀처럼 제목 행이 먼저
    ws.append(["베이스 모델", "개발 모델", "툴", "향지"])
    for row in projects:
        ws.append(list(row))
    pj = root / f"개발건{name}.xlsx"
    wb.save(pj)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["모델", "레벨", "품번", "설명"])
    for m in sorted({x for row in projects for x in row[:2]}):
        for r in bom_rows(m, "W" if m == deco_exception else None):
            ws.append(r)
    bm = root / f"bom{name}.xlsx"
    wb.save(bm)
    return pj, bm


def test_levels_and_parents(tmp_path: Path) -> None:
    assert [_level(x) for x in (1, "2", 3.0, "..3", "1.2.1", "")] == [1, 2, 3, 3, 3, 1]
    _, bm = write_files(tmp_path, PROJECTS[:1])
    b = read_boms([bm])["FW2409B.AKOR"]
    assert b["도어 Assy > 도어 커버"] == "D24-B01"  # 부품 자리 = 바로 위 레벨 부모 > 설명
    assert b["컨트롤 패널 > 패널 데코"] == "PD-B" and b["세탁기 > 모터"] == "MT-090"
    assert parse_model("FW2409B.AKOR", PATTERN) == {"series": "FW24", "capacity": "09", "color": "B", "suffix": "AKOR"}
    assert parse_model("abc.suf", None) == {"name": "abc", "suffix": "suf"}


async def test_learn_ask_answer_export_evaluate_iterate(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        call = rt.hub.call
        files = Path(rt.workspace.files)
        pj, bm = write_files(files / "prod", PROJECTS, deco_exception="FW2409S.AKOR")  # 한 건은 색이 바뀌어도 패널 데코를 안 바꿈
        out = await call("product_rules_learn", {"name": "washer", "projects": [str(pj)], "boms": [str(bm)], "pattern": PATTERN})
        assert not out.is_error, out.text
        assert out.data["projects"] == 6 and out.data["with_bom"] == 6 and "담당자에게 확인할 것" in out.text

        rules = (await call("product_rules_show", {"name": "washer"})).data["rules"]
        by_text = {r["text"]: r for r in rules}
        door = by_text["모델명의 'color' 이(가) 바뀌면 '도어 Assy > 도어 커버' 부품이 바뀐다"]
        assert (door["support"], door["total"]) == (4, 4) and "D24-W01 → D24-B01" in door["evidence"][0]
        deco = by_text["모델명의 'color' 이(가) 바뀌면 '컨트롤 패널 > 패널 데코' 부품이 바뀐다"]
        assert (deco["support"], deco["total"]) == (3, 4) and deco["exceptions"] == ["FW2409W.AKOR → FW2409S.AKOR"]
        assert "모델명의 'capacity' 이(가) 바뀌면 '세탁기 > 모터' 부품이 바뀐다" in by_text
        assert not any("라벨" in t for t in by_text)  # 무엇을 바꾸든 늘 바뀌는 부품은 규칙이 아님
        assert not any("드럼" in t or "글라스" in t for t in by_text)  # 안 바뀌는 부품도 아님
        cat = next(r for r in rules if r["kind"] == "catalog" and r["tool"] == "T24" and "도어 커버" in r["slot"])
        assert cat["mapping"] == {"B": "D24-B01", "S": "D24-S01", "W": "D24-W01"}  # T24 도어 커버는 3색 개발됨
        cat27 = next(r for r in rules if r["kind"] == "catalog" and r["tool"] == "T27" and "도어 커버" in r["slot"])
        assert cat27["mapping"] == {"B": "D27-B01", "W": "D27-W01"}  # T27 은 실버가 없음
        base = {r["text"] for r in rules if r["kind"] == "base"}
        assert "베이스는 개발 모델과 'series' 이(가) 같은 모델에서 고른다" in base
        assert "'T24' 개발에서는 베이스로 'FW2409W.AKOR' 을(를) 가장 많이 쓴다" in base

        # 사람에게 물을 것: 예외가 있는 애매한 규칙이 먼저
        q = (await call("product_rules_show", {"name": "washer", "view": "review"})).data["questions"]
        assert q[0]["id"] == deco["id"] and "3/4건" in q[0]["question"]
        # 담당자 답: 데코 규칙은 조건을 고쳐 확정, 도어는 확정, 베이스 인기 규칙 하나는 틀림
        r = await call("product_rules_answer", {"name": "washer", "rule_id": deco["id"], "verdict": "edited",
                                                "text": "색상이 바뀌면 패널 데코도 바뀐다. 단 실버는 화이트 데코를 같이 쓴다", "note": "실버 전용 데코 없음"})
        assert not r.is_error and "수정해서 확정" in r.text
        await call("product_rules_answer", {"name": "washer", "rule_id": door["id"], "verdict": "confirmed"})
        pop = next(r for r in rules if r["kind"] == "base" and r.get("base") == "FW2709W.AKOR")
        await call("product_rules_answer", {"name": "washer", "rule_id": pop["id"], "verdict": "rejected", "note": "우연히 겹친 것"})
        bad = await call("product_rules_answer", {"name": "washer", "rule_id": "nope", "verdict": "confirmed"})
        assert bad.is_error and "없습니다" in bad.text

        # 새 개발 건에 적용: 바뀔 부품과 쓸 품번, 개발 안 된 색은 알려줌
        chk = await call("product_rules_check", {"name": "washer", "base": "FW2412W.AEUR", "dev": "FW2412S.AEUR"})
        assert "'도어 Assy > 도어 커버' 변경 (color: W → S) → 품번 D24-S01 [확정]" in chk.text
        chk27 = await call("product_rules_check", {"name": "washer", "base": "FW2709W.AKOR", "dev": "FW2709S.AKOR"})
        assert "도어 커버' 변경 (color: W → S) → 개발된 품번 없음" in chk27.text  # T27 에는 실버 도어 커버가 없음

        # 채점: 과거 건을 하나씩 빼고 맞히기
        ev = await call("product_rules_evaluate", {"name": "washer"})
        assert ev.data["projects"] == 6 and ev.data["precision"] >= 0.8 and ev.data["recall"] is not None
        assert "라벨" in " ".join(ev.data["often_missed"])  # 규칙으로 설명 안 되는 변경이 무엇인지 보여줌

        # 스킬에 반영 (확정·수정 규칙과 담당자 메모, 틀린 추정은 '쓰지 말 것')
        sk = Path(rt.skills.root) / "new_model_bom"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: new_model_bom\ndescription: 신규 모델 BOM 검토\n---\n규칙은 rules/washer.md 를 읽으세요.\n", encoding="utf-8")
        ex = await call("product_rules_export_skill", {"name": "washer", "skill": "new_model_bom"})
        assert not ex.is_error, ex.text
        md = (sk / "rules" / "washer.md").read_text(encoding="utf-8")
        assert "단 실버는 화이트 데코를 같이 쓴다 (3/4건) - 담당자 메모: 실버 전용 데코 없음" in md
        assert "## 틀린 것으로 확인된 추정 (쓰지 말 것)" in md and "우연히 겹친 것" in md
        assert md.index("## 바뀌는 부품 - 확정") < md.index("## 바뀌는 부품 - 후보")

        # 반복: 새 개발 건이 들어옴 → 다시 학습해도 사람의 답은 그대로, 근거 건수는 갱신
        pj2, bm2 = write_files(files / "prod", EXTRA, name="_2")
        out2 = await call("product_rules_learn", {"name": "washer", "projects": [str(pj2)], "boms": [str(bm2)]})
        assert out2.data["round"] == 2 and out2.data["projects"] == 7
        rules2 = {r["id"]: r for r in (await call("product_rules_show", {"name": "washer"})).data["rules"]}
        assert rules2[door["id"]]["status"] == "confirmed" and rules2[door["id"]]["support"] == 5
        assert rules2[door["id"]].get("changed_since_answer") and rules2[deco["id"]]["status"] == "edited"
        assert rules2[pop["id"]]["status"] == "rejected"  # 틀림이라고 한 것은 다시 후보로 올라오지 않음
        hist = (await call("product_rules_show", {"name": "washer", "view": "history"})).data["history"]
        assert [h["event"] for h in hist].count("learn") == 2 and any(h["event"] == "answer" for h in hist)


async def test_helpful_errors(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        call = rt.hub.call
        files = Path(rt.workspace.files)
        wb = openpyxl.Workbook()
        wb.active.append(["A", "B"])
        wb.active.append(["x", "y"])
        wb.save(files / "odd.xlsx")
        r = await call("product_rules_learn", {"name": "w", "projects": ["files/odd.xlsx"], "boms": []})
        assert r.is_error and "cols 로 열 이름을" in r.text
        r = await call("product_rules_learn", {"name": "w", "projects": ["files/odd.xlsx"], "boms": [], "cols": {"base": "A", "dev": "B"}})
        assert not r.is_error and "BOM 이 없는 모델: x, y" in r.text and "모델명 규칙(pattern)이 없어" in r.text
        assert (await call("product_rules_learn", {"name": "w", "projects": [], "boms": [], "pattern": "(("})).is_error
        assert (await call("product_rules_show", {})).data["names"] == ["w"]  # 이름 없이 부르면 목록


async def test_loop_skill_runs_without_llm(tmp_path: Path) -> None:
    """저장소의 product_rule_loop 스킬: LLM 없이 학습 → 질문 → 채점까지."""
    from inhouse_agent.skills import parse_skill
    from inhouse_agent.workflow import run_workflow, validate_steps

    folder = Path(__file__).resolve().parents[1] / "skills" / "product_rule_loop"
    skill = parse_skill((folder / "SKILL.md").read_text(encoding="utf-8"), folder)
    cfg = load_config(write_config(tmp_path))
    async with AgentRuntime(cfg, llm=ScriptedLLM([])) as rt:
        pj, bm = write_files(Path(rt.workspace.files) / "prod", PROJECTS)
        inputs = skill.resolve_inputs({"rulebook": "washer", "projects": str(pj), "boms": str(bm), "pattern": PATTERN}, "Asia/Seoul")
        r = await run_workflow(rt.hub, validate_steps(skill.steps), inputs, timezone="Asia/Seoul", output_template=skill.output_template)
    assert r.success, r.output
    assert "학습 1회차" in r.output and "담당자에게 확인할 것" in r.output and "정밀도" in r.output
    assert "/product_rules_answer name=washer" in r.output
