"""명령행 인터페이스.

    inhouse-agent check                      # LLM / MCP 연결 점검
    inhouse-agent tools                      # 사용 가능한 tool 목록
    inhouse-agent chat                       # 대화형 모드
    inhouse-agent run "요청 내용"             # 1회 실행
    inhouse-agent jobs list                  # 예약작업 목록
    inhouse-agent jobs run <작업이름>         # 예약작업 즉시 1회 실행
    inhouse-agent skills list|show|new|validate|run   # 스킬 관리/테스트
    inhouse-agent files list|status|analyze|query|wait  # 파일 점검 (LLM 없이)
    inhouse-agent fetch <URL> [--cookies]    # 다운로드 URL 받기
    inhouse-agent link <경로>                # 파일 공유용 다운로드 링크
    inhouse-agent ctools list|show|test|delete  # 동적 도구 관리
    inhouse-agent -u <ID> chat [--resume 작업ID]  # 사용자별 작업폴더 / 이전 작업 이어서
    inhouse-agent -u <ID> sessions           # 저장된 작업 목록
    inhouse-agent call <도구> key=value      # 도구 직접 실행 (LLM 없이)
    inhouse-agent skills run <스킬> --mode workflow  # 스킬을 LLM 없이 steps 로 실행
    inhouse-agent scheduler                  # 예약작업 데몬
    inhouse-agent serve --port 8080          # 웹/API 서버 + 예약작업
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
import re
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigError, load_config
from .runtime import AgentRuntime, render_prompt, setup_logging


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _print_event(verbose: bool):
    def cb(kind: str, data: dict[str, Any]) -> None:
        if kind == "tool_start":
            args = json.dumps(data["arguments"], ensure_ascii=False)
            if len(args) > 200:
                args = args[:200] + "…"
            print(f"  → [{data['server']}] {data['tool']} {args}", flush=True)
        elif kind == "tool_end":
            mark = "✗" if data["is_error"] else "✓"
            line = f"  {mark} {data['tool']} ({data['elapsed']}s, {len(data['result'])}자)"
            if data["is_error"] or verbose:
                line += "\n    " + data["result"][: 2000 if verbose else 300].replace("\n", "\n    ")
            for f in data.get("files") or []:
                from .files import format_status

                line += "\n    " + format_status(f).replace("\n", "\n    ")
            print(line, flush=True)
        elif kind == "thought" and verbose:
            print(f"  · {data['content']}", flush=True)
        elif kind == "model":
            head = {"start": "모델", "escalate": "모델 승격", "fallback": "모델 변경"}.get(data.get("kind"), "모델")
            print(f"  [{head}] {data['from'] + ' → ' if data.get('from') and data.get('kind') != 'start' else ''}{data['model']} · {data['reason']}", flush=True)

    return cb


def _parse_servers(v: str | None) -> list[str] | None:
    return [s.strip() for s in v.split(",") if s.strip()] if v else None


def _warn_failed(rt: AgentRuntime) -> None:
    for name, err in rt.start_results.items():
        if err:
            print(f"[경고] MCP 서버 '{name}' 연결 실패: {err}", file=sys.stderr)


# ---------------------------------------------------------------------------


async def cmd_check(cfg: AppConfig, args: argparse.Namespace) -> int:
    ok = True
    print("== MCP 서버 ==")
    rt = AgentRuntime(cfg)
    try:
        results = await rt.start()
        for name, err in results.items():
            conn = rt.hub.connections[name]
            if err:
                ok = False
                print(f"  ✗ {name}: {err}")
            else:
                print(f"  ✓ {name}: tool {len(conn.tools)}개 ({', '.join(t.name for t in conn.tools)})")
        if not results:
            print("  (설정된 MCP 서버 없음)")
        print("== Azure OpenAI ==")
        try:
            resp = await rt.llm.chat([{"role": "user", "content": "연결 테스트입니다. 'OK' 라고만 답하세요."}])
            print(f"  ✓ deployment '{cfg.llm.deployment}' 응답: {(resp.content or '').strip()[:50]!r} usage={resp.usage}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ✗ LLM 호출 실패: {type(e).__name__}: {e}")
    finally:
        await rt.close()
    return 0 if ok else 1


async def cmd_tools(cfg: AppConfig, args: argparse.Namespace) -> int:
    async with AgentRuntime(cfg) as rt:
        for st in rt.hub.server_status():
            state = "연결됨" if st["connected"] else f"연결 실패: {st['error']}"
            print(f"[{st['name']}] ({st['transport']}) {st['description']} - {state}")
        print()
        for spec in rt.hub.openai_tools(_parse_servers(args.servers)):
            f = spec["function"]
            print(f"- {f['name']}: {f['description'][:150]}")
            if args.verbose:
                print("    " + json.dumps(f["parameters"], ensure_ascii=False))
    return 0


async def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    prompt = args.prompt if args.prompt != "-" else sys.stdin.read()
    async with AgentRuntime(cfg) as rt:
        result = await rt.agent.run(
            prompt, servers=_parse_servers(args.servers), on_event=None if args.quiet else _print_event(args.verbose)
        )
        if not args.quiet:
            print()
        print(result.output)
        _print_status(result)
        path = None
        if args.save:
            path = rt.save_result(args.save, prompt, result)
            print(f"\n[저장] {path}", file=sys.stderr)
        _print_links(rt, result, path)
    return 0 if result.success else 2


async def _cli_ask(question: str, options: list[str] | None, default: str | None) -> str:
    """ask_user → 터미널에서 질문."""
    print(f"\n🙋 에이전트 질문: {question}")
    if options:
        for i, o in enumerate(options, 1):
            print(f"   {i}) {o}")
    hint = f" [기본값: {default}]" if default else ""
    ans = (await asyncio.to_thread(input, f"답변{hint}> ")).strip()
    if options and ans.isdigit() and 1 <= int(ans) <= len(options):
        ans = options[int(ans) - 1]
    return ans or (default or "")


def _print_links(rt: AgentRuntime, result: Any, saved: Any = None) -> None:
    """결과 파일 다운로드 링크 출력 (server.public_url 이 설정된 경우)."""
    links = rt.file_links(result, saved)
    if links:
        print("\n[파일 받기]")
        for name, url in links:
            print(f"  {name}: {url}")


def _print_skill_menu(rt: AgentRuntime, show_tools: bool = False) -> None:
    """CLI: 사용할 수 있는 스킬 목록 (모든 사용자 공용)."""
    skills, errors = rt.skills.list()
    ready = rt.llm_status()[0]
    print("\n[사용할 수 있는 스킬]  /<이름> key=value 로 실행")
    for sk in skills:
        mark = "토큰 없이 가능" if sk.steps else ("LLM 필요" if not ready else "")
        ins = " ".join(f"{i.name}={i.default if i.default not in (None, '') else '?'}" for i in sk.inputs)
        print(f"  /{sk.name} {ins}  {('(' + mark + ')') if mark else ''}\n      {sk.description[:90]}")
    for n, e in errors.items():
        print(f"  /{n}  (오류: {e})")
    tools = rt.hub.openai_tools()
    if show_tools:
        print("\n[기능(도구)]")
        for t in tools:
            print(f"  /{t['function']['name']}  {t['function']['description'][:80]}")
    else:
        print(f"  … 기능(도구) {len(tools)}개는 '/ tools' 로 보기")
    print()


def _parse_inputs(items: list[str] | None) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep:
            raise SystemExit(f"입력값 형식은 key=value 입니다: {it}")
        out[k.strip()] = v
    return out


def _print_status(result: Any) -> None:
    """완료가 아니면 상태를 따로 한 줄로 (스크립트·배치에서 알아보기 쉽게, stderr)."""
    if result.stopped_reason != "completed":
        from .errors import status_label

        print(f"\n[상태] {status_label(result.stopped_reason)}" + (f" - {result.error['title']}" if result.error else "")
              + (f" ({result.detail})" if result.detail else ""), file=sys.stderr)


async def cmd_chat(cfg: AppConfig, args: argparse.Namespace) -> int:
    servers = _parse_servers(args.servers)
    async with AgentRuntime(cfg) as rt:
        _warn_failed(rt)
        print("사내 자료수집 에이전트 - 대화형 모드")
        print("/ 만 입력하면 사용할 수 있는 스킬 목록, /<스킬이름> key=value 로 바로 실행, /<도구이름> key=value 로 기능 직접 실행")
        print("명령: /tools  /servers  /files  /skills  /skill <이름> [k=v ...]  /noskill  /attach <경로> (이미지·PPT·엑셀)  /model [이름|auto]  /sessions  /reset  /save <이름>  /reconnect <서버>  /exit")
        ready, why = rt.llm_status()
        if not ready:
            print(f"[LLM 없음] {why} → 워크플로가 있는 스킬과 도구는 / 로 실행할 수 있습니다.")
        _print_skill_menu(rt)
        skill = rt.skills.get(args.skill) if getattr(args, "skill", None) else None
        skill_inputs: dict[str, str] = _parse_inputs(getattr(args, "input", None))
        messages = rt.agent.new_conversation(servers)
        last = None
        store = rt.sessions()
        sid = store.new_id()
        images: list[str] = []
        attachments: list[str] = []
        chat_model: str | None = None  # /model 로 고정 (기본: 자동 선택)
        if rt.workspace.user:
            print(f"[사용자] {rt.workspace.user} · 작업폴더 {rt.workspace.root}")
        if getattr(args, "resume", None):
            d = store.load(args.resume)
            sid, messages = d["id"], d.get("messages") or messages
            if d.get("skill") and not skill:
                skill, skill_inputs = rt.skills.get(d["skill"]), d.get("inputs") or {}
            print(f"[이어서] {d.get('title')} (대화 {len(d.get('turns', []))}회)")
            for t in d.get("turns", [])[-2:]:
                print(f"  나> {t.get('prompt')}\n  에이전트> {str(t.get('output', ''))[:300]}")
        if skill:
            print(f"[스킬 모드] {skill.name}: {skill.description}")
        while True:
            try:
                line = await asyncio.to_thread(input, "나> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                cmd, _, rest = line.partition(" ")
                if cmd in ("/exit", "/quit", "/q"):
                    break
                if cmd == "/reset":
                    messages = rt.agent.new_conversation(servers)
                    print("대화를 초기화했습니다.")
                elif cmd == "/skills":
                    print(rt.skills.catalog_text() or "(스킬 없음)")
                elif cmd == "/skill":
                    parts = rest.split()
                    if not parts:
                        print("사용법: /skill <이름> [key=value ...]")
                        continue
                    try:
                        skill = rt.skills.get(parts[0])
                        skill_inputs = _parse_inputs(parts[1:])
                    except Exception as e:  # noqa: BLE001
                        print(f"스킬 오류: {e}")
                        continue
                    messages = rt.agent.new_conversation(servers)
                    print(f"[스킬 모드] {skill.name}: {skill.description}\n  입력: {[i.name for i in skill.inputs]} · 빈 줄 대신 '실행' 이라고 입력하면 바로 실행합니다.")
                elif cmd == "/model":
                    name = rest.strip()
                    if not name:
                        print("\n".join(f"- {m['name']} ({m['tier']}){' ← 기본' if m['default'] else ''}" for m in rt.router.catalog())
                              + f"\n지금: {chat_model or 'auto (비용 기준 자동 선택)'} · 바꾸기: /model <이름> 또는 /model auto")
                    elif name == "auto" or rt.router.get(name):
                        chat_model = None if name == "auto" else name
                        print(f"모델: {name}")
                    else:
                        print(f"없는 모델입니다: {name} (있는 모델: {', '.join(s.name for s in rt.router.specs)})")
                elif cmd in ("/image", "/attach"):
                    from .images import is_image

                    try:
                        ip = rt.workspace.resolve(rest.strip().strip('"'))
                    except PermissionError as e:
                        print(f"오류: {e}")
                        continue
                    if not ip.is_file():
                        print(f"파일이 없습니다: {ip}")
                    elif is_image(ip):
                        images.append(str(ip))
                        print(f"다음 요청에 이미지 첨부: {ip.name} (이미지 {len(images)}장, 문서 {len(attachments)}개)")
                    else:
                        attachments.append(str(ip))
                        print(f"다음 요청에 문서 첨부: {ip.name} (이미지 {len(images)}장, 문서 {len(attachments)}개)"
                              + (" · 2개 이상이면 비교 분석합니다" if len(attachments) >= 2 else ""))
                elif cmd == "/sessions":
                    for it in store.list(15):
                        print(f"- {it['id']}  {it['title']}  ({it['turns']}회)")
                    print("이어서 하려면: inhouse-agent chat --resume <ID>")
                elif cmd == "/noskill":
                    skill = None
                    messages = rt.agent.new_conversation(servers)
                    print("스킬 모드를 해제했습니다.")
                elif cmd == "/files":
                    for p in rt.workspace.list_files(rest.strip() or "*")[:30]:
                        print(f"- {rt.workspace.display(p)} ({p.stat().st_size:,} bytes)")
                elif cmd == "/tools":
                    for spec in rt.hub.openai_tools(servers):
                        print(f"- {spec['function']['name']}")
                elif cmd == "/servers":
                    for st in rt.hub.server_status():
                        print(f"- {st['name']}: {'연결됨' if st['connected'] else '끊김 ' + str(st['error'])} tool {len(st['tools'])}개")
                elif cmd == "/reconnect" and rest:
                    try:
                        await rt.hub.reconnect(rest.strip())
                        print("재연결 완료")
                    except Exception as e:  # noqa: BLE001
                        print(f"재연결 실패: {e}")
                elif cmd == "/save":
                    if last is None:
                        print("저장할 결과가 없습니다.")
                    else:
                        print(f"저장: {rt.save_result(rest.strip() or 'chat', last[0], last[1])}")
                elif cmd in ("/", "/?"):
                    _print_skill_menu(rt, show_tools=bool(rest.strip() == "tools"))
                elif rt.skills.root.joinpath(cmd[1:], "SKILL.md").is_file():  # /<스킬이름> k=v
                    try:
                        res, path = await rt.run_skill(cmd[1:], _parse_inputs(rest.split()), "", on_event=_print_event(args.verbose), ask=_cli_ask)
                    except Exception as e:  # noqa: BLE001
                        print(f"오류: {e}")
                        continue
                    print(f"\n에이전트> {res.output}\n[저장] {path}\n")
                    last = (line, res)
                elif rt.hub.route_of(cmd[1:]) is not None:  # /<도구이름> k=v (LLM 없이)
                    vals: dict[str, Any] = {}
                    for k, v in _parse_inputs(rest.split()).items():
                        try:
                            vals[k] = json.loads(v)
                        except ValueError:
                            vals[k] = v
                    out = await rt.hub.call(cmd[1:], vals)
                    print(out.text + "\n")
                else:
                    print("알 수 없는 명령 - / 를 입력하면 스킬 목록이 나옵니다.")
                continue
            if not rt.llm_status()[0] and attachments and not skill:  # 토큰 없이 첨부 문서 자동 분석/비교
                from .compare import attachment_steps

                res = await rt.run_workflow(attachment_steps(attachments), {}, title="첨부 문서 분석", on_event=_print_event(args.verbose), ask=_cli_ask)
                attachments = []
                print(f"\n에이전트> {res.output}\n")
                continue
            if not rt.llm_status()[0] and not (skill and skill.steps and line == "실행"):
                print("LLM 이 연결되어 있지 않아 자유 질문에는 답할 수 없습니다. / 를 입력하면 실행할 수 있는 스킬이 나옵니다.\n")
                continue
            if skill and skill.steps and not rt.llm_status()[0]:  # 스킬 모드 + LLM 없음 → 워크플로
                res, path = await rt.run_skill(skill.name, skill_inputs, "", on_event=_print_event(args.verbose), ask=_cli_ask)
                print(f"\n에이전트> {res.output}\n[저장] {path}\n")
                continue
            try:
                result = await rt.agent.run(
                    "" if (skill and line == "실행") else line,
                    messages=messages, servers=servers, on_event=_print_event(args.verbose), ask=_cli_ask,
                    skill=skill, skill_inputs=skill_inputs, images=images or None, attachments=attachments or None, model=chat_model,
                )
            except Exception as e:  # noqa: BLE001 - 스킬 입력 오류 등
                print(f"오류: {e}")
                continue
            store.save(sid, messages, skill=skill.name if skill else None, inputs=skill_inputs or None, servers=servers, turn={
                "time": time.time(), "prompt": line, "images": images, "attachments": attachments, "kind": "skill" if skill else "chat",
                "skill": skill.name if skill else None, "usage": result.usage, "elapsed": round(result.elapsed, 1),
                "status": result.stopped_reason, "output": result.output,
                "tools": [f"{e.server}.{e.tool}" for e in result.tool_events], "files": [f["path"] for f in result.files if f.get("path")],
            })
            images, attachments = [], []
            last = (line, result)
            print(f"\n에이전트> {result.output}")
            _print_links(rt, result)
            print()
    return 0


async def cmd_jobs(cfg: AppConfig, args: argparse.Namespace) -> int:
    if args.jobs_cmd == "list":
        if not cfg.schedules:
            print("(등록된 예약작업 없음)")
        from .scheduler import make_trigger

        for s in cfg.schedules:
            trig = make_trigger(s, cfg.agent.timezone)
            from datetime import datetime
            from zoneinfo import ZoneInfo

            nxt = trig.get_next_fire_time(None, datetime.now(ZoneInfo(s.timezone or cfg.agent.timezone)))
            state = "사용" if s.enabled else "중지"
            print(f"- {s.name} [{state}] cron='{s.cron}' 다음실행={nxt:%Y-%m-%d %H:%M} 서버={s.servers or '전체'}")
            if s.description:
                print(f"    {s.description}")
            if args.verbose:
                print("    프롬프트: " + render_prompt(s.prompt, s.timezone or cfg.agent.timezone).replace("\n", "\n    "))
        return 0

    job = cfg.get_schedule(args.name)
    async with AgentRuntime(cfg) as rt:
        result, path = await rt.run_job(job, on_event=_print_event(args.verbose))
        print()
        print(result.output)
        _print_status(result)
        print(f"\n[저장] {path}")
        _print_links(rt, result, path)
    return 0 if result.success else 2


async def cmd_skills(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .skills import SkillStore

    store = SkillStore(cfg.skills_path)
    sc = args.skills_cmd
    if sc in ("import", "update"):
        from .skill_install import SkillInstaller
        from .skills import SkillError

        inst = SkillInstaller(store, cfg.raw.get("skills") or {})
        try:
            if sc == "update":
                names = [s.name for s in store.list()[0] if (s.info.get("source") or {}).get("type") in ("git", "folder")] if args.all else args.names
                for n in names:
                    r = inst.update(n)
                    print(f"- {n}: " + ("갱신함" if r.get("updated") else r.get("reason", "변경 없음")) + (f" ({str(r.get('commit'))[:8]})" if r.get("commit") else ""))
                return 0
            src = args.source
            if re.match(r"^(https?://|git@|ssh://)", src):
                sid = inst.stage_git(src, args.ref, args.path)
            elif Path(src).is_dir():
                sid = inst.stage_folder(src)
            elif src.lower().endswith(".md"):
                sid = inst.stage_text(Path(src).read_text(encoding="utf-8-sig"))
            else:
                sid = inst.stage_zip(Path(src).read_bytes(), Path(src).name)
            found = inst.scan(sid)
            for f in found:
                print(f"- {f.name}{' (이미 있음)' if f.exists else ''}{' ⚠ ' + f.error if f.error else ''}: {f.description[:70]}  [{f.key}]")
            if args.list:
                inst.drop(sid)
                return 0
            res = inst.install(sid, args.only.split(",") if args.only else None, overwrite=args.overwrite)
            inst.drop(sid)
        except SkillError as e:
            print(f"오류: {e}")
            return 1
        print(f"설치: {', '.join(x['name'] for x in res['installed']) or '-'}")
        if res["skipped"]:
            print(f"건너뜀(이미 있음, --overwrite 로 덮어쓰기): {', '.join(res['skipped'])}")
        for k, v in res["errors"].items():
            print(f"오류 {k}: {v}")
        return 0 if res["installed"] or not res["errors"] else 1
    if sc == "list":
        skills, errors = store.list()
        for s in skills:
            ins = ", ".join(f"{i.name}{'*' if i.required else ''}" for i in s.inputs)
            print(f"- {s.name}: {s.description}" + (f"  [입력: {ins}]" if ins else "") + (f"  [서버: {', '.join(s.servers)}]" if s.servers else ""))
        for n, e in errors.items():
            print(f"- {n}: ⚠ 오류 - {e}")
        if not skills and not errors:
            print(f"(스킬 없음) {store.root}")
        return 0
    if sc == "show":
        sk = store.get(args.name)
        print(sk.raw)
        if sk.files:
            print("\n추가 파일:", ", ".join(sk.files))
        return 0
    if sc == "new":
        sk = store.create(args.name, args.description or "")
        print(f"생성: {sk.path / 'SKILL.md'}")
        return 0
    if sc == "validate":
        from .skills import parse_skill

        target = store.root / args.name / "SKILL.md"
        try:
            sk = parse_skill(target.read_text(encoding="utf-8"), target.parent)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {e}")
            return 1
        print(f"✓ {sk.name} - 입력 {len(sk.inputs)}개, 서버 {sk.servers or '전체'}")
        print("\n--- 치환된 지침 미리보기 ---")
        print(sk.render(sk.resolve_inputs(_parse_inputs(args.input), cfg.agent.timezone), cfg.agent.timezone))
        return 0
    # run
    async with AgentRuntime(cfg) as rt:
        _warn_failed(rt)
        result, path = await rt.run_skill(
            args.name, _parse_inputs(args.input), args.prompt or "",
            on_event=_print_event(args.verbose), ask=None if args.no_ask else _cli_ask, mode=args.mode,
        )
        print()
        print(result.output)
        _print_status(result)
        print(f"\n[저장] {path}")
        _print_links(rt, result, path)
    return 0 if result.success else 2


async def cmd_files(cfg: AppConfig, args: argparse.Namespace) -> int:
    """LLM 없이 파일 상태 점검/분석 (MCP 다운로드 결과 확인용)."""
    from . import analyzers
    from .files import FileWorkspace, file_status, format_status, wait_for_download

    ws = FileWorkspace(
        cfg.workspace_path,
        watch_dirs=[cfg.resolve_path(d) for d in cfg.files.watch_dirs],
        extra_read_dirs=[cfg.output_path] + [cfg.resolve_path(d) for d in cfg.files.extra_read_dirs],
        max_file_mb=cfg.files.max_file_mb,
    )
    fc = args.files_cmd
    if fc == "list":
        print("감시 폴더:", ", ".join(str(d) for d in ws.watch_dirs))
        for p in ws.list_files(args.pattern, args.recent):
            print(f"- {ws.display(p)}\t{p.stat().st_size:,} bytes")
        return 0
    if fc == "wait":
        st = await wait_for_download(ws, args.pattern, args.timeout or cfg.files.wait_timeout)
        print(format_status(st))
        return 0 if st["state"] == "complete" else 1
    p = ws.resolve(args.path)
    st = await file_status(p, ws)
    print(format_status(st))
    if fc == "status":
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0 if st["state"] == "complete" else 1
    if fc == "analyze":
        if st["state"] in ("missing", "downloading", "empty"):
            return 1
        print()
        print(await asyncio.to_thread(analyzers.analyze, p, args.sheet))
        return 0
    if fc == "query":
        print(await asyncio.to_thread(
            analyzers.query_table, p, args.sheet, None, json.loads(args.filters) if args.filters else None,
            args.group_by.split(",") if args.group_by else None,
            [{"column": c.split(":")[1], "func": c.split(":")[0]} if ":" in c else {"func": c} for c in (args.agg or "").split(",") if c] or None,
            None, False, args.limit, 0,
        ))
        return 0
    return 1


async def cmd_ctools(cfg: AppConfig, args: argparse.Namespace) -> int:
    """동적 도구 관리 (LLM 없이)."""
    async with AgentRuntime(cfg) as rt:
        dyn = rt.dynamic
        c = args.ctools_cmd
        if c == "list":
            tools, errors = dyn.list()
            print(f"승인 모드: {dyn.cfg.mode} · 폴더: {dyn.root}")
            for t in tools:
                lt = t.meta.get("last_test") or {}
                print(f"- custom__{t.name}: {t.description}  [작성: {t.meta.get('created_by')}, 테스트: {'성공' if lt.get('ok') else ('실패' if lt else '-')}]")
            for n, e in errors.items():
                print(f"- {n}: ⚠ {e}")
            if not tools and not errors:
                print("(동적 도구 없음)")
            return 0
        if c == "show":
            print(dyn.load(args.name).path.read_text(encoding="utf-8"))
            return 0
        if c == "test":
            t = dyn.load(args.name)
            r = await dyn.execute("tool", t.path, json.loads(args.args or "{}"))
            print(dyn.format_result(r))
            return 0 if r["ok"] else 2
        if c == "delete":
            dyn.delete(args.name)
            print(f"삭제(→ {dyn.root / '.trash'}): {args.name}")
            return 0
    return 1


async def cmd_link(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .files import FileWorkspace
    from .links import LinkSigner
    from .toolkit import ToolkitConfig

    tk = ToolkitConfig.from_raw(cfg.raw)
    ws = FileWorkspace(cfg.workspace_path, watch_dirs=[cfg.resolve_path(d) for d in cfg.files.watch_dirs],
                       extra_read_dirs=[cfg.output_path] + [cfg.resolve_path(d) for d in cfg.files.extra_read_dirs])
    info = LinkSigner(ws, tk.public_url, tk.link_expire_hours).describe(args.path, args.hours)
    print(info["url"])
    if not info["absolute"]:
        print("※ server.public_url 이 없어 상대 링크입니다 (예: http://agent-server:8080 을 설정하세요).", file=sys.stderr)
    return 0


async def cmd_fetch(cfg: AppConfig, args: argparse.Namespace) -> int:
    """다운로드 URL 직접 받기 + 상태 점검."""
    async with AgentRuntime(cfg) as rt:
        a: dict[str, Any] = {"url": args.url}
        if args.cookies:
            a["cookies"] = args.cookies
        if args.filename:
            a["filename"] = args.filename
        out = await rt.hub.call("download_url", a)
        print(out.text)
        return 1 if out.is_error else 0


async def cmd_call(cfg: AppConfig, args: argparse.Namespace) -> int:
    """도구(기능) 직접 실행 - LLM/토큰 불필요. 값은 key=value (JSON 도 가능: columns='["a","b"]')."""
    vals: dict[str, Any] = {}
    for k, v in _parse_inputs(args.args).items():
        try:
            vals[k] = json.loads(v)
        except ValueError:
            vals[k] = v
    async with AgentRuntime(cfg) as rt:
        if args.tool in ("list", "ls"):
            for t in rt.hub.openai_tools():
                print(f"- {t['function']['name']}: {t['function']['description'][:100]}")
            return 0
        if rt.hub.route_of(args.tool) is None:
            print(f"없는 도구입니다: {args.tool} (inhouse-agent call list 로 목록 확인)")
            return 1
        out = await rt.hub.call(args.tool, vals)
        print(out.text)
        return 1 if out.is_error else 0


async def cmd_sessions(cfg: AppConfig, args: argparse.Namespace) -> int:
    from datetime import datetime

    rt = AgentRuntime(cfg)
    items = rt.sessions().list(200)
    if args.query:
        items = [i for i in items if args.query.lower() in i["title"].lower()]
    who = rt.workspace.user or "(공용)"
    print(f"사용자 {who} · 작업 {len(items)}개 · {rt.workspace.root / 'sessions'}")
    for i in items:
        print(f"- {i['id']}  {datetime.fromtimestamp(i['updated'] or 0):%Y-%m-%d %H:%M}  {i['title']}  ({i['turns']}회)")
    if items:
        print("\n이어서 하려면: inhouse-agent" + (f" -u {rt.workspace.user}" if rt.workspace.user else "") + " chat --resume <ID>")
    return 0


async def cmd_scheduler(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .scheduler import AgentScheduler

    async with AgentRuntime(cfg) as rt:
        _warn_failed(rt)
        sched = AgentScheduler(rt)
        jobs = sched.register_all()
        if not jobs:
            print("활성화된 예약작업이 없습니다. config.yaml 의 schedules 를 확인하세요.")
            return 1
        sched.start()
        for j in sched.jobs_info():
            if j["enabled"]:
                print(f"- {j['name']} cron='{j['cron']}' 다음실행={j['next_run']}")
        print("예약작업 대기 중... (종료: Ctrl+C)", flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            sched.shutdown()
    return 0


def lan_addresses() -> list[str]:
    """이 PC 의 사내망 IP (다른 사람이 접속할 주소)."""
    import socket

    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 실제로 보내지 않음 - 기본 경로의 내 IP 확인용
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in ips:
                ips.append(info[4][0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def cmd_serve(cfg: AppConfig, args: argparse.Namespace) -> int:
    import socket

    import uvicorn

    from .server import create_app

    ips = lan_addresses() if args.host in ("0.0.0.0", "") else [args.host]
    urls = [f"http://{ip}:{args.port}" for ip in ips]
    server_cfg = cfg.raw.setdefault("server", {}) if isinstance(cfg.raw, dict) else {}
    if urls and not str(server_cfg.get("public_url") or "").strip():
        server_cfg["public_url"] = urls[0]  # 메일/메신저 공유 링크도 다른 PC 에서 열리도록
    name = socket.gethostname()
    print("=" * 64)
    print(" 사내 자료수집 에이전트 서버")
    print(f"  이 PC 에서:      http://localhost:{args.port}")
    for u in urls:
        print(f"  다른 PC 에서:    {u}")
    print(f"  (PC 이름으로:    http://{name}:{args.port})")
    print("  사용자는 접속 후 오른쪽 위 👤 에 자기 ID 를 입력하면 각자 작업폴더에서 씁니다.")
    if args.host in ("127.0.0.1", "localhost"):
        print("  ※ --host 127.0.0.1 이라 이 PC 에서만 접속됩니다. 다른 사람에게 열려면 --host 0.0.0.0")
    print("  다른 PC 에서 안 열리면 Windows 방화벽에서 이 포트를 허용하세요: scripts\\windows_firewall.bat")
    if not (cfg.raw.get("skills") or {}).get("editors"):
        print("  ⚠ skills.editors 가 비어 있어 접속한 모든 사람이 스킬 편집·코드 실행(run_python, 도구 만들기)을 할 수 있습니다.")
        print("    여러 사람에게 서비스하면 config.yaml 의 skills.editors 에 담당자 ID 를 적으세요.")
    print("  종료: Ctrl+C")
    print("=" * 64, flush=True)
    app = create_app(cfg, enable_scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inhouse-agent", description="사내 사이트 MCP 자료수집 에이전트")
    p.add_argument("-c", "--config", help="설정 파일 경로 (기본: ./config/config.yaml)")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("-u", "--user", help="사용자 ID (사용자별 작업폴더 workspace/users/<ID> 에서 실행)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Azure OpenAI / MCP 서버 연결 점검")

    t = sub.add_parser("tools", help="tool 목록")
    t.add_argument("-s", "--servers", help="쉼표로 구분한 MCP 서버 이름")
    t.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("chat", help="대화형 모드")
    c.add_argument("-s", "--servers", help="사용할 MCP 서버 제한 (쉼표 구분)")
    c.add_argument("--skill", help="스킬 모드로 시작")
    c.add_argument("--resume", metavar="작업ID", help="저장된 작업(대화)을 이어서 (inhouse-agent sessions 로 확인)")
    c.add_argument("-i", "--input", action="append", help="스킬 입력값 key=value (여러 번)")
    c.add_argument("-v", "--verbose", action="store_true")

    r = sub.add_parser("run", help="요청 1회 실행")
    r.add_argument("prompt", help="요청 내용 ('-' 이면 표준입력)")
    r.add_argument("-s", "--servers", help="사용할 MCP 서버 제한 (쉼표 구분)")
    r.add_argument("--save", metavar="NAME", help="결과를 outputs/NAME/ 에 저장")
    r.add_argument("-q", "--quiet", action="store_true", help="진행 로그 없이 결과만 출력")
    r.add_argument("-v", "--verbose", action="store_true")

    j = sub.add_parser("jobs", help="예약작업")
    jsub = j.add_subparsers(dest="jobs_cmd", required=True)
    jl = jsub.add_parser("list")
    jl.add_argument("-v", "--verbose", action="store_true")
    jr = jsub.add_parser("run")
    jr.add_argument("name")
    jr.add_argument("-v", "--verbose", action="store_true")

    sk = sub.add_parser("skills", help="스킬 관리/테스트")
    sks = sk.add_subparsers(dest="skills_cmd", required=True)
    sks.add_parser("list")
    x = sks.add_parser("show")
    x.add_argument("name")
    x = sks.add_parser("new")
    x.add_argument("name")
    x.add_argument("-d", "--description")
    x = sks.add_parser("import", help="스킬 가져오기: zip / GitHub·Git 주소 / 폴더 / SKILL.md (안의 스킬 여러 개 모두)")
    x.add_argument("source", help="예) https://github.com/org/repo  ·  skills.zip  ·  C:/work/skill_repo  ·  SKILL.md")
    x.add_argument("--only", help="이 스킬만 (쉼표로 여러 개)")
    x.add_argument("--overwrite", action="store_true", help="이미 있는 스킬도 덮어쓰기 (data/ 와 DB 파일은 유지)")
    x.add_argument("--ref", help="브랜치/태그 (Git)")
    x.add_argument("--path", help="저장소 안 하위 폴더 (Git)")
    x.add_argument("--list", action="store_true", help="설치하지 않고 들어 있는 스킬만 보기")
    x = sks.add_parser("update", help="Git/폴더에서 가져온 스킬을 원본에서 갱신")
    x.add_argument("names", nargs="*")
    x.add_argument("--all", action="store_true")
    x = sks.add_parser("validate", help="형식 검사 + 치환된 지침 미리보기")
    x.add_argument("name")
    x.add_argument("-i", "--input", action="append")
    x = sks.add_parser("run", help="스킬 테스트 실행")
    x.add_argument("name")
    x.add_argument("-i", "--input", action="append", help="입력값 key=value (여러 번)")
    x.add_argument("-p", "--prompt", help="추가 지시")
    x.add_argument("--no-ask", action="store_true", help="ask_user 질문 없이 기본값으로 진행")
    x.add_argument("--mode", choices=["auto", "agent", "workflow"], default="auto", help="workflow: LLM 없이 steps 로 실행")
    x.add_argument("-v", "--verbose", action="store_true")

    fl = sub.add_parser("files", help="파일 상태 점검/분석 (LLM 없이)")
    fls = fl.add_subparsers(dest="files_cmd", required=True)
    x = fls.add_parser("list")
    x.add_argument("pattern", nargs="?", default="*")
    x.add_argument("--recent", type=float, help="최근 N분")
    x = fls.add_parser("status")
    x.add_argument("path")
    x = fls.add_parser("analyze")
    x.add_argument("path")
    x.add_argument("--sheet")
    x = fls.add_parser("query")
    x.add_argument("path")
    x.add_argument("--sheet")
    x.add_argument("--filters", help='JSON 예: [{"column":"부서","op":"==","value":"인사팀"}]')
    x.add_argument("--group-by", help="쉼표 구분 열")
    x.add_argument("--agg", help="예: count,sum:금액,avg:금액")
    x.add_argument("--limit", type=int, default=50)
    x = fls.add_parser("wait", help="다운로드 완료 대기")
    x.add_argument("pattern", nargs="?", default="*")
    x.add_argument("--timeout", type=float, help="최대 대기 초 (기본: files.wait_timeout = 300)")

    ct = sub.add_parser("ctools", help="동적 도구(에이전트가 만든 tool) 관리")
    cts = ct.add_subparsers(dest="ctools_cmd", required=True)
    cts.add_parser("list")
    x = cts.add_parser("show")
    x.add_argument("name")
    x = cts.add_parser("test")
    x.add_argument("name")
    x.add_argument("--args", help='JSON 예: {"months":["2026-10"]}')
    x = cts.add_parser("delete")
    x.add_argument("name")

    x = sub.add_parser("link", help="파일 공유용 다운로드 링크 만들기 (server.public_url 필요)")
    x.add_argument("path")
    x.add_argument("--hours", type=float, help="유효 시간")

    x = sub.add_parser("fetch", help="다운로드 URL 받기 + 상태 점검")
    x.add_argument("url")
    x.add_argument("--cookies", help="'A=1; B=2'")
    x.add_argument("--filename")

    x = sub.add_parser("call", help="도구(기능) 직접 실행 - LLM 없이 (예: call analyze_file path=downloads/a.xlsx)")
    x.add_argument("tool", help="도구 이름 (list: 목록)")
    x.add_argument("args", nargs="*", help="key=value")

    x = sub.add_parser("sessions", help="저장된 작업(대화) 목록")
    x.add_argument("-q", "--query")

    sub.add_parser("scheduler", help="예약작업 데몬 실행")

    s = sub.add_parser("serve", help="웹/API 서버 (+예약작업)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--no-scheduler", action="store_true", help="예약작업은 실행하지 않음")
    return p


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 1
    setup_logging(cfg, args.log_level, console=args.cmd not in ("chat", "skills", "ctools", "fetch", "link", "sessions", "call"))
    if args.user:
        from .users import UserError, current_user, normalize_user

        try:
            current_user.set(normalize_user(args.user))  # asyncio.run 안의 작업까지 전달됨
        except UserError as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1

    if args.cmd == "serve":
        return cmd_serve(cfg, args)
    handlers = {
        "check": cmd_check,
        "tools": cmd_tools,
        "chat": cmd_chat,
        "run": cmd_run,
        "jobs": cmd_jobs,
        "skills": cmd_skills,
        "files": cmd_files,
        "ctools": cmd_ctools,
        "fetch": cmd_fetch,
        "link": cmd_link,
        "sessions": cmd_sessions,
        "call": cmd_call,
        "scheduler": cmd_scheduler,
    }
    try:
        return asyncio.run(handlers[args.cmd](cfg, args))
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 1
    except (ValueError, PermissionError) as e:  # SkillError, FileAccessError 등
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
