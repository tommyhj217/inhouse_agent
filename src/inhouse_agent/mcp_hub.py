"""여러 MCP 서버에 동시에 연결하고, 각 서버의 tool 을 LLM function 으로 노출하는 허브.

- mcp 파이썬 SDK 1.x / 2.x 모두 지원합니다.
- 서버마다 전용 asyncio Task 가 연결(context manager)을 소유하므로, 서버가 죽어도
  해당 서버만 재연결합니다.
- 웹 컨트롤(브라우저 자동화) 서버는 동시 호출에 취약하므로 서버 단위로 호출을 직렬화합니다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import MCPServerConfig

log = logging.getLogger(__name__)

NAME_SEP = "__"
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------


def _dump(obj: Any) -> Any:
    """pydantic 모델 → camelCase(alias) dict. (SDK 1.x/2.x 필드명 차이 흡수)"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, exclude_none=True, mode="json")
    return obj


def make_function_name(server: str, tool: str, used: set[str]) -> str:
    """OpenAI function 이름 규칙(^[a-zA-Z0-9_-]{1,64}$)에 맞는 고유 이름 생성."""
    base = _NAME_OK.sub("_", f"{server}{NAME_SEP}{tool}")
    name = base[:64]
    if name in used or len(base) > 64:
        digest = hashlib.sha1(f"{server}/{tool}".encode()).hexdigest()[:8]
        name = f"{base[:55]}_{digest}"
    used.add(name)
    return name


def normalize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    schema = dict(schema or {})
    schema.pop("$schema", None)
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": schema.get("properties", {}), **{k: v for k, v in schema.items() if k not in ("type", "properties")}}
    schema.setdefault("properties", {})
    return schema


def describe_exception(e: BaseException) -> str:
    """ExceptionGroup(anyio TaskGroup) 안의 실제 원인을 꺼내 사람이 읽을 수 있게."""
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


BlobSaver = Callable[[bytes, str, "str | None"], Path]


def tool_result_data(data: dict[str, Any]) -> Any:
    """결과에서 구조화 데이터 추출: structuredContent → 텍스트 항목 JSON 파싱(여러 개면 리스트)."""
    sc = data.get("structuredContent")
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    texts = [i.get("text", "") for i in data.get("content") or [] if i.get("type") == "text"]
    if not texts:
        return None
    parsed = []
    for t in texts:
        try:
            parsed.append(json.loads(t))
        except (json.JSONDecodeError, TypeError):
            return None
    return parsed[0] if len(parsed) == 1 else parsed


def tool_result_to_text(result: Any, saver: BlobSaver | None = None) -> tuple[str, bool, list[Path]]:
    """CallToolResult → (LLM 에 줄 텍스트, is_error, 저장한 파일 목록).

    바이너리(EmbeddedResource blob / image / audio)는 saver 가 있으면 파일로 저장하고 경로를 알려줍니다.
    """
    data = _dump(result)
    if not isinstance(data, dict):
        return str(data), False, []
    is_error = bool(data.get("isError"))
    parts: list[str] = []
    saved: list[Path] = []

    def _save(b64: str, name: str, mime: str | None) -> str:
        if saver is None:
            return "텍스트로 전달되지 않음"
        try:
            from .files import decode_b64

            p = saver(decode_b64(b64), name, mime)
            saved.append(p)
            return f"파일로 저장됨: {p}"
        except Exception as e:  # noqa: BLE001
            return f"저장 실패: {e}"

    for item in data.get("content") or []:
        typ = item.get("type")
        if typ == "text":
            parts.append(item.get("text", ""))
        elif typ in ("image", "audio"):
            size = len(item.get("data", "")) * 3 // 4
            mime = item.get("mimeType", "")
            note = _save(item.get("data", ""), f"{typ}.{(mime.split('/')[-1] or 'bin')}", mime)
            parts.append(f"[{'이미지' if typ == 'image' else '오디오'} {mime} 약 {size:,} bytes - {note}]")
        elif typ == "resource":
            res = item.get("resource") or {}
            if "text" in res:
                parts.append(f"[리소스 {res.get('uri', '')}]\n{res['text']}")
            elif res.get("blob"):
                uri = str(res.get("uri", ""))
                import urllib.parse

                # SDK 버전에 따라 URI 의 한글이 %인코딩되어 오므로 원래 이름으로 복원
                name = urllib.parse.unquote(uri.split("?")[0].rstrip("/").split("/")[-1]) or "download"
                note = _save(res["blob"], name, res.get("mimeType"))
                parts.append(f"[바이너리 리소스 {uri} {res.get('mimeType', '')} - {note}]")
            else:
                parts.append(f"[바이너리 리소스 {res.get('uri', '')} {res.get('mimeType', '')}]")
        elif typ == "resource_link":
            parts.append(f"[리소스 링크] {item.get('name', '')} {item.get('uri', '')}")
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    structured = data.get("structuredContent")
    if structured is not None and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
    text = "\n".join(p for p in parts if p) or ("(빈 결과)" if not is_error else "(오류 - 상세 내용 없음)")
    return text, is_error, saved


