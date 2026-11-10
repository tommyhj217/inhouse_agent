"""파일 버전 보기·되돌리기 (opencode 의 파일 변경 기록을 가져옴).

에이전트가 엑셀·PPT·텍스트 파일을 덮어쓸 때마다 FileWorkspace._before_write 가 원본을 작업폴더의
.versions/<상대경로>/ 에 남깁니다(파일마다 최근 10개). 여기서는 그 목록을 보여주고 되돌립니다.
되돌릴 때도 지금 내용을 먼저 보관하므로, 되돌리기를 다시 되돌릴 수 있습니다.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from .files import FileAccessError, file_status, format_status
from .mcp_hub import BuiltinTool, ToolOutcome


def make_version_tools(ws: Any) -> list[BuiltinTool]:
    async def file_versions(args: dict[str, Any]) -> ToolOutcome:
        p = ws.resolve(str(args["path"]))
        vs = await asyncio.to_thread(ws.list_versions, p)
        if not vs:
            return ToolOutcome(f"'{ws.display(p)}' 의 이전 버전이 없습니다 (에이전트가 덮어쓴 적이 없음).", False, [], {"versions": []})
        lines = [f"## '{ws.display(p)}' 이전 버전 {len(vs)}개 (최근 순, restore_file_version 으로 되돌리기)"]
        lines += [f"{v['no']}. {v['saved']} · {v['size']:,} bytes · id {v['id']}" for v in vs]
        return ToolOutcome("\n".join(lines), False, [], {"versions": vs})

    async def restore_file_version(args: dict[str, Any]) -> ToolOutcome:
        p = ws.resolve(str(args["path"]))
        vs = await asyncio.to_thread(ws.list_versions, p)
        if not vs:
            return ToolOutcome(f"오류: '{ws.display(p)}' 의 이전 버전이 없습니다.", True)
        want = str(args.get("version") or "1")
        v = next((x for x in vs if want in (str(x["no"]), x["id"])), None)
        if v is None:
            return ToolOutcome(f"오류: 버전 '{want}' 이 없습니다. file_versions 로 목록을 보세요.", True)
        try:
            ws.resolve(str(p), write=True)  # 지금 내용도 먼저 보관 (되돌리기를 되돌릴 수 있게)
        except FileAccessError as e:
            return ToolOutcome(f"오류: {e}", True)
        await asyncio.to_thread(shutil.copy2, v["path"], p)
        st = await file_status(p, ws, stable_wait=0)
        return ToolOutcome(f"되돌림: '{ws.display(p)}' ← {v['saved']} 버전 (되돌리기 전 내용도 보관함)\n" + format_status(st), False, [st])

    path_prop = {"path": {"type": "string", "description": "파일 경로 (작업폴더 기준)"}}
    return [
        BuiltinTool("file_versions", "에이전트가 덮어쓴 파일의 이전 버전 목록을 봅니다 (덮어쓸 때마다 자동 보관, 파일마다 최근 10개).",
                    {"type": "object", "properties": path_prop, "required": ["path"]}, file_versions),
        BuiltinTool("restore_file_version", "파일을 이전 버전으로 되돌립니다. version: file_versions 의 번호(1 = 바로 전) 또는 id. 지금 내용도 먼저 보관합니다.",
                    {"type": "object", "properties": {**path_prop, "version": {"type": "string", "description": "번호 또는 id (기본 1 = 바로 전 버전)"}},
                     "required": ["path"]}, restore_file_version),
    ]
