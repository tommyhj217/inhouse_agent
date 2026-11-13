# 변경 이력

새 항목은 위에 추가합니다. 각 항목에는 **무엇을 · 왜 · 설정 변경(이전 방법)**을 적습니다. 지켜야 할 정책은 [`AGENTS.md`](../AGENTS.md)에 있습니다.

---

## 2026-11-13 — 스킬 러너 (Claude Code 스킬·스크립트를 Git에서 받아 실행·테스트)

**검토 결과** (이전 구현이 러너로 충분했는가)
- 이미 되던 것: Git·zip·폴더에서 스킬 가져오기, 스킬 스크립트 실행, `${CLAUDE_SKILL_DIR}`·`.claude/skills/` 경로 치환
- 빠져 있던 것:
  - `$ARGUMENTS`·`$0` 치환
  - 설명 없는 스킬 (오류로 거절됨)
  - `.claude/commands` 가져오기
  - Bash/Read 등 도구 이름 대응
  - 스크립트 환경변수 `CLAUDE_SKILL_DIR`
  - 설정한 저장소를 한 번에 받는 동기화
  - 테스트 러너

**무엇**
- **형식 호환**: 위 빠진 것 전부를 넣었습니다.
- **동기화**: `SkillInstaller.sync` + `skills.sources` 설정
  - CLI `skills sync [주소] [--test]`, `POST /api/skills/sync`, `skills.sync_on_start`
- **테스트 러너**: `skill_tests.py`
  - 스킬 폴더의 `tests/cases.yaml`(command / inputs / prompt 사례 + expect) 또는 `evals/evals.json`(skill-creator 형식)을 실행합니다.
  - CLI `skills test`(실패 시 종료 코드 1), 도구 `test_skill`, `POST /api/skills/{name}/test`
- API·화면 버전은 7입니다.

**아직 안 한 것**
- 화면의 동기화·테스트 버튼 (지금은 CLI, 채팅창 `/test_skill`, API)
- 글로 적힌 기대(`expectations`)를 LLM으로 판정하는 것
- 스크립트의 패키지(`requirements.txt`) 자동 설치

## 2026-11-11 — 제품 개발 규칙 학습 (과제 핵심 목표의 첫 구현)

**목표** (사용자 설명)
- 세탁기·건조기 개발 문서와 시스템 데이터에서 사람들이 한 행동을 이해해 규칙을 만듭니다.
- 규칙을 사람에게 묻고, 스킬에 반영하고, 새 행동을 다시 받아 반복할수록 똑똑해지는 에이전트를 만듭니다.
- 데이터: 베이스 `abc.서픽스` → 개발 모델 `def.서픽스`, BOM(품번·설명·레벨, 레벨로 부모를 찾음).

**무엇**
- `product_rules.py`: 개발 건 표와 BOM에서 세 종류의 규칙 후보를 뽑습니다.
  - 바뀌는 부품: 모델명 속성(색상 등)이 바뀌면 바뀌는 부품
  - 개발 현황: 툴별·부품별로 개발된 색상과 품번
  - 베이스 선택 경향
- 도구 6개: `product_rules_learn / show / answer / check / evaluate / export_skill`
- 스킬 `product_rule_loop`: 학습 → 담당자 질문 → 답 기록 → 스킬 반영 → 채점
- 담당자의 답은 다시 학습해도 유지하고, 회차별 정밀도·재현율을 기록합니다.

**아직 안 한 것**
- 사내 시스템(MCP)에서 개발 건·BOM을 자동으로 모으는 단계. 지금은 파일로 받습니다.
- PPT 문서에서 규칙 뽑기와의 연결(기존 규칙집 `rulebook_*`과 통합).
- 향지·스펙 열을 조건으로 쓰는 규칙. 지금은 모델명 속성의 변화만 봅니다.
- 화면의 검토 대기열. 지금은 도구와 채팅으로 합니다.

## 2026-11-10 — 파일 안전장치, 스킬 초안 제안, 참고 효과 측정

