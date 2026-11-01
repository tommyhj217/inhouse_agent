"""명령행 인터페이스.

    inhouse-agent check                      # LLM / MCP 연결 점검
    inhouse-agent tools                      # 사용 가능한 tool 목록
    inhouse-agent chat                       # 대화형 모드
    inhouse-agent run "요청 내용"             # 1회 실행
    inhouse-agent jobs list                  # 예약작업 목록
    inhouse-agent jobs run <작업이름>         # 예약작업 즉시 1회 실행
    inhouse-agent scheduler                  # 예약작업 데몬
    inhouse-agent serve --port 8080          # 웹/API 서버 + 예약작업
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
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
            print(line, flush=True)
        elif kind == "thought" and verbose:
            print(f"  · {data['content']}", flush=True)

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
        if args.save:
            path = rt.save_result(args.save, prompt, result)
            print(f"\n[저장] {path}", file=sys.stderr)
    return 0 if result.success else 2


async def cmd_chat(cfg: AppConfig, args: argparse.Namespace) -> int:
    servers = _parse_servers(args.servers)
    async with AgentRuntime(cfg) as rt:
        _warn_failed(rt)
        print("사내 자료수집 에이전트 - 대화형 모드")
        print("명령: /tools  /servers  /reset  /save <이름>  /reconnect <서버>  /exit\n")
        messages = rt.agent.new_conversation(servers)
        last = None
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
                else:
                    print("알 수 없는 명령")
                continue
            result = await rt.agent.run(line, messages=messages, servers=servers, on_event=_print_event(args.verbose))
            last = (line, result)
            print(f"\n에이전트> {result.output}\n")
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
        print(f"\n[저장] {path}")
    return 0 if result.success else 2


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


def cmd_serve(cfg: AppConfig, args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    app = create_app(cfg, enable_scheduler=not args.no_scheduler)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inhouse-agent", description="사내 사이트 MCP 자료수집 에이전트")
    p.add_argument("-c", "--config", help="설정 파일 경로 (기본: ./config/config.yaml)")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="Azure OpenAI / MCP 서버 연결 점검")

    t = sub.add_parser("tools", help="tool 목록")
    t.add_argument("-s", "--servers", help="쉼표로 구분한 MCP 서버 이름")
    t.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("chat", help="대화형 모드")
    c.add_argument("-s", "--servers", help="사용할 MCP 서버 제한 (쉼표 구분)")
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
    setup_logging(cfg, args.log_level, console=args.cmd not in ("chat",))

    if args.cmd == "serve":
        return cmd_serve(cfg, args)
    handlers = {
        "check": cmd_check,
        "tools": cmd_tools,
        "chat": cmd_chat,
        "run": cmd_run,
        "jobs": cmd_jobs,
        "scheduler": cmd_scheduler,
    }
    try:
        return asyncio.run(handlers[args.cmd](cfg, args))
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
