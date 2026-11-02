"""데모/테스트용 샘플 MCP 서버 (실제 사이트 대신 가짜 데이터를 돌려줌).

실제 사내 웹컨트롤 MCP 서버도 이와 같은 형태로 tool 을 노출하면 에이전트가 자동으로 인식합니다.
mcp SDK 1.x(FastMCP) / 2.x(MCPServer) 모두에서 동작합니다.
"""

from __future__ import annotations

import base64
import csv
import io
import os
import random
import threading
import time
from datetime import date, timedelta
from pathlib import Path

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource

# 에이전트가 stdio 로 띄울 때 넘겨주는 다운로드 폴더 (실제 웹컨트롤 MCP 도 브라우저 다운로드 경로를 여기로 맞추면 좋음)
DOWNLOAD_DIR = Path(os.environ.get("AGENT_DOWNLOAD_DIR", "./downloads"))

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


def _sales_rows(month: str) -> list[list]:
    y, m = map(int, month.split("-"))
    rows = []
    d = date(y, m, 1)
    while d.month == m:
        rnd = random.Random(d.toordinal())
        for dept in _DEPTS[:3]:
            rows.append([d.isoformat(), dept, rnd.choice(["A제품", "B제품", "C제품"]), rnd.randint(1, 30), rnd.randint(5, 50) * 100_000])
        d += timedelta(days=1)
    return rows


def _write_xlsx(path: Path, month: str) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "매출현황"
    ws.append([f"{month} 부서별 매출 현황"])  # 실제 사내 엑셀처럼 제목행/출력일 행이 먼저 나옴
    ws.append([f"출력일: {date.today().isoformat()}"])
    ws.append([])
    ws.append(["일자", "부서", "제품", "수량", "금액"])
    for r in _sales_rows(month):
        ws.append(r)
    wb.save(path)


def _slow_download(final: Path, writer, seconds: float) -> None:
    """브라우저처럼 .crdownload 로 받다가 끝나면 이름을 바꿈."""
    tmp = final.with_name(final.name + ".crdownload")
    buf = io.BytesIO()
    writer(buf)
    data = buf.getvalue()
    with open(tmp, "wb") as f:
        step = max(1, len(data) // 4)
        for i in range(0, len(data), step):
            f.write(data[i : i + step])
            f.flush()
            time.sleep(seconds / 4)
    tmp.replace(final)


@mcp.tool()
def download_sales_excel(month: str, wait: bool = False) -> str:
    """ERP 화면에서 월별 매출 엑셀 다운로드 버튼을 누릅니다. month 형식: YYYY-MM.
    브라우저 다운로드처럼 백그라운드로 받으므로, 끝났는지는 다운로드 폴더를 확인해야 합니다."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    final = DOWNLOAD_DIR / f"매출현황_{month}.xlsx"

    def writer(buf):
        tmp = DOWNLOAD_DIR / f".tmp_{month}.xlsx"
        _write_xlsx(tmp, month)
        buf.write(tmp.read_bytes())
        tmp.unlink()

    if wait:
        _slow_download(final, writer, 0.2)
        return f"다운로드 완료: {final}"
    threading.Thread(target=_slow_download, args=(final, writer, 2.0), daemon=True).start()
    return "다운로드 버튼을 눌렀습니다. 브라우저가 파일을 받는 중입니다."


@mcp.tool()
def export_notice_attachment(notice_id: str) -> list:
    """공지사항 첨부파일(CSV)을 내려받아 파일 내용 자체를 돌려줍니다."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["순번", "항목", "담당부서", "마감일"])
    rnd = random.Random(notice_id)
    for i in range(1, 8):
        w.writerow([i, rnd.choice(_TITLES), rnd.choice(_DEPTS), f"2026-11-{rnd.randint(10, 28)}"])
    blob = base64.b64encode(out.getvalue().encode("cp949")).decode()  # 사내 시스템은 cp949 CSV 가 흔함
    return [
        f"{notice_id} 첨부파일을 내려받았습니다.",
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(uri=f"file:///attachments/{notice_id}_첨부.csv", mimeType="text/csv", blob=blob),
        ),
    ]


@mcp.tool()
def download_approval_list(month: str) -> str:
    """결재 목록 엑셀을 다운로드합니다 (세션이 만료된 상황을 흉내냄 - 로그인 페이지가 xlsx 로 저장됨)."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = DOWNLOAD_DIR / f"결재목록_{month}.xlsx"
    path.write_text(
        "<!DOCTYPE html><html><head><title>로그인</title></head><body>"
        "<h1>세션이 만료되었습니다. 다시 로그인하세요.</h1><form><input name='id'></form></body></html>",
        encoding="utf-8",
    )
    return f"파일 저장: {path}"


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