# ---------------------------------------------------------------------------
# 트랜스포트 (SDK 버전 호환)
# ---------------------------------------------------------------------------


async def _open_transport(stack: AsyncExitStack, cfg: MCPServerConfig, errlog: Any) -> tuple[Any, Any]:
    if cfg.transport == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=cfg.command or "",
            args=cfg.args,
            env=cfg.env or None,
            cwd=cfg.cwd,
        )
        streams = await stack.enter_async_context(stdio_client(params, errlog=errlog))
        return streams[0], streams[1]

    if cfg.transport == "sse":
        from mcp.client.sse import sse_client

        streams = await stack.enter_async_context(
            sse_client(cfg.url or "", headers=cfg.headers or None, sse_read_timeout=cfg.tool_timeout + 60)
        )
        return streams[0], streams[1]

    # streamable http
    try:  # mcp >= 2
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http_client = create_mcp_http_client(headers=cfg.headers or None)
        await stack.enter_async_context(http_client)
        streams = await stack.enter_async_context(streamable_http_client(cfg.url or "", http_client=http_client))
    except ImportError:  # mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client

        streams = await stack.enter_async_context(
            streamablehttp_client(cfg.url or "", headers=cfg.headers or None, sse_read_timeout=cfg.tool_timeout + 60)
        )
    return streams[0], streams[1]


# ---------------------------------------------------------------------------
# 서버 연결
# ---------------------------------------------------------------------------


@dataclass
class ToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    function_name: str = ""


