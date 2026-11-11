---
name: product_rule_loop
description: 과거 개발 건(베이스 → 개발 모델)과 BOM 에서 제품 개발 규칙을 뽑고, 담당자에게 확인받아 스킬에 반영하고 채점한다. "개발 규칙 학습", "BOM 규칙 찾아줘", "베이스 선택 경향", "색상 바뀌면 바뀌는 부품" 요청에 사용. 자료가 늘 때마다 다시 실행하면 규칙이 정확해진다.
inputs:
  - name: rulebook
    label: 제품 규칙집 이름
    default: washer
    required: true
    description: 제품군마다 하나 (예 washer, dryer)
  - name: projects
    label: 개발 건 표 (베이스·개발 모델, 툴, 향지… 파일/폴더, 여러 개는 줄바꿈)
    type: text
    description: 처음이면 필수. 다시 실행할 때는 새로 생긴 자료만 주면 됩니다
  - name: boms
    label: BOM (모델·레벨·품번·설명, 파일/폴더)
    type: text
  - name: pattern
    label: 모델명 규칙 (정규식, 이름 붙은 그룹)
    description: "예) (?P<series>[A-Z]{2}\\d{2})(?P<capacity>\\d{2})(?P<color>[A-Z])\\.(?P<suffix>\\w+)"
  - name: target_skill
    label: 규칙을 반영할 스킬 이름 (선택)
# LLM 없이(토큰 없이) 실행: 학습 → 물어볼 질문 → 채점. 담당자 답 기록·스킬 반영은 LLM 이 있을 때 지침대로 (또는 도구를 직접 실행)
steps:
  - id: learn
    title: 자료 학습 (규칙 후보 뽑기)
    tool: product_rules_learn
    args: {name: "{rulebook}", projects: "{projects|lines}", boms: "{boms|lines}", pattern: "{pattern}"}
  - id: review
    title: 담당자에게 확인할 질문
    tool: product_rules_show
    args: {name: "{rulebook}", view: review, limit: 10}
  - id: eval
    title: 과거 건으로 채점
    tool: product_rules_evaluate
    args: {name: "{rulebook}"}
    on_error: continue
output: |
  ## '{rulebook}' 제품 규칙 학습
  {steps.learn.text}

  {steps.review.text}

  {steps.eval.text}

  답을 기록하려면: /product_rules_answer name={rulebook} rule_id=<규칙 id> verdict=confirmed|rejected|edited note="이유"
---

# 제품 개발 규칙 학습 반복

사람들이 과거에 한 개발(베이스 선택, BOM 변경)에서 규칙을 찾고, 담당자의 답으로 다듬는 반복 작업입니다.
한 번에 끝내는 일이 아니라, 새 개발 건이 생기거나 담당자가 답할 때마다 다시 돌립니다.

## 절차

1. `product_rules_learn` 으로 자료를 학습합니다 (name={rulebook}, projects, boms, pattern).
   - "열을 찾지 못했다"고 하면 파일을 `analyze_file` 로 열어 실제 열 이름을 확인하고 `cols` 로 알려 줍니다.
   - "BOM 이 없는 모델"이 나오면 사용자에게 그 모델의 BOM 을 받을 수 있는지 알립니다.
2. `product_rules_show` (view=review) 로 확인할 질문을 받습니다.
3. 질문을 **하나씩** `ask_user` 로 담당자에게 묻습니다. 근거(어느 개발 건에서 어떤 품번이 어떻게 바뀌었는지)를 함께 보여 주세요.
   - 선택지: 맞음 / 틀림 / 조건이 있음(수정). 한 번에 최대 5개만 묻고, 더 있으면 다음에 이어서 하자고 알립니다.
   - 답을 추측해서 기록하지 마세요. 담당자가 답하지 않은 규칙은 후보로 둡니다.
4. 답을 `product_rules_answer` 로 기록합니다. "조건이 있음"이면 verdict=edited, text 에 고친 규칙 문장, note 에 이유를 적습니다.
5. {target_skill} 이 있으면 `product_rules_export_skill` 로 확정 규칙을 그 스킬에 반영합니다.
6. `product_rules_evaluate` 로 채점하고, 지난 회차와 비교해 알려 줍니다. "자주 놓친 부품"은 아직 규칙으로 설명하지 못한 변경이므로,
   모델명 규칙에 속성이 빠졌는지(예: 향지, 등급) 사용자와 확인합니다.

## 결과 보고

- 이번 회차에 찾은 규칙 수 (바뀌는 부품 / 개발 현황 / 베이스 선택), 새로 확정·틀림 처리한 수
- 정밀도·재현율과 지난 회차 대비 변화
- 아직 답이 필요한 질문 수, 더 있으면 좋은 자료 (BOM 없는 모델 등)
