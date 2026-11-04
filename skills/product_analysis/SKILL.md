---
name: product_analysis
description: 규칙집(과거 보고서에서 배운 장별 구성·규칙)을 바탕으로 새 제품/기술(예 뮤즈, AI 비서)을 조사·분석해 같은 형식의 PPT 보고서를 만들고, 규칙으로 검증·수정하며, 분석 중 알게 된 것으로 규칙을 갱신한다.
inputs:
  - name: product
    label: 분석할 제품/기술
    required: true
    description: 예) 뮤즈, AI 비서
  - name: rulebook
    label: 규칙집
    default: tech_report
    required: true
  - name: sources
    label: 참고 자료 (파일/URL, 선택)
    type: text
  - name: template
    label: 디자인 템플릿 (과거 PPT, 선택)
    type: file
---
# 목표
'{rulebook}' 규칙집 기준으로 '{product}' 분석 보고서를 만들고, 규칙을 지켰는지 검증한 뒤, 이번 분석에서 배운 점을 규칙집에 반영한다.

# 절차
1. update_plan 으로 단계를 세운다 (계획 → 조사 → 작성 → 검증 → 수정 → 기록 → 규칙 갱신).
2. rulebook_outline(name={rulebook}, product={product}) 로 장별 계획을 받는다. 장마다 필요한 형식(표/차트)과 표 열, 다룰 내용을 확인한다.
3. 장마다 필요한 자료를 모은다. 숫자는 출처가 있는 것만 쓴다.
   - 참고 자료: {sources} (파일은 analyze_file/doc_structure, URL 은 web_fetch/download_url)
   - 사내 사이트는 MCP tool, 공개 정보는 web_search/web_fetch (설정된 경우)
   - recall / list_past_work 로 이전에 분석한 관련 제품과 rulebook_show(view=products) 의 기록을 참고해 비교 관점을 맞춘다.
   - 자료가 부족한 장은 지어내지 말고 "확인 필요"로 표시한다. 필요하면 ask_user 로 확인한다.
4. write_pptx 로 보고서를 만든다 (template={template} 가 있으면 사용). 장 제목은 장별 구성의 이름을 따른다.
   필요하면 write_excel 로 평가표/비교표도 만든다.
5. rulebook_validate(name={rulebook}, path=만든 PPT, product={product}).
   - 어긴 must/should 규칙을 고쳐서 다시 작성하고 다시 검증한다 (최대 3회).
   - judgment 규칙은 문서를 읽고 rulebook_judge 로 판정한다.
6. 규칙 갱신을 **스스로 판단**한다.
   - 이 제품 특성상 꼭 필요한데 장별 구성에 없는 장(예: 생성형 AI 면 '데이터·보안/환각 리스크')이 있었다면, 근거를 적어 후보 규칙(status=candidate)으로 추가한다.
   - 규칙이 이 제품에 맞지 않아 어길 수밖에 없었다면, rulebook_update_rule 로 조건을 고치기보다 먼저 이유를 결과에 적고 사용자에게 제안한다.
7. rulebook_record_product(name={rulebook}, product={product}, summary, facts={핵심 수치/특징}, files, validation_id).
8. 최종 답변에 보고서 경로, 검증 점수, 남은 문제, 제안하는 규칙 변경을 정리한다.
   사용자가 보고서를 승인하면 rulebook_validate(label=exemplar) 로 좋은 예시에 추가할 수 있다고 안내한다.

# 결과 형식
- 핵심 요약 3줄
- 장별 작성 결과 (장 / 내용 요약 / 출처)
- 검증 결과 (점수, 어긴 규칙과 처리)
- 규칙집 갱신 제안 / 반영 내역
- 파일 경로 (PPT, 엑셀)