class ServerConnection:
    def __init__(self, cfg: MCPServerConfig, log_dir: Path | None = None):
        self.cfg = cfg
        self.log_dir = log_dir
        self.session: Any = None
        self.tools: list[ToolInfo] = []
        self.instructions: str = ""
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()  # 서버 단위 호출 직렬화
        self._conn_lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_data: Any = None
        from .remote_files import RemoteFilesConfig, is_remote

        self.remote_files = RemoteFilesConfig.from_dict(cfg.remote_files)
        self.is_remote = is_remote(cfg)

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def connected(self) -> bool:
        return self.session is not None and self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._conn_lock:
            if self.connected:
                return
            await self._start_locked()

    async def _start_locked(self) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._runner(ready, self._stop), name=f"mcp:{self.name}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=self.cfg.connect_timeout)
        except BaseException as e:
            self._stop.set()
            if not isinstance(e, asyncio.TimeoutError):
                self.last_error = describe_exception(e)
                raise RuntimeError(self.last_error) from e
            self.last_error = f"연결 시간 초과({self.cfg.connect_timeout}s)"
            self._task.cancel()
            raise TimeoutError(f"MCP 서버 '{self.name}' {self.last_error}") from None
        await self._refresh_tools()
        self.last_error = None
        log.info("MCP 서버 '%s' 연결됨 (tool %d개)", self.name, len(self.tools))

    async def _runner(self, ready: asyncio.Future[None], stop: asyncio.Event) -> None:
        from mcp import ClientSession

        errlog: Any = None
        try:
            async with AsyncExitStack() as stack:
                if self.cfg.transport == "stdio":
                    if self.log_dir is not None:
                        self.log_dir.mkdir(parents=True, exist_ok=True)
                        errlog = open(self.log_dir / f"mcp_{self.name}.log", "a", encoding="utf-8")  # noqa: SIM115
                        stack.callback(errlog.close)
                    else:
                        import sys

                        errlog = sys.stderr
                read, write = await _open_transport(stack, self.cfg, errlog)
                session = await stack.enter_async_context(ClientSession(read, write))
                init = await session.initialize()
                self.instructions = getattr(init, "instructions", None) or ""
                self.session = session
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except BaseException as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
            elif not isinstance(e, asyncio.CancelledError):
                self.last_error = describe_exception(e)
                log.warning("MCP 서버 '%s' 연결이 끊어졌습니다: %s", self.name, self.last_error)
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self.session = None

    async def _refresh_tools(self) -> None:
        tools: list[ToolInfo] = []
        cursor: str | None = None
        while True:
            if cursor is None:
                res = await self.session.list_tools()
            else:
                from mcp import types

                res = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            data = _dump(res)
            for t in data.get("tools", []):
                name = t["name"]
                if self.cfg.include_tools and not any(fnmatch(name, p) for p in self.cfg.include_tools):
                    continue
                if any(fnmatch(name, p) for p in self.cfg.exclude_tools):
                    continue
                tools.append(
                    ToolInfo(
                        server=self.name,
                        name=name,
                        description=t.get("description") or t.get("title") or "",
                        input_schema=normalize_schema(t.get("inputSchema")),
                    )
                )
            cursor = data.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    async def stop(self) -> None:
        async with self._conn_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._task = None
        self.session = None

    async def restart(self) -> None:
        async with self._conn_lock:
            await self._stop_locked()
            await self._start_locked()

    async def read_resource_to_file(self, uri: str, saver: BlobSaver) -> Path | None:
        """MCP resources/read 로 파일 내용을 받아 저장 (원격 서버 파일 가져오기용)."""
        from .files import decode_b64

        async with self._lock:
            if not self.connected:
                await self.start()
            res = await asyncio.wait_for(self.session.read_resource(uri), timeout=self.cfg.tool_timeout)
        for c in _dump(res).get("contents") or []:
            if c.get("blob"):
                return saver(decode_b64(c["blob"]), uri, c.get("mimeType"))
            if "text" in c:
                return saver(c["text"].encode("utf-8"), uri, c.get("mimeType"))
        return None

    async def call(self, tool: str, arguments: dict[str, Any], saver: BlobSaver | None = None) -> tuple[str, bool, list[Path]]:
        async with self._lock:
            attempts = 2 if self.cfg.retry_on_failure else 1
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    if not self.connected:
                        await self.start()
                    result = await asyncio.wait_for(
                        self.session.call_tool(tool, arguments), timeout=self.cfg.tool_timeout
                    )
                    self.last_data = tool_result_data(_dump(result)) if hasattr(result, "model_dump") else None
                    return tool_result_to_text(result, saver)
                except asyncio.TimeoutError:
                    return f"오류: tool '{tool}' 실행이 {self.cfg.tool_timeout}초 내에 끝나지 않았습니다.", True, []
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    log.warning("MCP '%s.%s' 호출 실패(%d/%d): %s", self.name, tool, attempt + 1, attempts, e)
                    if attempt + 1 < attempts:
                        try:
                            await self.restart()
                        except Exception as re_e:  # noqa: BLE001
                            return f"오류: MCP 서버 '{self.name}' 재연결 실패: {re_e}", True, []
            return f"오류: {describe_exception(last_exc) if last_exc else '알 수 없음'}", True, []


# ---------------------------------------------------------------------------
# 내장 tool (MCP 서버 없이 에이전트가 직접 제공)
# ---------------------------------------------------------------------------

BuiltinHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class BuiltinTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: BuiltinHandler


@dataclass
class _Route:
    server: str
    tool: str
    builtin: BuiltinTool | None = None


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False
    files: list[dict[str, Any]] = field(default_factory=list)  # 감지/점검한 파일 상태
    data: Any = None  # 구조화 결과 (동적 도구의 ctx.call_tool_data 용)


