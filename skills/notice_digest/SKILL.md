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
steps:
  - id: list
    title: 공지 목록
    tool: demo__list_notices
    args: {date_from: "{date}"}
  - id: body
    title: 첫 공지 본문
    tool: demo__get_notice
    args: {notice_id: "{steps.list.data.0.id}"}
  - id: att
    title: 첨부파일 받기
    tool: demo__export_notice_attachment
    args: {notice_id: "{steps.list.data.0.id}"}
  - id: ana
    title: 첨부파일 분석
    tool: analyze_file
    args: {path: "{steps.att.file}", sample_rows: 5}
output: |
  ## {date} 공지 요약 (워크플로 실행)
  {steps.list.text}

  ### 첫 공지 ({steps.list.data.0.title} / {steps.list.data.0.dept})
  {steps.body.text}

  ### 첨부파일
  {steps.ana.text}
---
# 절차
1. `list_notices` 로 {date} 공지 목록을 가져온다.
2. 각 공지에 대해 `get_notice` 로 본문을 확인하고, 첫 번째 공지는 `export_notice_attachment` 로 첨부파일도 받는다.
3. 받은 첨부파일은 [파일 감지] 상태를 확인한 뒤 `analyze_file` 로 내용을 본다.
4. 부서별로 묶어서 정리한다.

# 결과 형식
| 부서 | 제목 | 핵심 내용 | 마감일 |
그리고 첨부파일 요약과 파일 경로.
