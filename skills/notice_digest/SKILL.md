---
name: notice_digest
description: 그룹웨어 공지사항과 첨부파일을 확인해 부서별로 요약한다. "공지 요약", "공지 정리" 요청에 사용.
servers: [demo]
inputs:
  - name: date
    label: 조회일
    type: date
    default: "{yesterday}"
    required: true
---
# 절차
1. `list_notices` 로 {date} 공지 목록을 가져온다.
2. 각 공지에 대해 `get_notice` 로 본문을 확인하고, 첫 번째 공지는 `export_notice_attachment` 로 첨부파일도 받는다.
3. 받은 첨부파일은 [파일 감지] 상태를 확인한 뒤 `analyze_file` 로 내용을 본다.
4. 부서별로 묶어서 정리한다.

# 결과 형식
| 부서 | 제목 | 핵심 내용 | 마감일 |
그리고 첨부파일 요약과 파일 경로.
