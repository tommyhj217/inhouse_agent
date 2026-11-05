---
name: monthly_sales_report
description: ERP 에서 월별 매출 엑셀을 내려받아 상태를 확인하고 부서별/제품별 매출을 집계해 보고서로 만든다. "월 매출", "매출 집계", "매출 보고" 요청에 사용.
servers: [demo]
inputs:
  - name: month
    label: 대상 월
    type: string
    default: "{last_month}"
    required: true
    description: YYYY-MM
  - name: dept
    label: 부서
    type: select
    options: [전체, 인사팀, 총무팀, IT운영팀]
    default: 전체
  - name: confirm_dept
    label: 실행 중 부서 확인 질문
    type: boolean
    default: false
# LLM 없이(토큰 없이) 실행할 단계: 채팅창에서 /monthly_sales_report 로 바로 테스트
steps:
  - id: dl
    title: 매출 엑셀 다운로드
    tool: demo__download_sales_excel
    args: {month: "{month}", wait: true}
  - id: dept
    title: 부서별 집계
    tool: query_table
    when: "{dept} == 전체"
    args: {path: "{steps.dl.file}", group_by: [부서], aggregates: [{column: 금액, func: sum}, {column: 수량, func: sum}, {func: count}]}
  - id: one
    title: 선택 부서의 제품별 집계
    tool: query_table
    when: "{dept} != 전체"
    args: {path: "{steps.dl.file}", filters: [{column: 부서, op: "==", value: "{dept}"}], group_by: [제품], aggregates: [{column: 금액, func: sum}]}
  - id: prod
    title: 제품별 집계
    tool: query_table
    args: {path: "{steps.dl.file}", group_by: [제품], aggregates: [{column: 금액, func: sum}]}
output: |
  ## {month} 매출 집계 (부서: {dept})
  {steps.dept.text}{steps.one.text}

  ### 제품별
  {steps.prod.text}

  사용 파일: {steps.dl.file}
---
# 목표
{month} 매출 엑셀을 ERP 에서 받아 정확한 수치로 집계한다. 추측한 숫자는 절대 쓰지 않는다.

# 절차
1. `download_sales_excel` 로 {month} 엑셀 다운로드를 요청한다.
2. 결과의 [파일 감지] 를 확인한다.
   - 다운로드 중이거나 감지된 파일이 없으면 `wait_for_download` (pattern: `매출현황_{month}*.xlsx`) 로 완료를 기다린다.
   - 파일이 손상/의심 상태면 1번을 한 번만 다시 시도하고, 그래도 안 되면 원인을 보고하고 멈춘다.
3. `analyze_file` 로 시트 구조(열 이름, 행 수)를 확인한다.
4. confirm_dept 가 true 이면 `ask_user` 로 "어느 부서를 집계할까요?" 를 묻는다 (선택지: 전체, 인사팀, 총무팀, IT운영팀, 기본값 {dept}). 아니면 {dept} 를 쓴다.
5. `query_table` 로 계산한다.
   - 부서가 '전체'가 아니면 filters 로 해당 부서만 남긴다.
   - group_by [부서] + aggregates [sum(금액), sum(수량), count] 로 부서별 합계
   - group_by [제품] + aggregates [sum(금액)] 로 제품별 합계
6. 결과를 `save_file` 로 `reports/sales_{month}.md` 에 저장한다.

# 결과 형식
- 핵심 요약 3줄 (총매출, 1위 부서, 1위 제품)
- 부서별 표, 제품별 표 (금액은 천 단위 콤마)
- 사용한 파일 경로와 파일 상태