**무엇·왜**
- **파일 버전 보관·되돌리기** (opencode `history`)
  - 덮어쓰기 전 원본을 `.versions/`에 남깁니다(10개).
  - `file_versions`, `restore_file_version` 도구를 추가했습니다.
  - 이유: 에이전트가 엑셀·PPT를 잘못 고쳐도 되돌릴 수 있게.
- **읽은 뒤 바뀐 파일 덮어쓰기 막기** (opencode 수정 시각 검사)
  - 공유 폴더 파일을 다른 사람이 그사이 고쳤으면 쓰기를 멈춥니다.
- **작성 후 자동 검증** (opencode 편집 후 LSP 진단)
  - 결과 PPT는 레이아웃, 엑셀은 열림·오류값·빈 시트를 검사해 도구 결과에 붙입니다(`agent.auto_verify`).
  - 이유: 깨진 파일을 사용자에게 넘기지 않고 같은 차례에 고치게.
- **스킬 초안 제안** (AWM·SkillWeaver)
  - 같은 순서로 3번 이상 성공한, 스킬 없는 업무의 `SKILL.md` 초안을 만듭니다.
  - 매번 같은 값은 그대로, 다른 값은 입력값으로, 저장 경로는 파일 이름 입력값으로 바꿉니다.
  - 초안을 실제 데모 MCP로 실행하는 테스트가 있습니다.
- **참고 효과 측정**
  - 참고를 붙인 실행에 `knowledge_hint`를 남깁니다.
  - 사용 이력에서 같은 업무 유형끼리 성공률·도구 왕복·토큰·비용을 비교합니다.

**하지 않은 것 (비용 정책 5장)**
- LLM으로 업무 유형 이름 다듬기, 개념 분류 체계 만들기, 문서 지식 그래프 연결
- 셋 다 토큰 비용이 드는 작업이라, 필요해지면 선택 옵션으로 넣습니다.

**설정**: `agent.auto_verify: true` (새 항목). API·화면 버전은 6입니다(`/api/knowledge/suggest-skills`).

## 2026-11-10 — 업무 지식 그래프(온톨로지)

**무엇**
- `knowledge.py`: 모든 사용자의 실행 기록에서 업무 온톨로지를 만듭니다. LLM 비용은 없습니다.
  - 업무 유형 · 검증된 작업 순서(인자 형태) · 도구 · 사이트 · 개념 · 파일 종류 · 스킬 · 자주 난 오류
  - 관계: `hasConcept`, `solvedBy`, `uses`, `partOf`, `followedBy`(DFG), `produces`, `implementedBy`, `hasPitfall`
  - 횟수와 처음·마지막 본 시각을 붙입니다.
- **자동 참고**: 새 요청이 2번 이상 성공한 업무와 비슷하면 요청 끝에 검증된 순서와 주의할 오류를 붙입니다(`knowledge.auto_hint`).
- **도구**: `knowledge_search`, `knowledge_build`, `knowledge_export`(ttl = RDF/OWL, graphml, json)
- **CLI·API·화면**
  - CLI: `inhouse-agent knowledge …`
  - API: `/api/knowledge`, `/api/knowledge/build`, `/api/knowledge/search`
  - 화면: 상태 탭 "업무 지식"
  - API가 늘어서 `API_VERSION`/`UI_API`를 5로 올렸습니다.

**왜**
- Agent Workflow Memory(ICML 2025) 등에서 성공한 실행의 작업 순서를 기억으로 다시 쓰면 웹 에이전트 성공률이 오른다는 결과가 있습니다.
- 사내에서는 같은 업무를 여러 사람이 반복하므로, 그 경험을 에이전트가 공유하게 했습니다.
- 반복 한도 도달과 도구 오류의 주된 원인인 시행착오를 줄이는 목적입니다.

**설정**: `knowledge:` (enabled, dir, auto_hint, hint_min_score, min_runs, rebuild_minutes) — 기본값으로 켜져 있습니다.

## 2026-11-10 — 출력 한도 장애 수정, 입력 토큰 줄이기 (`87f65a1`)

