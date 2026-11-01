"""데모/테스트용 샘플 MCP 서버 (실제 사이트 대신 가짜 데이터를 돌려줌).

실제 사내 웹컨트롤 MCP 서버도 이와 같은 형태로 tool 을 노출하면 에이전트가 자동으로 인식합니다.
mcp SDK 1.x(FastMCP) / 2.x(MCPServer) 모두에서 동작합니다.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

mcp = _Server("demo-intranet")

_DEPTS = ["인사팀", "총무팀", "IT운영팀", "재무팀", "품질팀"]
_TITLES = ["보안 점검 일정 안내", "연말정산 서류 제출", "사내 시스템 정기 점검", "출장비 정산 기준 변경", "품질 교육 신청"]


@mcp.tool()
def list_notices(date_from: str, date_to: str | None = None) -> list[dict]:
    """그룹웨어 공지사항 목록을 조회합니다. 날짜 형식은 YYYY-MM-DD 입니다."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to) if date_to else start
    rows = []
    d = start
    while d <= end:
        rnd = random.Random(d.toordinal())
        for i in range(rnd.randint(1, 3)):
            rows.append(
                {
                    "id": f"N{d:%Y%m%d}{i}",
                    "date": d.isoformat(),
                    "title": rnd.choice(_TITLES),
                    "dept": rnd.choice(_DEPTS),
                }
            )
        d += timedelta(days=1)
    return rows


@mcp.tool()
def get_notice(notice_id: str) -> dict:
    """공지사항 ID 로 본문을 조회합니다."""
    rnd = random.Random(notice_id)
    return {"id": notice_id, "body": f"{notice_id} 공지 본문입니다. 마감일은 {rnd.randint(1, 28)}일 입니다."}


@mcp.tool()
def get_daily_sales(date_from: str, date_to: str) -> list[dict]:
    """ERP 일별 매출(단위: 원)을 조회합니다. 날짜 형식은 YYYY-MM-DD 입니다."""
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if (end - start).days > 92:
        # ToolError 메시지는 그대로 클라이언트(LLM)에 전달됩니다.
        raise ToolError("조회 기간은 최대 92일입니다.")
    out = []
    d = start
    while d <= end:
        out.append({"date": d.isoformat(), "sales": random.Random(d.toordinal()).randint(5, 50) * 1_000_000})
        d += timedelta(days=1)
    return out


if __name__ == "__main__":
    import sys

    # python sample_mcp_server.py            → stdio
    # python sample_mcp_server.py --http 8001 → http://127.0.0.1:8001/mcp (Streamable HTTP)
    if len(sys.argv) >= 3 and sys.argv[1] == "--http":
        port = int(sys.argv[2])
        try:
            mcp.run("streamable-http", host="127.0.0.1", port=port)  # mcp >= 2
        except TypeError:
            mcp.settings.host, mcp.settings.port = "127.0.0.1", port  # mcp 1.x
            mcp.run("streamable-http")
    else:
        mcp.run()