@dataclass
class MCPHub:
    servers: dict[str, MCPServerConfig]
    log_dir: Path | None = None
    builtins: list[BuiltinTool] = field(default_factory=list)
    workspace: Any = None  # files.FileWorkspace
    auto_detect: bool = True

    def __post_init__(self) -> None:
        self.connections: dict[str, ServerConnection] = {
            name: ServerConnection(cfg, self.log_dir) for name, cfg in self.servers.items() if cfg.enabled
        }
        self._routes: dict[str, _Route] = {}
        self._functions: list[tuple[str, dict[str, Any]]] = []  # (server, openai tool spec)
        self.custom_tools: list[BuiltinTool] = []  # 에이전트가 만든 동적 도구 (custom__<이름>)
        self.downloader: Any = None  # downloader.Downloader (다운로드 링크 자동 처리용)

    async def __aenter__(self) -> "MCPHub":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self, only: list[str] | None = None) -> dict[str, str | None]:
        """모든(또는 지정한) 서버에 병렬 연결. 실패한 서버는 건너뛰고 오류를 반환."""
        targets = [c for n, c in self.connections.items() if only is None or n in only]

        async def _one(c: ServerConnection) -> tuple[str, str | None]:
            try:
                await c.start()
                return c.name, None
            except Exception as e:  # noqa: BLE001
                msg = c.last_error or describe_exception(e)
                log.error("MCP 서버 '%s' 연결 실패: %s", c.name, msg)
                return c.name, msg

        results = dict(await asyncio.gather(*(_one(c) for c in targets)))
        self._rebuild()
        return results

    async def reconnect(self, name: str) -> ServerConnection:
        conn = self.connections[name]
        await conn.restart()
        self._rebuild()
        return conn

    async def close(self) -> None:
        await asyncio.gather(*(c.stop() for c in self.connections.values()), return_exceptions=True)

    def _rebuild(self) -> None:
        used: set[str] = set()
        routes: dict[str, _Route] = {}
        funcs: list[tuple[str, dict[str, Any]]] = []
        for b in self.builtins:
            used.add(b.name)
            routes[b.name] = _Route("builtin", b.name, b)
            funcs.append(("builtin", _spec(b.name, b.description, b.input_schema)))
        for b in self.custom_tools:
            fn = f"custom{NAME_SEP}{b.name}"
            used.add(fn)
            routes[fn] = _Route("custom", b.name, b)
            funcs.append(("custom", _spec(fn, b.description, b.input_schema)))
        for conn in self.connections.values():
            for t in conn.tools:
                fn = make_function_name(conn.name, t.name, used)
                t.function_name = fn
                routes[fn] = _Route(conn.name, t.name)
                desc = t.description
                prefix = f"[{conn.name}" + (f": {conn.cfg.description}" if conn.cfg.description else "") + "] "
                funcs.append((conn.name, _spec(fn, (prefix + desc)[:1024], t.input_schema)))
        self._routes = routes
        self._functions = funcs

    def set_custom_tools(self, tools: list[BuiltinTool]) -> None:
        self.custom_tools = list(tools)
        self._rebuild()

    def openai_tools(self, servers: list[str] | None = None) -> list[dict[str, Any]]:
        """LLM 에 전달할 tools 목록. servers 를 주면 해당 서버(+builtin, 동적 도구)만."""
        return [spec for srv, spec in self._functions if servers is None or srv in servers or srv in ("builtin", "custom")]

    def server_status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "transport": c.cfg.transport,
                "description": c.cfg.description,
                "connected": c.connected,
                "tools": [t.name for t in c.tools],
                "error": c.last_error,
            }
            for c in self.connections.values()
        ]

    def describe_servers(self, servers: list[str] | None = None) -> str:
        lines = []
        for c in self.connections.values():
            if servers is not None and c.name not in servers:
                continue
            state = "연결됨" if c.connected else f"연결 안 됨({c.last_error or '미연결'})"
            line = f"- {c.name}: {c.cfg.description or '(설명 없음)'} [{state}, tool {len(c.tools)}개]"
            if c.instructions:
                line += f"\n  서버 안내: {c.instructions.strip()[:500]}"
            lines.append(line)
        return "\n".join(lines) or "- (연결된 MCP 서버 없음)"

    async def call(self, function_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        route = self._routes.get(function_name)
        if route is None:
            return ToolOutcome(f"오류: 알 수 없는 tool '{function_name}'", True)
        ws = self.workspace
        if route.builtin is not None:
            # 동적 도구는 파일을 내려받는 경우가 많으므로 MCP tool 처럼 폴더 변화도 점검
            before = await asyncio.to_thread(ws.snapshot) if route.server == "custom" and ws is not None and self.auto_detect else None
            try:
                res = await route.builtin.handler(arguments)
            except Exception as e:  # noqa: BLE001
                return ToolOutcome(f"오류: {describe_exception(e)}", True)
            out = res if isinstance(res, ToolOutcome) else ToolOutcome(str(res))
            if before is not None:
                inspected = await self._inspect_files(out.text, out.is_error, [], before)
                inspected.files = out.files + inspected.files
                return inspected
            return out

        before = await asyncio.to_thread(ws.snapshot) if ws is not None and self.auto_detect else None
        saver = (lambda data, name, mime: ws.save_blob(data, name, mime, route.server)) if ws is not None else None
        conn = self.connections[route.server]
        text, is_err, saved = await conn.call(route.tool, arguments, saver)
        data = conn.last_data if not is_err else None  # 서버 단위 lock 안에서 설정됨
        if ws is None:
            return ToolOutcome(text, is_err, data=data)
        if conn.is_remote:
            text, fetched = await self._handle_remote(conn, text, is_err)
            saved = saved + fetched
        out = await self._inspect_files(text, is_err, saved, before, remote=conn.is_remote)
        out.data = data
        return out

    async def _handle_remote(self, conn: ServerConnection, text: str, is_err: bool) -> tuple[str, list[Path]]:
        """원격 MCP 결과: localhost 링크를 서버 주소로 바꾸고, 원격 경로의 파일을 가져옴."""
        from .remote_files import RemoteFileFetcher, find_remote_paths, rewrite_localhost

        rcfg = conn.remote_files
        notes: list[str] = []
        if rcfg.rewrite_localhost and conn.cfg.url:
            text, changes = rewrite_localhost(text, conn.cfg.url)
            import urllib.parse

            # 원래 URL 을 그대로 적으면 링크 감지에 다시 잡히므로 '호스트:포트' 만 표시
            pairs = dict.fromkeys((urllib.parse.urlparse(o).netloc, urllib.parse.urlparse(n).netloc) for o, n in changes)
            for o, n in list(pairs)[:5]:
                notes.append(f"- 링크 주소 변환: {o} → {n} (원격 서버 기준 localhost 를 MCP 서버 주소로)")
        fetched: list[Path] = []
        if rcfg.auto_fetch and not is_err:
            for rp in find_remote_paths(text)[: rcfg.max_files]:
                try:  # 에이전트 PC 에도 같은 경로가 있으면(공유 드라이브 등) 그대로 사용
                    lp = self.workspace.resolve(rp)
                    if lp.exists():
                        continue
                except (PermissionError, ValueError, OSError):
                    pass
                p, how = await RemoteFileFetcher(self).fetch(conn, rp)
                if p is not None:
                    fetched.append(p)
                    notes.append(f"- {rp}\n  → {p} ({how})")
                else:
                    notes.append(f"- {rp}\n  → 가져오지 못함: {how}")
        if notes:
            text += "\n\n[원격 MCP 파일 처리]\n" + "\n".join(notes)
        return text, fetched

    async def _claim(self, st: dict[str, Any]) -> dict[str, Any]:
        """사용자 실행이면, 공용 다운로드 폴더에 완성된 파일을 그 사용자의 downloads 로 옮김."""
        claim = getattr(self.workspace, "claim", None)
        if claim is None or st.get("state") not in ("complete", "suspicious", "corrupt", "empty"):
            return st
        from .files import file_status

        old = Path(st["path"])
        new = await asyncio.to_thread(claim, old)
        if Path(new) == old:
            return st
        st2 = await file_status(Path(new), self.workspace, stable_wait=0)
        st2.setdefault("issues", []).append(f"공용 다운로드 폴더에서 사용자 폴더로 옮김 (원래: {old})")
        return st2

    @staticmethod
    def _download_links(text: str) -> list[str]:
        from .downloader import find_urls

        links = find_urls(text)
        # resource_link 로 온 http 링크도 포함
        for m in re.finditer(r"\[리소스 링크\][^\n]*?(https?://\S+)", text):
            if m.group(1) not in links:
                links.append(m.group(1))
        return links

    async def _inspect_files(
        self, text: str, is_err: bool, saved: list[Path], before: dict[str, Any] | None, remote: bool = False
    ) -> ToolOutcome:
        """tool 호출로 생긴 파일(저장된 blob / 다운로드 폴더 변화 / 결과에 적힌 경로)을 찾아 상태를 점검."""
        from .files import file_status, format_status

        ws = self.workspace
        paths: list[Path] = list(saved)
        if before is not None:
            after = await asyncio.to_thread(ws.snapshot)
            paths += ws.diff(before, after)
        paths += await asyncio.to_thread(ws.find_paths_in_text, text)
        uniq: list[Path] = []
        for p in paths:
            if p not in uniq:
                uniq.append(p)
        links = [] if is_err else self._download_links(text)
        if links and self.downloader is not None and self.downloader.cfg.auto_download:
            from .downloader import DownloadError

            for url in links[:3]:
                try:
                    info = await asyncio.to_thread(self.downloader.download, url)
                    p = Path(info["path"])
                    if p not in uniq:
                        uniq.append(p)
                except DownloadError as e:
                    text += f"\n\n[다운로드 링크 자동 받기 실패] {url}\n  - {e}"
            links = []
        if links:
            text += "\n\n[다운로드 링크 감지] " + " , ".join(links) + "\n※ download_url tool 로 받으세요 (로그인이 필요하면 cookies 인자 사용)."
        if not uniq:
            if not links and not is_err and re.search(r"다운로드|내려받|download|export|엑셀|excel", text, re.I):
                if remote:
                    text += (
                        "\n\n[파일 감지] 원격 MCP 서버 PC 에서 받은 파일은 에이전트 PC 다운로드 폴더에 보이지 않습니다. "
                        "결과에 서버 경로가 나오면 fetch_remote_file 로 가져오세요. "
                        "(공유폴더 path_map 이 설정되어 있으면 wait_for_download 로 기다릴 수 있습니다)"
                    )
                else:
                    text += (
                        "\n\n[파일 감지] 아직 감시 폴더에 새 파일이 보이지 않습니다. 다운로드가 백그라운드로 진행 중일 수 있으니 "
                        "wait_for_download 로 완료를 확인하세요."
                    )
            return ToolOutcome(text, is_err)
        statuses = [await file_status(p, ws, stable_wait=1.0) for p in uniq[:10]]
        # 호출 중 잠깐 생겼다 사라진 임시 파일은 제외
        statuses = [st for st in statuses if not (st["state"] in ("missing", "locked") and not Path(st["path"]).exists())]
        if not statuses:
            return ToolOutcome(text, is_err)
        # 임시파일(.crdownload)과 그 완성본이 같이 잡히면 완성본만
        names = {Path(st["path"]).name for st in statuses}
        statuses = [
            st for st in statuses
            if not (Path(st["path"]).suffix.lower() in (".crdownload", ".part", ".tmp") and Path(st["path"]).stem in names)
        ]
        statuses = [await self._claim(st) for st in statuses]
        lines = ["", "[파일 감지 - 에이전트 자동 점검]"] + [format_status(st) for st in statuses]
        if any(st["state"] == "downloading" for st in statuses):
            lines.append("※ 아직 다운로드 중인 파일이 있습니다. wait_for_download 로 완료를 확인한 뒤 분석하세요.")
        if any(st["state"] in ("corrupt", "suspicious", "empty") for st in statuses):
            lines.append("※ 정상이 아닌 파일이 있습니다. 내용을 analyze_file 로 확인하고, 필요하면 다시 내려받으세요.")
        elif any(st["state"] == "complete" for st in statuses):
            lines.append("※ analyze_file 로 내용을 분석할 수 있습니다.")
        return ToolOutcome(text + "\n".join(lines), is_err, statuses)

    def route_of(self, function_name: str) -> tuple[str, str] | None:
        r = self._routes.get(function_name)
        return (r.server, r.tool) if r else None


def _spec(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}


__all__ = [
    "MCPHub",
    "ToolOutcome",
    "BuiltinTool",
    "ServerConnection",
    "ToolInfo",
    "make_function_name",
    "normalize_schema",
    "tool_result_to_text",
]