**장애**: 사내 실행 중 "에이전트 실행 중 오류 … RuntimeError … max_output_tokens"로 실행이 끝남.

**원인**
- 추론 모델(sol 등)은 보이지 않는 추론 토큰도 `max_output_tokens`를 씁니다.
- 한도가 4,096(기본)·8,192(예시 설정)라서 추론만 하다 답 없이 끝났습니다.
- Azure는 `status: incomplete`(사유 `max_output_tokens`)에 `error` 칸까지 채워 보냈고, `parse_response`가 이것을 예외로 바꿨습니다.
- 가짜 서버로 재현했습니다(`tests/test_output_budget.py`).

**변경**
- **잘린 응답 처리**: `incomplete`는 예외가 아닙니다. `status == "failed"`일 때만 예외를 냅니다.
- **`llm.OutputBudget`**: Chat Completions와 Responses 공통입니다. 답 없이 잘리면 다시 요청합니다(최대 2번).
  1. 한도를 상한으로 올리고 그 값을 기억합니다.
  2. 그래도 안 되면 `reasoning_effort`를 한 단계 낮춥니다.
  3. 배포가 "너무 크다"고 거절하면 반으로 줄입니다.
- **끝까지 실패하면**: `output_limit` 오류로 추론 토큰 수와 조치를 보여줍니다(`agent.output_limit_info`, `errors.describe_error`).
- **화면 표시**: "📏 답 없이 출력 한도에 닿아 다시 받았습니다 (…)" 줄이 나옵니다(`output_budget` 이벤트).
- **사용량 기록**: `reasoning_tokens`, `output_retries`를 사용량에 기록합니다.
- **큰 도구 결과**: `max_tool_result_chars`(30,000 → 16,000자)를 넘으면 앞·뒤만 대화에 넣고, 전체는 작업폴더 `tool_results/`에 저장합니다(`Agent._fit_result`).
  - 측정해 보니 한글 결과 하나가 최대 약 27,000 토큰이었고, 이후 모든 요청에 다시 실렸습니다.

**설정 변경 (기존 `config.yaml`을 쓰는 곳은 직접 바꾸세요)**
```yaml
llm:
  max_output_tokens: 25000      # 기본값 4096 → 25000 (OpenAI 권장, 쓴 만큼만 과금)
  max_output_tokens_cap: 64000  # 새 항목: 자동으로 올릴 상한
agent:
  max_tool_result_chars: 16000  # 30000 → 16000
```
예시 설정을 복사해 `max_output_tokens: 8192`가 남아 있어도 자동 복구는 동작합니다. 다만 첫 실패 호출만큼 토큰이 낭비됩니다.

---

## 2026-11-09 — Responses API 모드 (`e7c8565`)

- `llm.api: responses`(기본은 `chat`): 추론 내용(암호화)을 다음 요청에 이어 줍니다(`store:false` + `reasoning.encrypted_content`).
- 같은 배포에만 원본 항목을 다시 보냅니다. 다른 모델 항목은 텍스트·도구 호출만 id 없이 보냅니다(pi 방식).
- 배포가 `include`·`prompt_cache_key`를 거절하면 그 기능만 끕니다.
- 주소는 v1(`/openai/v1/responses`), 날짜 버전, 게이트웨이를 지원합니다. 모델마다 `api`를 따로 줄 수 있습니다.
- `inhouse-agent check`가 모델마다 실제 도구 왕복을 해 보고, 추론 항목을 이어 받았는지 알려줍니다.
- openai 최소 버전을 1.54에서 1.66으로 올렸습니다.

## 2026-11-09 — 압축 요약 양식 (`650ab9c`)

- 요약 양식을 고정했습니다: 목표 / 조건·선호 / 진행(완료·진행 중·막힘) / 결정 / 다음 할 일 / 꼭 필요한 정보 (pi 방식).
- 두 번째 압축부터는 이전 요약을 따로 넘겨 갱신합니다. 예전에는 요약을 다시 요약하며 800자까지 잘릴 수 있었습니다.
- 파일 목록은 기계적으로 모아 누적합니다(도구 메시지 `_files`, 도구 인자 경로).
- 요약 입력의 도구 결과는 2,000자로 자르고, 사용자·에이전트 글은 4,000자까지 넣습니다.

