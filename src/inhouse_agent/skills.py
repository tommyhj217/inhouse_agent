"""스킬: 반복 업무 절차를 SKILL.md 로 정의하고 에이전트가 불러 쓰는 구조.

skills/<이름>/SKILL.md
---
name: notice_digest
description: 그룹웨어 공지사항을 가져와 부서별로 요약 (언제 이 스킬을 쓰는지 적기)
servers: [groupware]          # (선택) 이 스킬에서 쓸 MCP 서버
inputs:                       # (선택) 실행 시 받는 입력값 → 웹 화면에서 입력 폼으로 표시
  - name: date
    label: 조회일
    type: date                # string | text | number | date | select | boolean | file
    default: "{yesterday}"
    required: true
  - name: dept
    type: select
    options: [전체, 인사팀, IT운영팀]
    default: 전체
---
# 절차
1. {date} 공지 목록을 가져온다 ...

같은 폴더의 다른 파일(템플릿, 참고자료)은 read_skill_file tool 로 읽을 수 있습니다.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .runtime_utils import render_dates

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
INPUT_TYPES = {"string", "text", "number", "date", "select", "boolean", "file"}
_FM = re.compile(r"\A﻿?---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.S)


class SkillError(ValueError):
    pass


@dataclass
class SkillInput:
    name: str
    label: str = ""
    type: str = "string"
    description: str = ""
    default: Any = None
    required: bool = False
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "type": self.type,
            "description": self.description,
            "default": self.default,
            "required": self.required,
            "options": self.options,
        }


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    servers: list[str] | None = None
    inputs: list[SkillInput] = field(default_factory=list)
    raw: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    """LLM 없이 실행할 단계 (workflow.py). 비어 있으면 LLM 이 지침(body)을 보고 실행."""
    output_template: str | None = None
    info: dict[str, Any] = field(default_factory=dict)
    """누가 언제 만들고 고쳤는지 (.meta.json) - 모든 사용자에게 보여줌"""

    @property
    def files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.path)).replace("\\", "/")
            for p in self.path.rglob("*")
            if p.is_file() and p.name != "SKILL.md" and not p.name.startswith(".")
        )

    def resolve_inputs(self, given: dict[str, Any] | None, timezone: str) -> dict[str, Any]:
        given = {k: v for k, v in (given or {}).items() if v not in (None, "")}
        values: dict[str, Any] = {}
        missing = []
        for inp in self.inputs:
            v = given.get(inp.name, inp.default)
            if isinstance(v, str):
                v = render_dates(v, timezone)
            if inp.required and v in (None, ""):
                missing.append(inp.label or inp.name)
            if inp.type == "number" and v not in (None, ""):
                try:
                    v = float(v) if "." in str(v) else int(v)
                except ValueError as e:
                    raise SkillError(f"입력값 '{inp.name}' 은 숫자여야 합니다: {v}") from e
            if inp.type == "boolean" and isinstance(v, str):
                v = v.lower() in ("1", "true", "yes", "y", "on", "예")
            if inp.type == "select" and inp.options and v not in (None, "") and str(v) not in inp.options:
                raise SkillError(f"입력값 '{inp.name}' 은 {inp.options} 중 하나여야 합니다: {v}")
            values[inp.name] = v
        if missing:
            raise SkillError(f"필수 입력값이 없습니다: {', '.join(missing)}")
        for k, v in given.items():  # 정의되지 않은 추가 입력도 전달
            values.setdefault(k, v)
        return values

    def render(self, inputs: dict[str, Any], timezone: str) -> str:
        """본문의 {입력이름} 과 {today} 등 날짜 변수 치환."""
        text = render_dates(self.body, timezone)
        for k, v in inputs.items():
            text = text.replace("{" + k + "}", "" if v is None else str(v))
        return text

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "servers": self.servers,
            "inputs": [i.to_dict() for i in self.inputs],
            "files": self.files,
            "path": str(self.path),
            "steps": len(self.steps),
            "workflow": bool(self.steps),
            "created_by": self.info.get("created_by"),
            "created": self.info.get("created"),
            "updated_by": self.info.get("updated_by"),
            "updated": self.info.get("updated"),
        }


def parse_skill(text: str, folder: Path) -> Skill:
    m = _FM.match(text)
    if not m:
        raise SkillError("SKILL.md 맨 앞에 '---' 로 감싼 YAML 머리말(name, description)이 필요합니다.")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"머리말 YAML 오류: {e}") from e
    if not isinstance(meta, dict):
        raise SkillError("머리말은 key: value 형식이어야 합니다.")
    name = str(meta.get("name") or folder.name)
    if not NAME_RE.match(name):
        raise SkillError(f"스킬 이름은 영문/숫자/_/- 만 사용할 수 있습니다: {name}")
    desc = str(meta.get("description") or "").strip()
    if not desc:
        raise SkillError("description 이 필요합니다 (에이전트가 언제 이 스킬을 쓸지 판단하는 기준).")
    inputs = []
    for i, d in enumerate(meta.get("inputs") or []):
        if isinstance(d, str):
            d = {"name": d}
        if not isinstance(d, dict) or not d.get("name"):
            raise SkillError(f"inputs[{i}] 에 name 이 필요합니다.")
        typ = str(d.get("type") or "string")
        if typ not in INPUT_TYPES:
            raise SkillError(f"inputs.{d['name']}.type 은 {sorted(INPUT_TYPES)} 중 하나여야 합니다.")
        inputs.append(
            SkillInput(
                name=str(d["name"]),
                label=str(d.get("label") or ""),
                type=typ,
                description=str(d.get("description") or ""),
                default=d.get("default"),
                required=bool(d.get("required", False)),
                options=[str(o) for o in d.get("options") or []],
            )
        )
    names = [i.name for i in inputs]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SkillError(f"입력 이름이 중복됩니다: {', '.join(dup)}")
    from .workflow import WorkflowError, validate_steps

    raw_steps = meta.get("steps")
    if isinstance(raw_steps, list):  # script: scripts/x.py 단계 → run_skill_script 호출로 (LLM 없이 스크립트 실행)
        raw_steps = [
            {**{k: v for k, v in s.items() if k not in ("script", "args", "argv")}, "tool": "run_skill_script",
             "args": {"name": name, "path": s["script"], **({"args": s["args"]} if s.get("args") else {}), **({"argv": s["argv"]} if s.get("argv") else {})}}
            if isinstance(s, dict) and s.get("script") and not s.get("tool") else s
            for s in raw_steps
        ]
    try:
        steps = validate_steps(raw_steps)
    except WorkflowError as e:
        raise SkillError(str(e)) from e
    servers = meta.get("servers")
    return Skill(
        name=name,
        description=desc,
        body=m.group(2).strip(),
        path=folder,
        servers=[str(s) for s in servers] if servers else None,
        inputs=inputs,
        raw=text,
        meta=meta,
        steps=steps,
        output_template=str(meta["output"]) if meta.get("output") else None,
    )


SKILL_TEMPLATE = """---
name: {name}
description: {description}
# servers: [demo]            # 이 스킬에서 쓸 MCP 서버 (생략 시 전체)
inputs:
  - name: date
    label: 조회일
    type: date
    default: "{{yesterday}}"
    required: true
