---
name: learn_rules
description: 과거 사람들이 만든 보고서(PPT/엑셀/Word/PDF)를 분석해서 장별 구성과 작성 규칙을 만들고, 근거(지지율)를 확인해 규칙집을 갱신한다. "과거 자료 학습", "규칙 만들어", "보고서 기준 정리" 요청에 사용.
inputs:
  - name: rulebook
    label: 규칙집 이름
    default: tech_report
    required: true
    description: 영문/한글 (예 tech_report)
  - name: description
    label: 규칙집 설명
    default: 기술·제품 분석 보고서
  - name: paths
    label: 과거 자료 (파일/폴더, 여러 개는 줄바꿈)
    type: text
    required: true
    description: 예) uploads/past_reports
# LLM 없이(토큰 없이) 실행: 학습 → 점검 → 엑셀 내보내기까지 (후보 규칙 검토·판단 규칙 추가는 LLM 이 있을 때 지침대로)
steps:
  - id: create
    title: 규칙집 만들기 (이미 있으면 건너뜀)
    tool: rulebook_create
    args: {name: "{rulebook}", description: "{description}"}
    on_error: continue
  - id: learn
    title: 과거 자료 학습
    tool: rulebook_learn
    args: {name: "{rulebook}", paths: "{paths|lines}", label: exemplar}
  - id: reflect
    title: 스스로 점검
    tool: rulebook_reflect
    args: {name: "{rulebook}"}
  - id: export
    title: 엑셀로 내보내기
    tool: rulebook_export
    args: {name: "{rulebook}"}
output: |
  ## '{rulebook}' 규칙집 학습 결과 (워크플로 실행 - 후보 규칙 검토는 LLM 연결 후 또는 규칙 탭에서)
  {steps.learn.text}

  {steps.reflect.text}

  내보낸 엑셀: {steps.export.file}
---
# 목표
{paths} 의 과거 자료를 **좋은 예시**로 보고, '{rulebook}' 규칙집에 다음을 만든다.
1) 장별 구성: 보통 어떤 장이 어떤 순서로, 장마다 어떤 형식(표/차트/이미지)과 내용으로 들어가는지
2) 검사 가능한 규칙 + 사람이 판단해야 하는 규칙(judgment)

# 절차
1. update_plan 으로 단계를 세운다.
2. rulebook_list 로 '{rulebook}' 이 있는지 보고, 없으면 rulebook_create(name={rulebook}, description={description}).
3. rulebook_learn(name={rulebook}, paths=[{paths} 의 각 줄], label=exemplar).
   - 읽지 못한 파일이 있으면 file_status 로 원인을 확인해 보고한다.
4. 자동으로 발굴된 후보 규칙을 **스스로 검토**한다.
   - 각 장의 실제 내용은 doc_structure 로 2~3개 자료를 직접 읽고 확인한다 (장마다 어떤 내용이 필요한지 판단).
   - 의미 없는 후보(우연히 겹친 핵심어, 제품명이 들어간 규칙 등)는 rulebook_update_rule 로 status=deprecated, 이유를 남긴다.
   - 꼭 필요한 장/요소는 severity 를 must 로 올린다.
5. 자료를 읽으며 발견한 기준 중 자동 후보에 없는 것을 rulebook_add_rule 로 추가한다. 예:
   - 출처 표기 (text_regex), 수치 단위, 표 열 구성, 결론 장 분량 (section_length)
   - 기계적으로 못 보는 기준은 judgment: "시사점이 앞 장의 데이터로 뒷받침되는가", "경쟁사 비교가 같은 기준으로 되어 있는가"
   - 규칙을 추가하면 지지율이 바로 나온다. 75% 미만이면 조건을 고치거나 근거를 다시 본다.
6. judgment 규칙은 좋은 예시 1~2건을 rulebook_validate(label=target) 로 검증한 뒤 내용을 읽고 rulebook_judge 로 판정해 근거를 쌓는다.
7. rulebook_reflect 로 스스로 점검하고, 제안을 반영한다 (반영하지 않는 제안은 이유를 적는다).
8. rulebook_export 로 엑셀을 만들어 경로를 알려준다.

# 결과 형식
- 장별 구성 표 (장 / 필요한 형식 / 다룰 내용)
- 활성 규칙 요약 (must → should → nice), 새로 추가·폐기한 규칙과 이유
- 규칙 신뢰도에 대한 의견 (좋은 예시 수, 지지율 낮은 규칙)
- 내보낸 엑셀 경로