## 2026-11-09 — 도구 묶음 실행 `run_tool_batch` (`55192c9`)

- 검색 → 상세 → 다운로드 같은 연쇄 호출과 `for_each` 항목별 실행을 LLM 왕복 한 번으로 처리합니다(pi codemode를 선언형으로 옮김).
- `select/where/fields/limit`로 줄인 결과가 다음 단계로 갑니다(원본은 `raw`). 중간 결과는 `hide`로 숨깁니다.
- 바깥 워크플로가 묶음 안의 `{steps…}`를 미리 빈 값으로 바꾸던 버그를 고쳤습니다(`workflow.DEFERRED`).
- 설정: `agent.tool_batch: true`

## 2026-11-09 — 프롬프트 캐시 (`89f2830`)

- 대화별 `prompt_cache_key`를 보냅니다.
- 시스템 프롬프트를 대화 중 고정합니다. 분 단위 시각은 사용자 메시지 `_sent`로 옮겼고, 최근 작업 목록은 `_recent`로 고정합니다.
- 마지막 정리에서 도구 목록을 유지하고 `tool_choice: none`만 씁니다.
- `cached_tokens` 집계와 `cost_cached` 단가를 추가했습니다. 예시 단가는 6.1 sol $2/$10/캐시 $0.1, 6.1 terra $1/$5/$0.05, 6 luna $0.1/$0.5/$0.01 (100만 토큰당)입니다.

## 2026-11-09 — 비용 기준 모델 선택 (`e0c9adc`)

- sol/terra/luna 중 난이도에 맞는 가장 싼 모델로 시작하고, 막히면 승격합니다. 응답하지 못하면 다른 모델로 넘어갑니다.
- 요약은 가장 싼 모델로 합니다. 모델별 비용과 절약액을 표시합니다(`llm.models`, `llm.routing`).

## 2026-11-09 — 실행 종료 명확화, 가변 반복 한도 (`dad09cd`)

- 오류를 원인·조치로 분류하고, 화면에 배너와 "다시 시도/계속 진행" 버튼을 보여줍니다.
- 반복 예산은 30에서 시작해 진전이 있으면 10씩 늘려 상한 100까지 갑니다. 되풀이·오류 연속·시간 초과로 일찍 멈추고, 도구 없이 마지막 정리를 합니다.

## 2026-11-09 — 사내 문서 도구 (`0d724ef` ~ `4b2acb3`)

- `fill_excel`, `inspect_excel_template`, 엑셀 데이터 규칙(`excel_rules_*`), `edit_pptx`, `check_pptx_layout`, `render_preview`, 영업일 계산(`business_days`, 한국 공휴일 + 회사 휴무일)을 추가했습니다.

## 2026-11-08 — 보안·압축·하위 에이전트 (`f76c4b2`, `c4ecdd7`, `31ae457`)

- 편집자가 아니거나 `dynamic_tools.mode: off`인데도 `/api/ctools/{name}/test`로 코드를 실행할 수 있던 문제를 막았습니다.
- 토큰 기준 자동 압축, 사용자별 다운로드 귀속(`UserBaton`), `run_python`은 편집자만 쓰도록 했습니다.
- `run_subagent`를 추가했습니다(깊이 1, 사용량 합산).

## 2026-11-05 이전

- 인증 제거와 사용자 ID 필수(`4e9a5fd`)
- 스킬 가져오기: GitHub·zip·폴더(`584dc48`)
- 스킬 스크립트 실행, PPT/엑셀 첨부 분석(`76eada6`)
- 슬래시 명령(`a58a3cc`)
- 규칙집(`1f9ae89`)
- 사용자별 작업폴더·세션(`74656ca`)
- 원격 MCP 파일(`ebd7920`)
- 최초 구성: MCP + Azure OpenAI + CLI/API/스케줄러(`98dad5d`)

자세한 내용은 `git log`를 보세요.