# (선택) LLM 없이(토큰 없이) 실행할 단계 - 채팅창에서 /{name} 으로 바로 테스트
# steps:
#   - id: s1
#     tool: get_current_time
#   - id: menu                  # 스킬 폴더의 스크립트 실행 (scripts/ 에 파일 추가)
#     script: scripts/make_menu.py
#     args: {{date: "{{date}}"}}     # → --date 2026-11-04
#   - id: s2
#     tool: list_downloads
#     args: {{recent_minutes: 60}}
# output: |
#   {{date}} 결과
#   {{steps.s2.text}}
---
# 목표
{description}

# 절차
1. 필요한 사이트(MCP tool)에서 {{date}} 기준 자료를 가져온다.
2. 파일을 내려받았다면 file_status 로 상태를 확인하고, 다운로드 중이면 wait_for_download 로 완료를 기다린다.
3. analyze_file / query_table 로 내용을 분석한다.
4. 판단이 필요한 부분은 ask_user 로 사용자에게 확인한다.

# 결과 형식
- 핵심 요약 3줄
- 상세 표
- 사용한 출처와 파일 경로
"""


class SkillStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise SkillError(f"잘못된 스킬 이름: {name}")
        return self.root / name

    @staticmethod
    def _info(d: Path) -> dict[str, Any]:
        import json

        info: dict[str, Any] = {}
        mf = d / ".meta.json"
        if mf.is_file():
            try:
                info = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
        f = d / "SKILL.md"
        if f.is_file():
            info.setdefault("updated", f.stat().st_mtime)  # 파일로 직접 넣은 스킬(GitHub 등)도 시각 표시
            info.setdefault("created", f.stat().st_mtime)
        return info

    def _load(self, d: Path) -> Skill:
        sk = parse_skill((d / "SKILL.md").read_text(encoding="utf-8"), d)
        sk.info = self._info(d)
        return sk

    def list(self) -> tuple[list[Skill], dict[str, str]]:
        """(정상 스킬 목록, {폴더명: 오류}) - 오류난 스킬 때문에 전체가 멈추지 않도록. 매번 디스크에서 읽으므로
        누가 추가/수정한 스킬이든 모든 사용자에게 바로 보임."""
        skills, errors = [], {}
        for d in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            try:
                skills.append(self._load(d))
            except (SkillError, OSError, UnicodeDecodeError) as e:
                errors[d.name] = str(e)
        return skills, errors

    def get(self, name: str) -> Skill:
        d = self._dir(name)
        f = d / "SKILL.md"
        if not f.is_file():
            raise SkillError(f"스킬 '{name}' 을(를) 찾을 수 없습니다.")
        return self._load(d)

    def raw(self, name: str) -> str:
        return (self._dir(name) / "SKILL.md").read_text(encoding="utf-8")

    def save(self, name: str, content: str, user: str | None = None) -> Skill:
        d = self._dir(name)
        skill = parse_skill(content, d)  # 저장 전에 검증
        if skill.name != name:
            raise SkillError(f"머리말의 name({skill.name})과 스킬 폴더 이름({name})이 다릅니다.")
        d.mkdir(parents=True, exist_ok=True)
        f = d / "SKILL.md"
        if f.exists():  # 직전 버전 백업 (되돌리기용)
            hist = d / ".history"
            hist.mkdir(exist_ok=True)
            from datetime import datetime

            shutil.copy2(f, hist / f"SKILL_{datetime.now():%Y%m%d_%H%M%S}.md")
            backups = sorted(hist.glob("SKILL_*.md"))
            for old in backups[:-20]:
                old.unlink(missing_ok=True)
        f.write_text(content, encoding="utf-8")
        import json
        import time

        info = self._info(d) if (d / ".meta.json").is_file() else {}
        now = time.time()
        info.setdefault("created", now)
        info.setdefault("created_by", user)
        info.update(updated=now, updated_by=user)
        (d / ".meta.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        skill.info = info
        return skill

    def write_file(self, name: str, rel: str, data: bytes, user: str | None = None) -> Path:
        """스킬 폴더에 참고 파일/스크립트 추가 (예: scripts/make_menu.py, templates/form.xlsx)."""
        d = self._dir(name)
        if not (d / "SKILL.md").is_file():
            raise SkillError(f"스킬 '{name}' 을(를) 찾을 수 없습니다.")
        rel = str(rel).replace("\\", "/").strip("/")
        p = (d / rel).resolve()
        if d.resolve() not in p.parents or not rel or any(x.startswith(".") for x in Path(rel).parts) or p.name == "SKILL.md":
            raise SkillError(f"이 경로에는 파일을 둘 수 없습니다: {rel}")
        if len(data) > 50 * 1024 * 1024:
            raise SkillError("스킬 파일은 50MB 이하만 올릴 수 있습니다.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self._touch(d, user)
        return p

    def delete_file(self, name: str, rel: str, user: str | None = None) -> None:
        d = self._dir(name)
        p = (d / str(rel)).resolve()
        if d.resolve() not in p.parents or not p.is_file() or p.name == "SKILL.md":
            raise SkillError(f"지울 수 없는 파일입니다: {rel}")
        p.unlink()
        self._touch(d, user)

    def _touch(self, d: Path, user: str | None) -> None:
        import json
        import time

        info = self._info(d)
        info.update(updated=time.time(), updated_by=user)
        (d / ".meta.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")

    def import_zip(self, data: bytes, user: str | None = None, overwrite: bool = False, name: str | None = None) -> Skill:
        """다른 곳에서 만든 스킬 폴더(zip: SKILL.md + scripts/ 등)를 가져옴. 기존 스킬은 overwrite 일 때만 덮어씀(이전 것은 _trash)."""
        import io
        import zipfile

        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise SkillError("zip 파일이 아닙니다.") from e
        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        mds = sorted((n for n in names if n.split("/")[-1] == "SKILL.md"), key=lambda n: n.count("/"))
        if not mds:
            raise SkillError("zip 안에 SKILL.md 가 없습니다.")
        base = mds[0][: -len("SKILL.md")]
        text = z.read(mds[0]).decode("utf-8-sig")
        orig = parse_skill(text, self.root / "_tmp").name
        skill_name = name or orig
        if skill_name != orig:
            import re as _re

            text = _re.sub(r"(?m)^name:\s*.*$", f"name: {skill_name}", text, count=1)
        if not NAME_RE.match(skill_name):
            # 외부 스킬 이름(예: menu-builder)도 받도록 폴더 이름 규칙에 맞춤
            import re as _re

            skill_name = _re.sub(r"[^A-Za-z0-9_]", "_", skill_name).strip("_").lower() or "imported_skill"
            text = _re.sub(r"(?m)^name:\s*.*$", f"name: {skill_name}", text, count=1)
        d = self._dir(skill_name)
        if (d / "SKILL.md").exists():
            if not overwrite:
                raise SkillError(f"스킬 '{skill_name}' 이(가) 이미 있습니다. 덮어쓰려면 overwrite 를 선택하세요.")
            self.delete(skill_name)
        total = 0
        d.mkdir(parents=True, exist_ok=True)
        try:
            for n in names:
                if not n.startswith(base) or n == mds[0]:
                    continue
                rel = n[len(base):]
                p = (d / rel).resolve()
                if d.resolve() not in p.parents or any(x.startswith(".") for x in Path(rel).parts):
                    continue  # 경로 탈출 / 숨김 파일 무시
                info = z.getinfo(n)
                total += info.file_size
                if total > 100 * 1024 * 1024:
                    raise SkillError("스킬 zip 의 압축 해제 크기가 100MB 를 넘습니다.")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(z.read(n))
            return self.save(skill_name, text, user)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise

    def create(self, name: str, description: str = "", content: str | None = None, user: str | None = None) -> Skill:
        d = self._dir(name)
        if (d / "SKILL.md").exists():
            raise SkillError(f"스킬 '{name}' 이(가) 이미 있습니다.")
        text = content or SKILL_TEMPLATE.format(name=name, description=description or f"{name} 스킬 설명을 적으세요")
        return self.save(name, text, user)

    def delete(self, name: str) -> None:
        d = self._dir(name)
        if d.is_dir():
            trash = self.root / "_trash"
            trash.mkdir(exist_ok=True)
            from datetime import datetime

            shutil.move(str(d), str(trash / f"{name}_{datetime.now():%Y%m%d_%H%M%S}"))

    def history(self, name: str) -> list[str]:
        h = self._dir(name) / ".history"
        return sorted((p.name for p in h.glob("SKILL_*.md")), reverse=True) if h.is_dir() else []

    def read_file(self, name: str, rel: str, max_chars: int = 20000) -> str:
        d = self._dir(name).resolve()
        p = (d / rel).resolve()
        if d not in p.parents or not p.is_file():
            raise SkillError(f"스킬 '{name}' 에 파일 '{rel}' 이(가) 없습니다.")
        text = p.read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= max_chars else text[:max_chars] + "\n...(생략)"

    def catalog_text(self, only: list[str] | None = None) -> str:
        skills, errors = self.list()
        lines = [f"- {s.name}: {s.description}" for s in skills if only is None or s.name in only]
        lines += [f"- {n}: (SKILL.md 오류로 사용 불가: {e})" for n, e in errors.items()]
        return "\n".join(lines)
