# inhouse-agent — 사내 사이트 자료수집 에이전트

사내 사이트별로 만들어 둔 **웹컨트롤 MCP 서버**들을 하나로 묶고, **Azure OpenAI** LLM이
사용자 명령이나 **예약작업**에 따라 필요한 MCP tool을 골라 호출하고, 결과를 정리하고 저장하는 에이전트입니다.

> **코드를 고치는 사람·에이전트는 먼저 [`AGENTS.md`](AGENTS.md)를 읽으세요.**
> 지켜야 할 결정 사항과 정책(출력 한도, 프롬프트 캐시, 오류 표시, 보안), 그리고 그 이유가 있습니다.
> 변경 이력과 설정 이전 방법은 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)에 있습니다.

```
 사용자 명령 (CLI / 웹 화면 / REST API)         예약작업 (cron)
                 │                                   │
                 └──────────────┬────────────────────┘
                                ▼
                     Agent (function-calling 루프)
                    ┌───────────┴─────────────┐
                    ▼                         ▼
          Azure OpenAI (배포 모델)        MCPHub ── stdio / http / sse ──▶ 사이트A MCP
                                              │                       ──▶ 사이트B MCP
                                              └─ 내장 tool (현재시각, 파일 저장/조회)
                                ▼
           outputs/<작업명>/<시각>.md, latest.md, _trace/*.json (+ 선택: Teams/Slack 웹훅)
```

## 주요 기능

| 기능 | 설명 |
|---|---|
| 여러 MCP 서버 동시 연결 | `config.yaml`에 등록한 서버를 병렬로 연결합니다. 실패한 서버는 건너뛰고 나머지로 계속 동작합니다. |
| tool 자동 인식 | 각 서버의 tool을 `서버명__tool명` 형태의 LLM function으로 노출합니다. tool이 추가되면 설정을 바꾸지 않아도 바로 반영됩니다. |
| 웹컨트롤 안전장치 | 같은 서버에 대한 호출은 자동으로 순차 실행하고(브라우저 충돌 방지), 서로 다른 서버는 병렬로 실행합니다. 서버별 호출 제한 시간을 둘 수 있고, 서버가 죽으면 자동으로 재시작한 뒤 1회 재시도합니다. |
| 예약작업 | cron 형식으로 등록합니다. 프롬프트에 `{yesterday}`, `{last_week_start}` 같은 날짜 변수를 쓸 수 있습니다. 같은 작업이 겹쳐서 실행되지 않습니다. |
| 결과 저장 | `outputs/<작업명>/`에 md/txt/json 결과와 `latest.*`, tool 입출력 전체 기록(`_trace/`)을 남깁니다. |
| 다운로드 파일 감지 | MCP tool을 호출하기 전과 후의 다운로드 폴더를 비교하고, 결과 텍스트에 적힌 경로와 바이너리(blob)로 받은 파일도 찾아서 자동으로 상태를 점검합니다. |
| 파일 상태 점검 | 다운로드 중(`.crdownload`, 크기 변화, 재다운로드), 빈 파일, 손상(ZIP/PDF 무결성), 의심(엑셀인데 실제로는 로그인 HTML 페이지) 상태를 구분합니다. |
| 파일 분석 | xlsx/xls/HTML표 xls/CSV(cp949)/PDF/DOCX/PPTX/JSON/ZIP을 분석합니다. 표는 제목행을 건너뛰고 열별 통계를 내며, `query_table`로 필터·그룹 집계를 정확하게 계산합니다. |
| 스킬 | `skills/<이름>/SKILL.md`에 업무 절차와 입력값을 정의합니다. 에이전트가 알아서 불러 쓰고, 예약작업에서도 실행할 수 있습니다. |
| 다운로드 URL | MCP 결과에 있는 다운로드 링크를 감지해서 `download_url`로 받습니다. 한글 파일명(`filename*=`, 깨진 인코딩)을 복원하고, 리다이렉트, 사내 프록시/CA, 쿠키/헤더, 사이트별 인증 설정을 지원합니다. 링크인데 로그인 페이지가 받아지면 의심 상태로 표시합니다. |
| 동적 도구 | 필요한 tool이 없으면 에이전트가 파이썬 코드로 tool을 만들고(`create_tool`) 테스트해서 바로 사용합니다. 일회성 계산은 `run_python`으로 처리합니다. 만든 도구는 `ctx.call_tool_data()`로 기존 MCP tool을 조합할 수 있고, `tools/`에 저장되어 계속 재사용됩니다. 승인 모드를 지원합니다. |
| 파일 받기 / 공유 | 답변과 tool 결과에 나온 파일·폴더 경로를 클릭하면 바로 받습니다(폴더는 zip). 🔗를 누르면 API 토큰 없이 열리는 기한부 서명 링크가 복사됩니다. 메일, 메신저, 웹훅, CLI 결과에도 링크가 붙습니다. |
| 범용 자동화 도구 | 공개 자동화 에이전트들의 도구를 분석해서 추가했습니다([분석 문서](docs/agent_tools_analysis.md)): 웹 읽기/검색, HTTP API, SQL, 표 비교, 엑셀·차트·zip 생성, 파일 내용 검색, 기억, 작업 계획, 메일/메신저 알림 |
| 사용자별 작업폴더 / 이어서 하기 | 사용자 ID별로 `workspace/users/<ID>/` 폴더를 따로 씁니다. 대화는 자동으로 저장되어 "내 작업" 목록에서 골라 이어서 진행할 수 있고, 서버를 다시 켜도 유지됩니다. 에이전트가 최근 작업을 기억합니다. |
| 채팅 첨부 (이미지 · PPT · 엑셀) | 📎 버튼, 붙여넣기(Ctrl+V), 끌어놓기로 이미지와 PPT/엑셀/Word/PDF/CSV를 첨부합니다. 엑셀에서 셀 범위를 복사해 붙여넣으면 표 파일로 첨부됩니다. 에이전트가 `compare_documents`로 파일끼리 비교 분석합니다(PPT 장별 차이, 엑셀 합계·행 변경, PPT 숫자↔엑셀 값 교차 확인). 토큰이 없어도 첨부만 하면 자동으로 비교합니다. |
| 스킬 스크립트 | 스킬 폴더의 `scripts/*.py` 등을 `run_skill_script`로 실행합니다(메뉴·보고서 생성 등). `steps`에 `script:`로 적으면 토큰 없이 실행되고, 스킬 폴더는 zip으로 가져올 수 있습니다. |
| 사용 이력 / 모델 목록 | `agent_usage`로 실행 횟수, 스킬·도구별 사용, 토큰, 최근 실행을 조회하고, `list_models`로 사용 중인 모델과 Azure에서 쓸 수 있는 모델을 봅니다. 상태 탭에도 표시됩니다. |
| 과거 자료 학습 / 규칙 | 과거 보고서(PPT/엑셀/Word/PDF)를 장별로 분석해 장별 구성과 규칙을 만듭니다. 새 제품(예: 뮤즈, AI 비서)을 분석해 같은 형식의 PPT를 만들고 규칙으로 검증합니다. 검증 결과와 승인된 결과물로 규칙을 스스로 고쳐 나가고, 버전과 변경 기록을 남깁니다. |
| / 명령 · 토큰 없이 사용 | 첫 화면 채팅창에 `/`를 치면 등록된 스킬과 기능(도구)이 나와 바로 실행됩니다. 스킬에 `steps`(실행 단계)를 적어 두면 LLM이나 토큰 없이 순서대로 실행되고, 도구는 모두 LLM 없이 직접 실행됩니다. |
| 인터랙티브 UI | 스킬을 편집하면 입력 폼이 즉시 바뀌고, 바로 테스트 실행할 수 있습니다. 실행 과정은 실시간으로 표시되고, 에이전트의 질문(`ask_user`)에 화면에서 답할 수 있습니다. 결과를 보고 후속 지시를 내릴 수 있습니다. |
| 사용 방식 | 대화형 CLI, 1회 실행 CLI, 웹 채팅 화면과 REST API(`serve`), 예약 데몬(`scheduler`) |
| SDK 호환 | mcp SDK 1.x와 2.x, openai SDK 1.x 이상에서 테스트했습니다. |

## 1. 설치

Python 3.10 이상이 필요합니다.

```bash
git clone <사내 GitHub>/inhouse_agent.git
cd inhouse_agent
python -m venv .venv
# Windows: .venv\Scripts\activate    /  Linux·Mac: source .venv/bin/activate
pip install -e ".[server]"          # 웹/API 서버까지 쓰는 경우
# pip install -e ".[server,entra]"  # Entra ID(AAD) 인증을 쓰는 경우
```

사내 PyPI 미러만 쓸 수 있으면 `pip install -r requirements.txt --index-url <사내 미러>` 후 `pip install -e . --no-deps`로 설치합니다.

## 2. 설정

```bash
cp .env.example .env                              # Windows: copy
cp config/config.example.yaml config/config.yaml
```

### 2-1. Azure OpenAI (`.env`)

```ini
AZURE_OPENAI_ENDPOINT=https://<리소스이름>.openai.azure.com
AZURE_OPENAI_API_KEY=<키>
AZURE_OPENAI_DEPLOYMENT=<배포 이름>
AZURE_OPENAI_API_VERSION=2024-10-21
```

- `AZURE_OPENAI_DEPLOYMENT`에는 모델명이 아니라 **Azure 포털 또는 Foundry에 표시되는 배포 이름(Deployment name)**을 넣습니다.
  사내에서 쓰는 6.1 sol 모델의 배포 이름을 그대로 넣으면 됩니다.
- 최신 추론형 모델은 `max_completion_tokens`를 사용하고 `temperature`를 지원하지 않습니다. 기본 설정이 이 기준입니다.
  구형 모델이나 구형 api_version을 쓰면 `config.yaml`에서 `max_tokens_param: max_tokens`로 바꾸세요.
- 모델이 `reasoning_effort`를 지원하면 `llm.reasoning_effort: medium`처럼 지정할 수 있습니다.
- APIM 같은 사내 게이트웨이를 거치면 `AZURE_OPENAI_BASE_URL`(…/openai/deployments/<배포이름>)과
  `llm.extra_headers`(예: `Ocp-Apim-Subscription-Key`)를 설정합니다.
- 키 대신 Entra ID(Managed Identity, az login)를 쓰려면 `llm.auth: entra_id`로 설정합니다.

#### 출력 한도(max_output_tokens)와 토큰

추론 모델(sol·terra·luna)은 답하기 전에 **보이지 않는 추론 토큰**을 만듭니다. 이 추론 토큰도 출력 한도(`max_output_tokens`)를 함께 씁니다.

- **문제가 생기는 경우**: 한도가 작으면(예: 8,192) 추론만 하다가 답을 한 글자도 못 쓰고 끝납니다.
  - Responses API에서는 `status: incomplete`(사유 `max_output_tokens`)로 옵니다.
  - 입력이 길수록(큰 도구 결과, 긴 대화) 모델이 더 오래 생각하므로 이런 일이 잦아집니다.
- **기본값**: `max_output_tokens: 25000`. OpenAI가 추론 모델에 권장하는 값입니다.
  - 출력은 정한 만큼이 아니라 실제로 만든 만큼만 과금되므로, 크게 둬도 비용이 늘지 않습니다.
- **자동 복구**: 그래도 답 없이 잘리면 다음 순서로 다시 요청합니다.
  1. 한도를 `max_output_tokens_cap`(기본 64,000)까지 바로 올려 다시 요청합니다. 올린 값은 기억해 다음 요청부터 바로 씁니다.
  2. 상한에서도 잘리면 추론 강도(`reasoning_effort`)를 한 단계 낮춰 한 번 더 요청합니다.
  3. 배포가 "한도가 너무 크다"고 거절하면 반으로 줄여 다시 요청합니다.
  - 화면에는 "📏 답 없이 출력 한도에 닿아 다시 받았습니다 (출력 한도 8,192 → 64,000)"처럼 한 줄로 표시됩니다.
  - 다시 보낸 호출의 토큰도 사용량·비용에 포함됩니다.
- **끝까지 안 될 때**: "모델이 출력 한도(max_output_tokens)에 닿아 답을 끝내지 못했습니다"로 끝나고, 추론에 쓴 토큰 수와 조치(요청 나누기, 추론 강도 낮추기)를 보여 줍니다.

**요청 한 번의 입력 토큰**은 다음으로 이루어집니다(데모 설정에서 측정).

| 항목 | 크기 | 줄이는 방법 |
|---|---|---|
| 시스템 프롬프트 | 약 3,000 토큰 | 대화 중 고정 → 프롬프트 캐시(10% 가격) |
| 도구 정의(75개) | 약 13,500 토큰 | 프롬프트 캐시. 쓰지 않는 서버는 요청에서 빼기(사용할 서버 선택) |
| 도구 결과 | 결과마다 다름 | `max_tool_result_chars`(기본 16,000자)를 넘으면 앞·뒤만 대화에 넣고, 전체는 작업폴더 `tool_results/`에 저장 |
| 이전 대화 | 길어질수록 증가 | 한도의 75%에서 자동 압축 |

- 도구 결과를 모두 대화에 넣으면 이후 모든 요청에 다시 실립니다. 그래서 큰 결과는 파일로 저장하고 에이전트가 필요한 부분만 `read_file_text(path, start, max_chars)`로 다시 읽게 합니다.
- 예전 기본값은 30,000자로, 한글 결과 하나가 약 27,000 토큰이었습니다.

#### Responses API로 쓰기 (추론 내용 이어 받기)

`llm.api: responses`로 두면 Chat Completions 대신 Responses API를 씁니다. opencode·pi가 Azure에서 쓰는 방식입니다.

- **추론 이어 받기**: 추론 모델(sol·terra·luna 등)은 답하기 전에 속으로 추론합니다.
  - Chat Completions에서는 도구 결과를 받을 때마다 그 추론이 사라져 처음부터 다시 생각합니다.
  - Responses API에서는 추론 내용을 암호화된 항목으로 받아 다음 요청에 그대로 다시 보냅니다(`store: false` + `reasoning.encrypted_content`). 서버에 대화를 저장하지 않습니다.
  - 그래서 도구를 여러 번 쓰는 작업에서 같은 생각을 반복하지 않아 추론(출력) 토큰과 헛도는 도구 호출이 줄어듭니다.
- **같은 모델에만**: 추론 항목은 그것을 만든 배포에만 다시 보냅니다.
  - 실행 중에 모델이 바뀌면(승격·다른 모델로 넘어가기) 앞 대화는 텍스트와 도구 호출만 넘깁니다.
  - Chat Completions로 만든 예전 대화도 그대로 이어집니다.
- **주소**:
  - 기본(`responses_api_version: v1`)은 `<리소스>/openai/v1/responses`입니다.
  - 날짜 버전(예: `2025-04-01-preview`)을 쓰면 `<리소스>/openai/responses?api-version=…`입니다.
  - APIM 게이트웨이(`base_url`)를 쓰면 `/openai` 앞부분을 살려 `…/openai/v1`로 갑니다. 주소가 다르면 `llm.responses_base_url`로 직접 지정합니다.
- **자동으로 끄기**: 배포가 추론 내용 반환(`include`)이나 캐시 키를 모른다고 거절하면 그 기능만 끄고 다시 보냅니다.
- **모델마다 다르게**: `llm.models`의 모델마다 `api: responses` / `api: chat`을 줄 수 있습니다.
- **필요한 패키지**: openai 1.66 이상

**켜기 전에 점검**하세요. `inhouse-agent check`를 실행하면 모델마다 실제 도구 왕복(도구 호출 → 결과 → 답)을 해 봅니다.

```text
== Azure OpenAI (Responses API) ==
  ✓ deployment 'sol' 응답: 'OK' usage={... 'reasoning_tokens': 20}
  ✓ 도구 왕복: 답 '42' · 추론 토큰 20, 추론 항목 1개를 이어 받음
```

"추론 항목 1개를 이어 받음"이 나오면 정상입니다. 실패하면 오류 내용이 그대로 나오니 `api: chat`으로 되돌리면 됩니다.

### 2-2. 기존 MCP 서버 등록 (`config/config.yaml`)

```yaml
mcp_servers:
  groupware:
    description: "그룹웨어 - 공지사항, 결재 문서, 일정 조회"   # LLM이 사이트를 고를 때 참고하므로 구체적으로
    transport: stdio                    # 에이전트가 프로세스를 직접 띄움
    command: C:/mcp/groupware/.venv/Scripts/python.exe
    args: ["C:/mcp/groupware/server.py"]
    cwd: "C:/mcp/groupware"
    env: { GW_USER: "${GW_USER}", GW_PASSWORD: "${GW_PASSWORD}" }
    connect_timeout: 90                 # 브라우저 기동이 느리면 늘림
    tool_timeout: 300

  erp:
    description: "ERP - 매출/재고/발주 현황"
    transport: http                     # 이미 떠 있는 Streamable HTTP MCP 서버
    url: http://127.0.0.1:8001/mcp
    headers: { Authorization: "Bearer ${ERP_MCP_TOKEN}" }
    exclude_tools: ["delete_*"]         # 노출하지 않을 tool
```

- 각 MCP 서버가 자기 가상환경을 쓰면 `command`에 **그 가상환경의 python 경로**를 넣어야 합니다.
- MCP tool의 `description`과 파라미터 설명이 좋을수록 LLM이 tool을 정확하게 고릅니다.
  예: "날짜 형식은 YYYY-MM-DD", "최대 조회 기간 92일"
- tool에서 오류를 낼 때는 `ToolError("사유")`를 쓰면 사유가 LLM에 그대로 전달되어 LLM이 인자를 고쳐서 재시도합니다
  (`examples/sample_mcp_server.py` 참고).
- stdio 서버의 stderr 로그는 `logs/mcp_<서버명>.log`에 쌓입니다.

### 2-3. 예약작업

```yaml
schedules:
  - name: daily_notice
    cron: "30 8 * * mon-fri"        # 분 시 일 월 요일  (평일 08:30)
    servers: [groupware]            # 이 작업에서 쓸 서버만 노출 (생략 시 전체)
    output_format: md               # md | txt | json
    webhook_url: ${TEAMS_WEBHOOK_URL:-}
    prompt: |
      {yesterday} 에 등록된 공지사항을 모두 가져와서 표로 정리하고
      save_file 로 notices/{yesterday}.md 에 저장해줘.
```

프롬프트에 쓸 수 있는 날짜 변수: `{today} {yesterday} {tomorrow} {now} {weekday} {this_month} {last_month} {week_start} {week_end} {last_week_start} {last_week_end}`
영업일 변수 (공휴일·회사 휴무일 반영): `{prev_business_day} {next_business_day} {this_month_first_business_day} {this_month_last_business_day} {last_month_last_business_day}`

예약작업 영업일 조건:
- `business_days_only: true`: 주말, 공휴일, 회사 휴무일에는 건너뜁니다.
- `business_day: last`: 그 달의 마지막 영업일에만 실행합니다. `first`, `3`(세 번째), `-2`(뒤에서 두 번째)도 됩니다. cron은 `"0 9 * * *"`처럼 매일로 둡니다.
- 건너뛴 이유는 로그와 서버 창에 나옵니다. 상태 탭의 "지금 실행" 버튼은 조건 없이 바로 실행합니다.

## 3. 사용법

```bash
inhouse-agent check                          # Azure OpenAI와 각 MCP 서버 연결 점검 (가장 먼저 실행)
inhouse-agent tools -v                       # LLM에 노출되는 tool 목록과 스키마

inhouse-agent chat                           # 대화형 모드 (/tools /servers /reset /save /reconnect /exit)
inhouse-agent chat -s groupware,erp          # 사용할 서버 제한
inhouse-agent run "ERP에서 지난주 일별 매출 가져와서 표로 정리해줘" --save weekly_sales
inhouse-agent run -q "..." > result.md       # 결과만 출력

inhouse-agent jobs list -v                   # 예약작업 목록, 다음 실행 시각, 치환된 프롬프트
inhouse-agent jobs run daily_notice          # 예약작업 즉시 1회 실행 (테스트용)
inhouse-agent scheduler                      # 예약작업 데몬 (상시 실행)

inhouse-agent serve --port 8080              # 웹 UI(/)와 REST API, 예약작업을 함께 실행

# 스킬
inhouse-agent skills list
inhouse-agent skills new my_skill -d "언제 쓰는 스킬인지"
inhouse-agent skills validate monthly_sales_report -i month=2026-10   # 형식 검사와 치환된 지침 미리보기
inhouse-agent skills run monthly_sales_report -i month=2026-10 -i dept=전체 -v
inhouse-agent chat --skill monthly_sales_report -i month=2026-10       # 스킬 모드로 대화 ('실행' 입력)

# 파일 (LLM 없이 바로 점검)
inhouse-agent files list "*.xlsx" --recent 60
inhouse-agent files status downloads/매출현황_2026-10.xlsx
inhouse-agent files analyze downloads/매출현황_2026-10.xlsx
inhouse-agent files query downloads/매출현황_2026-10.xlsx --group-by 부서 --agg sum:금액,count
inhouse-agent files wait "*.xlsx"             # 기본 300초 (--timeout 으로 변경)
```

### 웹 UI (`serve` 실행 후 http://<서버>:8080/)

| 탭 | 기능 |
|---|---|
| 조사 | 자유롭게 요청합니다. tool 호출, 인자, 결과, 소요 시간, 감지된 파일 상태가 실시간으로 표시됩니다. 에이전트가 질문하면 선택지 버튼이나 직접 입력으로 답합니다. 실행 중에 중단할 수 있습니다. |
| 스킬 | 왼쪽에서 스킬을 고르고 가운데에서 SKILL.md를 편집합니다. 편집하는 동안 형식을 검사하고 **오른쪽 입력 폼을 즉시 다시 그립니다**. "저장하고 실행"으로 테스트하고, 결과를 본 뒤 "이어서 지시"로 같은 스킬 대화를 이어갑니다. 저장할 때마다 이전 버전을 보관하고 되돌릴 수 있습니다. 지난 테스트 결과도 볼 수 있습니다. |
| 파일 | 다운로드/업로드/저장 폴더를 탐색합니다. 상태 점검과 분석은 LLM 없이 즉시 실행됩니다. 파일을 업로드하거나 "에이전트에게 요청"으로 조사 탭에 넘길 수 있습니다. |
| 상태 | MCP 서버 연결 상태와 재연결, 예약작업 목록과 즉시 실행, 최근 실행 기록을 봅니다. |

`python -m inhouse_agent ...`으로도 실행할 수 있습니다.

## 4. 파일 다운로드, 상태 점검, 분석

웹컨트롤 MCP가 파일을 받는 세 가지 방식을 모두 처리합니다.

| 방식 | 에이전트 처리 |
|---|---|
| 브라우저가 다운로드 폴더에 저장 (비동기) | tool 호출 전후의 감시 폴더를 비교해 새 파일을 감지합니다. 아직 받는 중이면 LLM에 `wait_for_download`를 쓰라고 알려줍니다. |
| tool 결과 텍스트에 저장 경로를 적음 | 경로를 찾아 상태를 점검합니다. |
| tool 결과에 파일 내용을 blob/image로 담아 보냄 | `workspace/downloads/<서버>/`에 저장하고 상태를 점검합니다. |

점검 결과는 tool 결과 뒤에 `[파일 감지 - 에이전트 자동 점검]`으로 붙어 LLM에 전달되고, 웹 UI에는 배지로 표시됩니다.

| 상태 | 의미 |
|---|---|
| ✅ 정상 `complete` | 확장자와 실제 형식이 같고 무결성 검사를 통과했습니다. |
| ⏳ 다운로드 중 `downloading` | `.crdownload`/`.part` 임시파일이 있거나, 크기가 변하는 중이거나, 같은 이름으로 다시 받는 중입니다. |
| ⚠️ 의심 `suspicious` | 확장자와 내용이 다릅니다. 예: `.xlsx`인데 실제로는 HTML이고 '로그인' 문구가 있어 세션 만료 페이지로 보입니다. |
| ❌ 손상 `corrupt` | ZIP 구조가 깨졌거나 PDF 끝(`%%EOF`)이 없습니다. 다운로드가 중간에 끊긴 경우입니다. |
| ⚠️ 빈 파일 / ❌ 없음 / 잠김 / 시간 초과 | 이름 그대로입니다. |

**권장 설정**: stdio MCP 서버에는 `AGENT_DOWNLOAD_DIR` 환경변수(= `workspace/downloads`)가 자동으로 전달됩니다.
웹컨트롤 MCP의 브라우저 다운로드 경로를 이 값으로 맞추면 감지가 가장 정확합니다.
예: Playwright `accept_downloads` 후 `download.save_as(os.environ["AGENT_DOWNLOAD_DIR"] + "/" + download.suggested_filename)`
브라우저 기본 다운로드 폴더를 그대로 쓰면 `config.yaml`의 `files.watch_dirs`에 추가하세요.

에이전트가 쓰는 내장 tool:

| tool | 용도 |
|---|---|
| `file_status` | 파일 1개의 상태를 상세히 점검합니다. |
| `wait_for_download` | 패턴에 맞는 파일이 완료될 때까지 기다립니다 (기본 300초, `files.wait_timeout`). |
| `fetch_remote_file` | 원격 MCP 서버 PC의 파일을 가져옵니다 (원격 서버가 있을 때만). |
| `list_downloads` | 최근 파일 목록을 보여줍니다. |
| `analyze_file` | 구조, 통계, 샘플 행, 문서 텍스트를 뽑습니다. |
| `query_table` | 필터, 정렬, 그룹 집계를 정확하게 계산합니다. |
| `read_file_text` | 긴 문서를 구간별로 이어서 읽습니다. |
| `extract_zip` | ZIP을 풉니다 (한글 파일명 지원, zip slip 차단). |
| `save_file` | 결과를 저장합니다. |
| `ask_user` | 사용자에게 확인을 받습니다. |
| `use_skill` / `read_skill_file` | 스킬 지침과 참고 파일을 불러옵니다. |

파일 접근은 workspace, 감시 폴더, `extra_read_dirs`, outputs 안으로만 제한되고, 쓰기는 workspace 안에서만 됩니다.

### 원격 MCP 서버(다른 PC)의 파일 받기

MCP 서버가 다른 PC에서 돌면, 그 서버가 알려주는 경로(`C:\mcp\downloads\a.xlsx`)와 `http://localhost:9000/...` 링크는
에이전트 PC에서 그대로 쓸 수 없습니다. 에이전트는 다음과 같이 처리합니다.

- **원격 서버 판단**: `remote: auto`(기본)면 `url`의 호스트가 localhost나 자기 PC가 아닐 때 원격 서버로 봅니다.
- **결과에 원격 경로가 나오면 자동으로 파일을 가져옵니다**. 아래 순서로 시도하고, 받은 파일은 `workspace/downloads/<서버이름>/`에 저장한 뒤 상태를 점검합니다.
  1. `path_map`: 원격 경로를 공유폴더(SMB)나 URL로 바꿉니다. 공유폴더는 다운로드 감시 폴더에 자동으로 들어가므로 `wait_for_download`도 됩니다.
  2. `fetch_tool`: 원격 MCP의 "파일 내용 돌려주기" tool을 호출합니다 (EmbeddedResource blob 또는 `{"base64": ...}`).
  3. `use_resources`: MCP `resources/read`로 `file:///경로`를 읽습니다.
- **localhost 링크**: 결과의 `http://localhost:포트/...`를 MCP 서버 주소로 바꾼 뒤 `[다운로드 링크 감지]`에 보여줍니다.
- **나중에 생기는 파일**: 원격에서 아직 받는 중이거나 나중에 생긴 파일은 `fetch_remote_file(server, path, wait_sec)`로 가져옵니다.
- **설정이 없을 때**: 가져올 방법이 없으면 이유와 필요한 설정(`remote_files`)을 결과에 알려줍니다. 원격 서버인데 "로컬 폴더를 기다리라"는 안내는 하지 않습니다.

설정 예는 `config/config.example.yaml`의 `groupware_remote`를 참고하세요.

> ⚠️ **mcp SDK 1.x(FastMCP)로 만든 MCP 서버를 다른 PC에서 접속하면 `421 Misdirected Request`가 날 수 있습니다.**
> FastMCP를 기본 host(127.0.0.1)로 만들면 DNS rebinding 보호가 켜져서 `localhost` Host만 허용하기 때문입니다.
> MCP 서버 쪽에서 접속에 쓰는 주소를 허용하세요.
> ```python
> from mcp.server.transport_security import TransportSecuritySettings
> mcp = FastMCP("gw", host="0.0.0.0", port=8001,
>               transport_security=TransportSecuritySettings(
>                   enable_dns_rebinding_protection=True, allowed_hosts=["10.0.0.5:*", "mcp.company.com:*"]))
> ```
웹컨트롤 MCP를 직접 고칠 수 있다면 **`fetch_tool`을 하나 만들어 두는 방법이 가장 간단하고 확실합니다** (`examples/sample_mcp_server.py`의 `read_server_file` 참고).

## 5. 스킬

`skills/<이름>/SKILL.md`. 같은 폴더의 다른 파일(양식, 기준표)은 `read_skill_file`로 참조합니다.

```markdown
---
name: monthly_sales_report
description: ERP 월 매출 엑셀을 받아 부서별/제품별로 집계한다. "월 매출" 요청에 사용.   # 에이전트가 고르는 기준
servers: [erp]                 # (선택) 이 스킬에서 쓸 MCP 서버
inputs:                        # (선택) 웹 UI 입력 폼으로 표시
  - name: month
    label: 대상 월
    type: string               # string | text | number | date | select | boolean | file
    default: "{last_month}"    # 날짜 변수 사용 가능
    required: true
  - name: dept
    type: select
    options: [전체, 인사팀, IT운영팀]
    default: 전체
---
# 절차
1. download_sales_excel 로 {month} 엑셀을 받는다.
2. [파일 감지] 결과를 보고, 다운로드 중이면 wait_for_download ...
3. query_table 로 집계한다. 숫자는 추측하지 않는다.
```

- **자동 사용**: 조사 탭에서 요청이 스킬 설명과 맞으면 에이전트가 `use_skill`로 지침을 불러와 따릅니다.
- **직접 실행/테스트**: 웹 UI의 스킬 탭, `inhouse-agent skills run`, `chat --skill`로 실행합니다. 결과는 `outputs/skill_<이름>/`에 저장됩니다.
- **예약작업**: `schedules`에 `skill: monthly_sales_report`와 `inputs: {month: "{last_month}"}`를 적으면 됩니다. 예약작업은 사람이 없으므로 `ask_user`가 기본값으로 진행합니다.
- 샘플 스킬 `monthly_sales_report`와 `notice_digest`가 데모 MCP 서버로 바로 동작합니다.

### REST API (`serve`)

별도 인증(API 토큰) 없이 호출합니다. 사용자별 작업폴더를 쓰려면 `X-Agent-User: <ID>` 헤더(또는 `?user=<ID>`)를 붙이세요.

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/runs` | 실행을 시작합니다. `{"prompt", "session_id", "servers", "skill", "inputs", "new_session"}`를 보내면 `{id, session_id, ...}`를 돌려줍니다. |
| GET | `/api/runs/{id}/events` | 실행 이벤트 스트림(SSE)입니다: start, llm_start, tool_start, tool_end(files 포함), ask, answer, done |
| POST | `/api/runs/{id}/answer` | `ask_user` 질문에 답합니다: `{"answer": "..."}` |
| POST | `/api/runs/{id}/cancel` | 실행을 중단합니다. |
| POST | `/api/chat` | 동기 방식으로 실행 1건을 처리합니다 (기존 호환). |
| GET/POST/PUT/DELETE | `/api/skills[/{name}]` | 스킬 목록, 조회, 생성, 저장(검증과 이전 버전 백업), 삭제 |
| POST | `/api/skills/validate` | 저장하지 않고 SKILL.md 형식만 검사합니다. |
| GET | `/api/files`, `/api/files/status`, `/api/files/analyze`, `/api/files/download` | 파일 목록, 상태, 분석, 내려받기 |
| POST | `/api/files/upload?name=` | 파일을 업로드합니다 (요청 body가 파일 내용). |
| DELETE | `/api/chat/{session_id}` | 대화 초기화 |
| GET | `/api/servers` | MCP 서버 상태 |
| POST | `/api/servers/{name}/reconnect` | MCP 서버 재연결 |
| GET | `/api/tools` | tool 목록 |
| GET | `/api/jobs` | 예약작업과 다음 실행 시각 |
| POST | `/api/jobs/{name}/run` | 예약작업 즉시 실행 |

### 상시 운영

- **Windows**: 작업 스케줄러에 "로그온 시 실행"으로 `C:\path\inhouse_agent\.venv\Scripts\inhouse-agent.exe serve`를 등록하거나 NSSM으로 서비스에 등록합니다.
  웹컨트롤이 화면이 있는 브라우저를 쓰면 서비스(세션 0) 대신 **로그온한 사용자 세션**에서 실행해야 합니다.
- **Linux**: systemd 유닛의 `ExecStart=/opt/inhouse_agent/.venv/bin/inhouse-agent serve`, `WorkingDirectory=/opt/inhouse_agent`

### 스킬 가져오기 (GitHub · zip · 폴더 · 다른 에이전트용 스킬)

스킬 탭의 **📦 가져오기**를 누르면 아래 방법 중 하나로 원본을 불러옵니다. 원본 안의 스킬 목록이 나오면 골라서 설치합니다.

| 방법 | 넣는 것 | 갱신 |
|---|---|---|
| GitHub / 사내 Git 주소 | `https://github.com/org/repo`, `…/tree/<브랜치>/<폴더>`, `…/blob/<브랜치>/<폴더>/SKILL.md`, GitHub Enterprise 주소, `git@…`, `file://` | 🔄 원본에서 업데이트 |
| zip / SKILL.md | GitHub **Code → Download ZIP** 파일을 그대로 올리거나 끌어놓기. SKILL.md 한 개도 가능 | 다시 가져오기 |
| 폴더 선택 | 브라우저에서 스킬 폴더나 저장소 폴더를 선택 | 다시 가져오기 |
| 서버 PC 폴더 | 에이전트 서버 PC에 있는 폴더 경로 (예: git으로 받아 둔 사내 스킬 저장소) | 🔄 원본에서 업데이트 |

- **여러 스킬을 한 번에**: `.agents/skills/`, `.claude/skills/`, `skills/` 등 어디에 있든 SKILL.md가 있는 폴더를 모두 찾습니다.
  - `.claude/skills/*`처럼 같은 스킬을 가리키는 링크(또는 링크 대신 생긴 텍스트 파일)는 한 번만 셉니다.
  - 스킬 폴더 안에 중첩된 예시 SKILL.md는 무시합니다.
- **Claude Code / Codex / Copilot / Gemini용 스킬도 그대로 동작합니다** (Agent Skills 형식):
  - `allowed-tools`, `metadata` 같은 머리말을 그대로 둡니다.
  - `description: Create a plan: scope, …`처럼 값 안에 `: `가 있는 머리말도 읽습니다.
  - `ui.json`의 `inputs`는 입력 폼으로 보여줍니다.
  - 본문의 `<this skill>`, `<skills dir>`, `.agents/skills/` 경로는 이 에이전트의 스킬 폴더로 바뀝니다.
  - 본문에 적힌 명령줄(`python3 …/scripts/view.py home`)은 `run_skill_script`의 `command`로 그대로 실행됩니다. 다른 PC의 절대경로여도 경로 속 스킬 이름으로 찾습니다.
  - `steps`가 없어도, 본문에 자리표시(`<MODEL>` 등)가 없는 명령이 있으면 그 명령으로 **토큰 없이 시작**합니다.
- **화면 문서(JSON view)**: 스크립트가 `{"view", "sections", "form", "actions"}` 형식을 출력하면 채팅에 클릭할 수 있는 메뉴와 입력 폼으로 보여줍니다.
  - 항목을 누르면 다음 명령이 실행되고, 폼은 `--set "이름=값"`으로 제출됩니다. ▶ 버튼은 해당 스킬을 실행합니다.
  - LLM이 없어도 메뉴를 따라갈 수 있습니다. LLM에는 짧은 텍스트로 요약해 넘겨 토큰을 아낍니다.
- **스크립트 환경변수** `skills.script_env`:
  - 쓸 수 있는 자리표시: `{user}`(현재 사용자 ID), `{skills_dir}`, `{skill_dir}`, `{data_dir}`, `{workspace}`, `{output_dir}`
  - 기본값: model-agents 스킬용으로 `MODEL_AGENTS_USER={user}`, `MODEL_AGENTS_SKILL_ROOTS={skills_dir}`가 들어 있습니다. 그래서 사용자마다 자기 모델과 이력을 봅니다.
- **갱신**: Git이나 서버 폴더에서 가져온 스킬은 출처(주소, 브랜치, 커밋)가 기록됩니다.
  - **🔄 원본에서 업데이트** 또는 `inhouse-agent skills update --all`로 새 버전을 받습니다. 커밋이 같으면 "이미 최신"으로 끝납니다.
  - 스킬이 실행 중에 만든 `data/` 폴더, `*.db`/`*.sqlite` 파일, 편집 기록은 유지됩니다.
- **비공개 저장소**:
  - 설정: `skills.git_token`, 호스트별 `skills.git_tokens: {github.company.com: …}`, 또는 `AGENT_GIT_TOKEN`/`GITHUB_TOKEN` 환경변수
  - 먼저 토큰 없이 시도하고, 실패하면 토큰으로 다시 받습니다.
  - git이 설치되어 있으면 `git clone`을 쓰고(사내 Git 인증 설정 사용), 없으면 GitHub 압축 파일로 받습니다(`downloads.verify`의 사내 CA 사용).
  - 주소의 브랜치가 없으면 있는 브랜치 목록을 보여줍니다.
- **CLI**:
  ```bash
  inhouse-agent skills import https://github.com/tommyhj217/skill_test --list   # 들어 있는 스킬만 보기
  inhouse-agent skills import https://github.com/tommyhj217/skill_test          # 전부 설치
  inhouse-agent skills import skill_test-main.zip --only model-agents --overwrite
  inhouse-agent skills update --all
  ```
- 가져오기와 업데이트는 편집자(`skills.editors`)만 할 수 있고, 설치한 스킬은 모든 사용자의 `/` 목록에 바로 보입니다.

> **"Method Not Allowed"(405)가 뜨면**: 서버 코드가 화면보다 이전 버전이라는 뜻입니다.
> - 원인: 웹 화면(index.html)은 바로 새 파일을 읽지만, API는 서버를 다시 켜야 바뀝니다. 그래서 `git pull` 뒤 서버를 재시작하지 않으면 새 기능의 API가 없습니다.
> - 해결: 서버 창에서 Ctrl+C를 누르고 다시 실행하세요. 이제는 화면 맨 위에 빨간 경고도 나옵니다.

## 6. 다운로드 URL 받기

MCP가 파일 대신 링크를 돌려주면(`파일 다운로드 주소: https://.../download?fileId=...`), 에이전트가 결과 뒤에
`[다운로드 링크 감지]`를 붙여 LLM에 알려주고, LLM이 `download_url`로 받은 뒤 상태 점검 결과를 확인합니다.
`downloads.auto_download: true`로 설정하면 링크를 바로 자동으로 받습니다.

| 상황 | 처리 |
|---|---|
| 한글 파일명 | `filename*=UTF-8''…`, latin-1로 깨진 UTF-8/CP949, `%` 인코딩, `?fileName=` 쿼리에서 복원합니다. |
| 사내 프록시 / 인증서 | `HTTP(S)_PROXY`/`NO_PROXY` 환경변수를 사용합니다. `downloads.verify`에 사내 CA 경로를 지정합니다. |
| 로그인이 필요한 링크 | 쿠키 없이 받은 결과가 로그인 HTML이면 ⚠️ 의심으로 표시합니다. LLM은 MCP에서 세션 쿠키를 얻는 tool이 있으면 `cookies` 인자로 다시 받습니다. 고정 토큰과 쿠키는 `downloads.sites`에 사이트별로 설정합니다. |
| 안전장치 | http/https만 허용하고, `allowed_hosts`/`blocked_hosts`로 호스트를 제한하며, 링크로컬(169.254.x) 주소를 차단합니다. 크기 제한이 있고, `.part`로 받다가 완료되면 이름을 바꿉니다. |

```bash
inhouse-agent fetch "https://erp.company.com/file/download?id=123" --cookies "JSESSIONID=..."
```
웹 UI 파일 탭의 "URL로 받기"로도 받을 수 있습니다.

## 7. 동적 도구 (tool이 없을 때 직접 만들어 실행)

에이전트가 필요한 tool이 없다고 판단하면 다음 두 가지로 처리합니다.

- **`run_python`**: 일회성 계산, 변환, 병합. 코드는 `workspace/_scripts/`에 감사 기록으로 남습니다.
- **`create_tool`**: 반복해서 쓸 기능을 만듭니다. 처리 순서는 다음과 같습니다.
  1. 정적 검사
  2. (approve 모드) 사용자 승인. 조사/스킬 화면에 코드와 함께 승인/거부 버튼이 표시됩니다.
  3. `test_args`로 테스트 실행. 실패하면 traceback을 LLM에 돌려주고, LLM이 코드를 고쳐 다시 시도합니다.
  4. `tools/<이름>.py`로 저장하고 `custom__<이름>`으로 등록합니다. 같은 실행 안에서 바로 호출할 수 있습니다.

도구 코드 형식:

```python
def run(args, ctx):
    rows = ctx.call_tool_data("erp__get_daily_sales", {"date_from": "2026-10-01", "date_to": "2026-10-31"})  # MCP tool → dict/list
    files = ctx.call_tool_files("download_url", {"url": args["url"]})   # 받은 파일 [{path, state, ...}]
    from inhouse_agent import analyzers
    t = analyzers.load_tables(files[0]["path"])[0]                       # 엑셀/CSV → Table(columns, rows)
    total = sum(analyzers.to_number(r[t.columns.index("금액")]) or 0 for r in t.rows)
    ctx.log("진행 로그")                                                    # stderr 로그
    return {"total": total, "rows": len(rows)}
```

`ctx`가 제공하는 것: `workspace`, `download_dir`, `files_dir`, `path('downloads/x.csv')`, `call_tool()`(텍스트), `call_tool_data()`(구조화), `call_tool_files()`, `log()`

**실행 환경과 안전장치**

| 항목 | 내용 |
|---|---|
| 실행 위치 | 별도 파이썬 프로세스 (`python -I`). 작업폴더는 workspace입니다. |
| 제한 시간 | `dynamic_tools.timeout` |
| 환경변수 | 최소한만 전달합니다. Azure 키 등은 넘어가지 않고, 필요한 변수만 `env_passthrough`에 지정합니다. |
| 정적 검사 | `subprocess`/`ctypes`/`pickle` import, `os.system`/`os.remove`/`shutil.rmtree`/`eval`/`exec` 호출을 차단합니다. 목록은 설정할 수 있습니다. |
| 도구 안에서 못 부르는 tool | `create_tool`, `run_python`, `ask_user`, 다른 `custom__` 도구 |
| 승인 모드 | `dynamic_tools.mode`: `approve`(기본, 사용자 승인), `auto`(자동), `off`(사용 안 함). 예약작업처럼 사람이 없는 실행에서는 approve 모드일 때 새 도구를 만들지 않고, 이미 저장된 도구만 씁니다. |

> ⚠️ 이 장치는 프로세스 격리, 정적 검사, 승인 절차를 합친 것이고, **완전한 보안 샌드박스는 아닙니다**.
> 신뢰할 수 있는 사내망에서만 운영하세요. 웹 화면은 인증 없이 접속되므로, 사내망 밖으로 포트를 열지 마세요.
> 웹 UI의 도구 탭에서는 코드를 직접 실행할 수 있습니다. 운영 환경에서는 `approve` 모드를 권장합니다.

도구 관리:

| 방법 | 기능 |
|---|---|
| 웹 UI **도구** 탭 | 목록을 보고 코드, 설명, 인자 스키마를 편집합니다. 편집하는 동안 정적 검사가 돌고, 편집 중인 코드로 바로 테스트하며, 저장(이전 버전 `.history` 보관)과 삭제를 합니다. |
| CLI | `inhouse-agent ctools list`, `ctools show <이름>`, `ctools test <이름> --args '{...}'`, `ctools delete <이름>` |

검토한 도구는 `tools/` 폴더째 GitHub에 커밋해서 다른 서버와 공유할 수 있습니다.

## 8. 사용자별 작업폴더 / 이전 작업 이어서 하기 / 이미지

**사용자 ID**

- 웹 화면 오른쪽 위 👤에 사번이나 사내 ID를 입력합니다. 브라우저에 기억되어 다음 접속에도 유지됩니다.
- CLI는 `inhouse-agent -u <ID> chat`처럼 씁니다.
- 한글 ID도 됩니다. 쓸 수 있는 문자는 한글/영문/숫자와 `. _ - @`입니다.
- `users.require: true`로 설정하면 ID 없이는 쓸 수 없고, `users.allowed`로 허용할 ID 패턴을 제한할 수 있습니다.

**작업폴더**: `workspace/users/<ID>/`

```
downloads/  uploads/images/  uploads/attachments/  files/  results/  sessions/  memory.json
```

- 모든 도구(파일 저장·분석·다운로드·기억·동적 도구 실행 폴더)가 이 사용자의 폴더를 씁니다. 다른 사용자의 파일은 열 수 없습니다.
- MCP 연결, 스킬, 동적 도구는 모두가 공유합니다.
- MCP가 공용 다운로드 폴더(`AGENT_DOWNLOAD_DIR`)에 저장한 파일은, 다운로드가 끝나면 요청한 사용자의 `downloads/`로 옮겨집니다 (`users.claim_downloads: move | copy | none`).
- 예약작업에 `user: <ID>`를 넣으면 그 사용자 폴더에서 실행되고, 결과는 `users/<ID>/results/`에 저장됩니다.

**이어서 하기**

- 대화는 요청마다 `sessions/<작업ID>.json`에 저장됩니다. 저장되는 내용은 LLM 대화 이력, 요청/결과/사용 tool/파일/첨부 이미지입니다.
- 웹: 조사 탭 왼쪽 **내 작업**에서 검색, 이름 바꾸기, 삭제를 하고, 작업을 클릭하면 이전 대화가 다시 표시됩니다. 그대로 이어서 요청하면 이전 대화와 파일을 기억한 채로 계속합니다.
- CLI: `inhouse-agent -u kim sessions`로 목록을 보고, `inhouse-agent -u kim chat --resume <작업ID>`로 이어서 합니다.
- 에이전트는 시스템 프롬프트로 이 사용자의 최근 작업 목록을 받습니다. "지난번 그 보고서"처럼 요청하면 `list_past_work`/`read_past_work`로 이전 작업 내용을 찾아 이어서 합니다.

**채팅 이미지**

- 📎 버튼, 붙여넣기(Ctrl+V, 화면 캡처), 채팅창에 끌어놓기로 첨부합니다. 한 번에 최대 10장이고, 썸네일을 클릭하면 크게 보입니다. PPT·엑셀 첨부는 [12장](#12-첨부-파일-비교-분석--스킬-스크립트--사용-이력)을 보세요.
- 이미지는 `uploads/images/`에 저장되고, 긴 변 2048px로 줄여 비전 모델에 보냅니다.
- 다음 요청부터는 이전 이미지를 경로 텍스트로 바꿔서 보내 토큰을 아낍니다. 다시 봐야 하면 에이전트가 `view_image`로 불러옵니다.
- `view_image`로는 MCP 화면 캡처나 다운로드한 이미지도 에이전트가 직접 볼 수 있습니다.
- 배포 모델이 이미지를 지원하지 않으면 `llm.vision: false`로 설정하세요. 그러면 이미지 대신 안내 문구를 보냅니다.

## 9. 내 PC 에서 다른 사용자들에게 서비스하기

내 PC에서 서버를 켜면 같은 사내망의 다른 사람들이 **브라우저만으로** 접속해 각자 ID로 스킬을 씁니다.

**1. 서버 켜기**

| 환경 | 방법 |
|---|---|
| Windows | `scripts\start_server.bat` 더블클릭. 처음이면 가상환경 설치와 설정 파일 복사까지 자동으로 합니다. 포트 지정: `start_server.bat 8080` |
| 리눅스/맥 | `scripts/start_server.sh` |
| 직접 실행 | `inhouse-agent serve --host 0.0.0.0 --port 8080` |

켜면 다른 사람이 쓸 주소가 표시됩니다.

```
  이 PC 에서:      http://localhost:8080
  다른 PC 에서:    http://10.20.30.40:8080
  (PC 이름으로:    http://MY-PC:8080)
```

`server.public_url`을 비워 두면, 공유 링크(메일·메신저·웹훅)에도 이 주소가 자동으로 들어갑니다.

**2. 방화벽 열기** (Windows, 한 번만)

`scripts\windows_firewall.bat`을 **관리자 권한으로 실행**하면 TCP 8080 포트가 도메인/개인 네트워크에 허용됩니다.

**3. 사용자들은**

- 브라우저로 위 주소에 접속하고, 오른쪽 위 👤에 자기 ID를 입력합니다. 작업폴더, 대화 기록, 기억은 사람마다 따로 저장됩니다.
- 여러 사람이 동시에 다른 사이트에서 파일을 받아도 섞이지 않습니다. 공용 다운로드 폴더를 비교해 새 파일을 찾는 동안에는 사용자별로 순서를 지킵니다. 같은 사람의 호출은 동시에 진행되고, 다른 사람이 기다리면 차례를 넘깁니다.
- 스킬 편집과 코드 실행(`run_python`, 도구 만들기, 도구 탭의 테스트 실행)은 `skills.editors`에 적은 사람만 할 수 있습니다. 비워 두면 모두 가능하므로, 여러 사람에게 서비스할 때는 담당자 ID를 적으세요. 비어 있으면 서버 시작 화면과 상태 탭에 경고가 나옵니다.
- 첫 화면에 **사용할 수 있는 스킬 카드**가 보이고, 입력창에 **`/`**를 치면 스킬 목록이 나옵니다. 스킬을 고르면 입력 카드가 열리고 바로 실행됩니다.
  - 기능(도구)은 목록 맨 아래 "기능(도구) N개 보기"를 누르거나 이름을 입력하면 함께 나옵니다.

**4. 스킬 공유와 편집 권한**

- 스킬은 `skills/` 폴더 하나를 모두가 같이 씁니다. 누가 스킬 탭에서 추가하거나 고쳐도 다른 사람의 `/` 목록과 첫 화면 카드에 바로 보입니다(목록은 10초마다 새로 받음).
- 각 스킬에는 만든 사람, 고친 사람, 날짜가 표시되고, 최근 3일 안에 화면에서 추가된 스킬에는 "새 스킬"이 붙습니다.
- 여러 사람이 쓰면 `config.yaml`의 `skills.editors`로 편집할 사람을 정하세요(예: `["kim.hj", "admin*"]`).
  - 그 외 사용자는 스킬과 동적 도구를 **보기·실행만** 할 수 있습니다.
  - 편집 버튼은 꺼지고, API도 403으로 막히며, 에이전트의 `create_tool`도 막힙니다.
- GitHub로 받은 스킬도 같은 폴더에 두면 그대로 보입니다.

**5. 운영 팁**

- PC가 절전 모드에 들어가면 서비스가 끊깁니다. Windows 전원 설정에서 "절전 안 함"으로 두세요.
- 부팅할 때 자동으로 켜려면 **작업 스케줄러**에 `scripts\start_server.bat`을 "로그온할 때"로 등록합니다.
- 별도 인증(API 토큰, 비밀번호)은 없습니다. 주소로 접속하면 바로 쓰고, 처음 접속할 때 사용자 ID를 입력하면 그 ID의 작업폴더(`workspace/users/<ID>/`)를 씁니다.
  - 같은 ID로 다시 접속하면 이전 작업을 이어서 합니다. ID는 오른쪽 위 👤에서 바꿉니다.
  - 허용할 ID를 제한하려면 `users.allowed`로 ID 패턴을 정합니다.
- MCP 서버(웹컨트롤)는 여러 사용자가 같이 씁니다. 같은 사이트에 대한 호출은 자동으로 순서대로 처리되고, 받은 파일은 요청한 사람의 폴더로 옮겨집니다.

**CLI**: `inhouse-agent chat`에서 `/`만 입력하면 스킬 목록이 나오고, `/스킬이름 key=value`로 실행하며, `/도구이름 key=value`로 기능을 직접 실행합니다.

## 10. / 명령과 LLM 없이(토큰 없이) 쓰기

**채팅창 `/` 메뉴** (조사 탭)

- `/`를 치거나 🧩 버튼을 누르면 목록이 나옵니다.
  - **스킬**: "토큰 없이 가능" / "LLM 필요"가 표시됩니다.
  - **명령**: `/new`, `/help`
  - **기능(도구)**: 모든 tool
- 글자를 치면 걸러지고, ↑↓로 고른 뒤 Enter/Tab으로 선택합니다. Esc는 닫기입니다.
- 선택하면 **입력 카드**가 열립니다. 스킬 입력 폼이나 도구 인자 폼이 나오고, 실행 방식을 고릅니다: 자동(LLM 있으면 에이전트, 없으면 워크플로) / 워크플로 / 에이전트.
- 한 줄로 바로 실행할 수도 있습니다: `/monthly_sales_report month=2026-10 dept=전체`, `/analyze_file path=downloads/a.xlsx`, `/query_table path=... group_by=["부서"]`
  - 필수 입력이 빠지면 카드가 열립니다.
- 스킬 실행은 "내 작업"에 따로 저장됩니다. 도구 실행은 지금 대화에 이어서 기록됩니다.

**LLM 없이(토큰 없이) 쓰기**: 사내 테스트용

- `llm.provider: none`으로 설정하거나 Azure 키가 없으면, 화면 위에 "LLM 없음"이 표시됩니다.
- 이 상태에서는 다음처럼 동작합니다.
  - 도구(기능)는 모두 실행됩니다.
  - `steps`가 있는 스킬도 실행됩니다.
  - 자유 질문에는 사용법을 안내합니다.

**스킬 워크플로** (`SKILL.md` 머리말)

```yaml
steps:
  - id: dl
    title: 매출 엑셀 다운로드
    tool: demo__download_sales_excel
    args: {month: "{month}", wait: true}
  - id: one
    tool: query_table
    when: "{dept} != 전체"            # 조건이 맞을 때만
    args: {path: "{steps.dl.file}", filters: [{column: 부서, op: "==", value: "{dept}"}], group_by: [제품]}
    on_error: continue                # 실패해도 계속 (기본 stop)
output: |
  ## {month} 매출
  {steps.one.text}
```

- 쓸 수 있는 변수:
  - 입력값 `{입력이름}`, 날짜 `{yesterday}` `{last_month}` 등
  - 이전 단계 결과 `{prev}`, `{steps.<id>.text}`, `{steps.<id>.file}`(첫 파일), `{steps.<id>.data.0.id}`(구조화 결과)
  - 필터 `{paths|lines}`(여러 줄 → 목록)
- 값 전체가 변수 하나면 원래 타입(숫자/목록/dict)으로 들어갑니다.
- LLM이 있어도 실행 방식을 "워크플로"로 고르면 LLM을 호출하지 않습니다. 같은 결과를 반복 테스트할 때 씁니다.
- 실행 중에는 단계별 진행이 작업 계획(✅/⏭)으로 표시되고, 결과는 `outputs/skill_<이름>/`(사용자면 `users/<ID>/results/`)에 저장됩니다.
- 샘플 스킬 4개(`monthly_sales_report`, `notice_digest`, `learn_rules`, `product_analysis`)에 모두 워크플로가 들어 있습니다. 규칙 학습은 만들기 → 학습 → 점검 → 엑셀, 제품 분석은 장별 계획 → 이전 제품 기록까지 진행합니다.

**CLI**

| 명령 | 용도 |
|---|---|
| `inhouse-agent call list` | 도구 목록 |
| `inhouse-agent call analyze_file path=downloads/a.xlsx` | 도구 직접 실행 |
| `inhouse-agent skills run monthly_sales_report -i month=2026-10 --mode workflow` | 스킬을 LLM 없이 실행 |

## 11. 과거 자료 학습 → 규칙 → 검증 → 규칙 갱신

```
과거 자료(PPT/Excel) ─▶ ① 장별 구조 추출 ─▶ ② 장별 구성(blueprint) 도출 + 규칙 후보 자동 발굴
                                              │
                    LLM 이 근거를 보고 규칙 추가/수정 ◀┘ (각 규칙은 과거 자료에서 '지지율'을 계산)
                                              ▼
새 제품(뮤즈, AI 비서 …) ─▶ ③ 장별 계획(outline) ─▶ 조사(MCP/웹) ─▶ PPT/Excel 작성 ─▶ ④ 규칙으로 검증
                                              ▼
                     ⑤ 회고(reflect): 좋은 자료인데 어긴 규칙 → 완화/폐기, 새 패턴 → 규칙 추가
                     승인된 결과물은 다시 '좋은 예시'로 학습 → 규칙 지지율 자동 갱신 (버전·변경기록 보관)
```

**① 장 구조 추출** (`doc_structure`)

| 형식 | 추출하는 내용 |
|---|---|
| PPT | 슬라이드마다 제목, 본문(수준), 표(열·행 수), 차트(종류·계열·항목), 이미지 수, 노트 |
| 엑셀 | 시트마다 열, 숫자 열, 빈칸 비율 |
| Word / PDF | 제목 단위 / 페이지 단위 |

**② 학습** (`rulebook_learn`)

- 좋은 예시들의 장 제목을 비슷한 것끼리 묶어 **장별 구성**을 만듭니다. 장마다 다음 정보가 들어갑니다.
  - 몇 %의 자료에 나오는지, 평균 위치
  - 장마다 필요한 형식(표/차트/이미지 비율), 공통 표 열, 핵심어, 평균 분량, 예시
- 제목은 공백을 없앤 글자 단위로 비교하고, "현황·분석" 같은 흔한 말을 뺀 비교도 함께 씁니다. 그래서 "3. 경쟁사 비교", "경쟁사비교 분석", "경쟁 현황 비교"가 같은 장으로 묶입니다.
- 제품명처럼 자료마다 바뀌는 부분은 빼고, 공통 부분(예: "기술 분석 보고서")을 장 이름으로 씁니다.
- 장별 구성에서 기계적으로 검사할 수 있는 **규칙 후보**를 자동으로 뽑습니다. 필수 장, 장의 표/차트, 표 열, 핵심어, 순서, 분량, 엑셀 시트/열이 대상입니다.
- 모든 규칙의 **지지율**(좋은 예시 중 그 규칙을 지킨 비율)을 다시 계산하고, 상태를 자동으로 바꿉니다.
  - 후보 → 활성: 지지율 75% 이상, 근거 자료 2건 이상
  - 활성 → 재검토: 지지율 50% 이하
- 사람이나 에이전트가 상태를 직접 정하면 🔒 잠겨서 자동으로 바뀌지 않습니다. 통계보다 팀 기준이 우선합니다.

**규칙 유형**

| 유형 | 검사 내용 |
|---|---|
| `section_required` / `section_order` | 장 존재 / 장 순서 |
| `section_element` | 장 안의 표·차트·이미지 |
| `table_columns` | 표 열 |
| `section_keywords` | 핵심어 |
| `section_length` | 분량 |
| `text_regex` | 정규식 (예: 출처 표기) |
| `slide_count` | 전체 장 수 |
| `excel_sheet` / `excel_columns` / `excel_values` | 엑셀 시트 / 열 / 값의 빈칸·범위 |
| `judgment` | LLM이 판단 (예: "시사점이 앞 장 데이터로 뒷받침되는가"). `rulebook_judge`로 판정을 기록하고, 판정도 지지율에 반영됩니다. |

**③④ 새 제품 분석** (`product_analysis` 스킬)

1. `rulebook_outline`으로 장별 계획을 받습니다. 장마다 필요한 형식, 표 열, 다룰 내용, 지킬 규칙, 과거 예시가 들어 있습니다.
2. 장마다 필요한 자료를 조사합니다.
3. `write_pptx`로 PPT를 만듭니다. 과거 보고서를 template으로 주면 같은 디자인을 씁니다.
4. `rulebook_validate`로 검증합니다. 장별 체크리스트, 어긴 규칙, 점수(must 3 / should 2 / nice 1 가중)가 나옵니다.
5. 어긴 규칙을 고치고 다시 검증한 뒤, `rulebook_record_product`로 제품의 핵심 사실을 기록합니다. 이 기록은 다음 제품과 비교하는 데 씁니다.

**⑤ 스스로 점검** (`rulebook_reflect`)

다음을 찾아 개선을 제안합니다. 에이전트는 근거를 `doc_structure`로 확인한 뒤 `rulebook_update_rule`/`rulebook_add_rule`로 반영하고, 반드시 이유를 남깁니다.

- 좋은 예시도 어기는 규칙 (너무 엄격함)
- 근거 없는 규칙
- 규칙이 없는 자주 나오는 장
- 새 자료에서 자주 빠지는 장
- 아직 판정되지 않은 판단형 규칙

**저장과 공유**

- 규칙집은 `rulebooks/<이름>/`에 저장되고 모든 사용자가 공유합니다.
  ```
  rulebook.json  corpus/  validations/  products.json  changelog.jsonl  .history/
  ```
- 규칙마다 버전과 변경 이력(누가, 왜)이 남고, 규칙집 전체 버전 백업도 보관됩니다.
- 사용자가 승인한 결과물을 `label=exemplar`로 검증하거나 학습하면 좋은 예시에 추가됩니다. 그러면 규칙 지지율이 다시 계산되어, 규칙이 실제 작성 관행을 따라가게 됩니다.
- 웹 UI **규칙** 탭에서 장별 구성, 규칙(상태·중요도·문장 바로 수정), 검증(장별 체크리스트), 점검, 제품, 변경 기록을 보고 자료 학습, 문서 검증, 엑셀 내보내기를 합니다. 🤖 버튼은 에이전트에게 작업을 넘깁니다.
- 스킬: `learn_rules`(과거 자료 학습과 규칙 정리), `product_analysis`(새 제품 분석, 검증, 규칙 갱신)

## 12. 첨부 파일 비교 분석 · 스킬 스크립트 · 사용 이력

### 채팅에 PPT / 엑셀 첨부

- **첨부 방법**: 📎 버튼, 탐색기에서 파일을 복사한 뒤 붙여넣기(Ctrl+V), 채팅 화면에 끌어놓기.
  - 가능한 형식: 이미지, pptx, xlsx/xlsm/xls, csv/tsv, docx, pdf, txt/md/json, zip
- **엑셀 셀 범위 붙여넣기**: 엑셀에서 표를 복사해 붙여넣으면 그림이 아니라 `붙여넣은_표.tsv` 파일로 첨부되어 데이터로 분석됩니다.
- **저장 위치**: 사용자 폴더 `uploads/attachments/<시각>/<원래 파일명>`. 업로드할 때 상태를 점검해 손상되었거나 로그인 HTML 같은 파일이면 바로 알려줍니다.
- **에이전트에 전달되는 것**: 첨부 파일 경로와 장별 요약. 에이전트는 이것을 보고 직접 분석합니다.
  - 2개 이상: `compare_documents`로 비교한 뒤, 차이가 큰 장은 `doc_structure`/`query_table`로 근거를 확인합니다.
  - 1개: `doc_structure`/`analyze_file`로 보고, 규칙집이 있으면 `rulebook_validate`로 검증합니다.
- **토큰 없이(LLM 없음)**: 첨부만 하고 보내면 자동으로 비교와 장별 구조 분석을 실행하고, 비교 보고서(md)를 저장합니다.
- **스킬에 첨부**: 스킬을 실행할 때 첨부한 파일은 `steps`에서 `{attachment}`(첫 파일), `{attachments|lines}`(전체)로 씁니다.
- **CLI**: `/attach <경로>`로 첨부한 뒤 요청을 입력합니다.

`compare_documents`가 비교하는 내용 (첫 파일이 기준):

| 조합 | 비교 내용 |
|---|---|
| PPT ↔ PPT (Word/PDF도 같은 방식) | 제목으로 장을 짝지어 추가/삭제/바뀐 장, 제목 변경, 추가/삭제된 문장, 숫자 변화(12.3억 → 13.1억), 표/차트/이미지 수, 발표자 노트, 순서 이동 |
| 엑셀 ↔ 엑셀 (CSV) | 시트별 열 추가/삭제, 행 수, 숫자 열 합계 차이(증감률), 키 열(자동 탐지 또는 `key_columns`) 기준 행 추가/삭제/값 변경 |
| PPT ↔ 엑셀 | PPT에 적힌 숫자가 엑셀 값(셀, 열 합계, 평균)과 맞는지 확인합니다. 억/만/천 단위, %(0.153 = 15.3%), "(단위: 백만원)"처럼 줄여 쓴 값, 반올림을 허용하고, 맞지 않는 숫자를 장 번호와 함께 보여줍니다. |

### 스킬 스크립트 실행

스킬 폴더에 스크립트를 넣어 두면 에이전트가 `run_skill_script`로 실행합니다(예: 메뉴 생성, 보고서 생성). 스킬을 불러오면 실행할 수 있는 스크립트 목록이 지침과 함께 전달되므로, "스크립트를 실행할 도구가 없다"는 답이 나오지 않습니다.

```
skills/menu_builder/
  SKILL.md
  scripts/make_menu.py     ← argparse 스크립트: args {"title": "점심"} 이 --title 점심 으로 전달됨
  scripts/with_ctx.py      ← def run(args, ctx): 형식이면 ctx.call_tool 로 MCP 도구도 호출 가능
```

- **실행 위치**: 작업 폴더가 사용자 `files/skill_<스킬>/<시각>/`이라서, 스크립트가 만든 파일은 결과 파일로 잡혀 바로 받을 수 있습니다.
- **스크립트에서 쓸 수 있는 환경변수**: `SKILL_DIR`, `SKILL_ARGS`(JSON), `AGENT_OUTPUT_DIR`, `AGENT_WORKSPACE`, `AGENT_USER`.
- **지원 형식**: `.py`(에이전트와 같은 파이썬), `.sh`, `.bat/.cmd`, `.ps1`, `.js`.
- **토큰 없이 실행**: `steps`에 적습니다.
  ```yaml
  steps:
    - id: m
      script: scripts/make_menu.py
      args: {title: "{title}", items: [김치찌개, 제육볶음]}
  ```
- **설정**: `skills.scripts: on | approve(실행 전 승인) | off`, `skills.script_timeout: 300`
- **스킬 탭에서 관리**:
  - 📦 **가져오기**: GitHub 주소, zip, 폴더, SKILL.md에서 가져옵니다 ([5장 스킬 가져오기](#스킬-가져오기-github--zip--폴더--다른-에이전트용-스킬)).
  - **파일 추가 / 삭제 / 보기**와 ▶ 실행.
  - 가져오기, 추가, 삭제는 편집자(`skills.editors`)만 할 수 있습니다.

### 사용 이력 / 모델 목록

- `agent_usage`(도구, `/agent_usage days=7`)와 상태 탭의 **사용 이력**:
  - 보여주는 것: 실행 횟수, 성공/실패, 대화·스킬·기능별 횟수, 스킬별·도구별 사용(오류 수), 토큰(입력/출력), 날짜별 실행, 최근 실행 목록
  - 기본은 본인 이력만 보입니다. 모든 사용자 합계(`all_users`, 상태 탭의 "모든 사용자")는 관리자(`skills.editors`)만 볼 수 있습니다.
  - 실행할 때마다 토큰, 소요 시간, 종류, 스킬, 첨부 파일이 기록됩니다.
- `list_models`와 상태 탭의 **모델**:
  - 현재 배포(deployment), 연결 상태, 이미지 지원 여부
  - `llm.models`에 적어 둔 모델
  - Azure 리소스에서 쓸 수 있는 모델 목록(게이트웨이가 막으면 사유를 표시)
- API: `GET /api/usage?days=30[&all=true]`, `GET /api/models`

### 긴 작업의 자동 압축 (토큰 기준)

큰 엑셀이나 PDF를 여러 번 읽는 긴 작업에서도 모델 한도를 넘지 않도록, LLM을 부르기 직전마다 대화 크기를 계산합니다.

- **계산 방법**: 글자 수로 토큰을 어림하고, 직전 호출에서 Azure가 알려준 실제 입력 토큰 수로 보정합니다.
- **압축 시점**: `agent.context_window`(모델의 입력 한도)의 `compact_at`(기본 75%)을 넘으면 압축합니다.
  1. **도구 결과 줄이기**: 최근 차례를 뺀 오래된 도구 결과를 앞부분과 파일 경로만 남긴 형태로 줄입니다. 필요하면 에이전트가 다시 읽습니다.
  2. **요약**: 그래도 한도의 절반을 넘으면 앞쪽 대화를 LLM 요약 하나로 바꿉니다.
     - 지금 처리 중인 요청 원문, 확인된 숫자, 파일 경로는 남깁니다.
     - 도구 호출과 결과의 짝이 깨지지 않는 위치에서만 자릅니다.
- **요약 양식** (pi 방식): 다음 항목을 고정 양식으로 정리해서, 압축 뒤에도 일이 끊기지 않게 합니다.
  - 목표 / 조건·선호
  - 진행(완료 · 진행 중 · 막힘)
  - 주요 결정 / 다음 할 일
  - 꼭 필요한 정보(조회한 ID·URL·중간 계산값)
- **요약 입력 다듬기**:
  - 기록은 `[사용자] … [에이전트 도구 호출] … [도구 결과] …` 텍스트로 넘깁니다. 그래서 요약 모델이 대화를 이어 쓰지 않습니다.
  - 도구 결과는 2,000자까지만 넣습니다. 사용자 요청은 4,000자까지 그대로 넣어 조건이 잘리지 않습니다.
- **두 번째 압축부터**: 이전 요약을 기록에 섞지 않고 따로 넘겨 "갱신"시킵니다. 요약을 다시 요약하다가 숫자·결정이 사라지지 않습니다.
- **파일 목록**: LLM이 기억하게 하지 않고 기록에서 기계적으로 모읍니다.
  - "받거나 만든 파일"은 다운로드 감지·저장 결과, "도구에 넘긴 파일"은 도구 인자의 경로입니다.
  - 압축을 거듭해도 목록이 누적됩니다.
- **오류 대비**: 그래도 모델이 "컨텍스트 초과"로 거절하면 더 강하게 줄여서 한 번 다시 시도합니다.
- **긴 대화 이어하기**: 대화 메시지가 `max_history_messages`(기본 200)를 넘으면 앞부분을 지우는 대신 요약합니다.
- **화면 표시**: "🗜 대화가 길어져 자동으로 줄였습니다 (약 N → M 토큰)"로 보여주고, 요약에 쓴 토큰도 사용량에 포함합니다.
- **설정**: 배포 모델의 입력 한도에 맞게 `agent.context_window`를 바꾸세요. 기본값은 128,000입니다.

### 실행이 끝나는 방식 (반복 한도 · 오류)

**반복 한도는 고정이 아닙니다.** 최신 에이전트들과 같은 방식으로 "넉넉한 상한 + 되풀이·오류·시간 감지 + 마지막 정리"를 씁니다.

| 에이전트 | 반복 한도 | 멈출 때 |
|---|---|---|
| Gemini CLI | 본 대화는 무제한, 하위 에이전트는 10~50회 + 시간 제한 | 같은 도구 호출이 5번 연속이거나 같은 내용이 반복되면 멈춤. 한도에 닿으면 마지막 정리 1회 |
| opencode | 에이전트별 `steps` (기본 무제한) | 도구를 끄고 "한 일 / 못 한 일 / 다음 할 일"을 글로 정리 |
| Goose | `GOOSE_MAX_TURNS` 기본 1000 (메신저 연동은 5) | 도구 반복 감시 |
| **이 에이전트** | 기본 30회에서 시작해 진전이 있으면 10회씩 최대 100회까지 늘어남. 시간 한도 30분 | 아래 표 |

**멈추는 경우와 화면 표시**

| 상태 | 언제 | 사용자가 보는 것 |
|---|---|---|
| 완료 | 모델이 답을 마침 | 답변 (최대 출력 길이에서 잘렸으면 "**이어서**"를 안내) |
| 반복 한도 | 상한에 닿았거나, 최근 3번 동안 새로 성공한 도구 호출이 없음 | 도구 없이 정리한 중간 결과 + ⚠️ 배너와 **계속 진행** 버튼 |
| 시간 한도 | `max_minutes`를 넘음 | 중간 결과 + ⚠️ 배너 |
| 되풀이 중단 | 같은 도구를 같은 인자로 3번 호출 (다운로드 대기 같은 도구는 제외) | 중간 결과 + ⚠️ 배너 |
| 도구 오류 | 도구가 모두 실패한 차례가 3번 이어짐 | 정리 답변과 마지막 오류 + ❌ 배너 |
| 오류 | LLM 오류, 워크플로 단계 실패, 스킬 오류 | ❌ 원인과 조치 + **다시 시도** 버튼 |

- **중간 결과로 멈춘 경우**: 모델이 도구 없이 1번 더 정리합니다. 정리에는 멈춘 이유, 확인한 결과(숫자·파일 경로), 못 한 일, 이어서 할 일이 들어갑니다.
  - **계속 진행**을 누르거나 "계속"이라고 입력하면 같은 대화에서 이어집니다.
- **LLM 오류의 분류와 안내**:

  | 분류 | 안내하는 조치 |
  |---|---|
  | 인증 실패 | API 키 확인 |
  | 배포 없음 | `llm.deployment` 확인 |
  | 요청 한도 초과 | 잠시 후 다시 |
  | 응답 시간 초과 | 다시 시도, `llm.timeout` 확인 |
  | 연결 실패 | 프록시, 엔드포인트 확인 |
  | 콘텐츠 필터 | 요청 표현 확인 |
  | 입력 한도 초과 | 새 대화로, 파일 나누기 |
  | 서버 오류 | 다시 시도 |

  다시 시도해도 되는 오류인지도 알려주고, 멈추기 전까지 실행한 도구와 받은 파일을 함께 보여줍니다.
- **빈 답변**은 1번 다시 받습니다.
- **기록과 출력**: 저장된 대화(내 작업), 상태 탭 실행 목록, CLI(`[상태] …`, 종료코드 2), 예약작업 웹훅에 같은 한국어 상태가 남습니다.
- **스킬별 한도**: SKILL.md 머리말에 `max_iterations: 60`, `max_minutes: 60`처럼 적으면 그 스킬만 다르게 씁니다.
- **하위 에이전트**: 12회(최대 30회), 10분입니다.

### 여러 모델을 비용 기준으로 쓰기 (sol · terra · luna …)

배포가 여러 개면 `llm.models`에 등록합니다(설정 예시는 `config/config.example.yaml`). 에이전트가 요청마다 모델을 골라 비용을 아낍니다.
최신 에이전트의 방식을 합쳤습니다. Gemini CLI처럼 난이도를 판별해 쉬운 일은 싼 모델로 보내고, Goose의 lead/worker처럼 막히면 더 강한 모델을 부릅니다.

1. **시작 모델**: 요청의 난이도에 맞는 가장 싼 모델로 시작합니다.

   | 난이도 | 예 | 시작 모델 |
   |---|---|---|
   | 쉬움 | 조회·확인·목록·날짜 | low (luna) |
   | 보통 | 자료 하나 정리·요약·집계 | mid (terra) |
   | 어려움 | 여러 자료 비교·분석·검증, 보고서·PPT 작성, 원인 분석, 긴 요청, 첨부 2개 이상 | high (sol) |

   - 판별은 기본으로 규칙을 씁니다(추가 비용 없음). `routing.classifier: llm`이면 가장 싼 모델에게 난이도를 물어봅니다.
   - 이미지가 있으면 이미지를 볼 수 있는 모델만 씁니다.
   - "계속"이라고 하면 직전 모델을 그대로 씁니다. "틀렸어, 다시 해" 같은 말이면 한 단계 위 모델로 시작합니다.
   - 스킬 머리말에 `model: sol` 또는 `model_tier: high`를 적으면 그 스킬은 지정한 대로 씁니다.
2. **승격**: 실패 신호가 나오면 한 단계 위 모델로 바꿉니다.
   - 신호: 도구 오류 2번 연속, 같은 호출 되풀이, 진전 없이 반복 한도에 닿음, 빈 답변
   - 반복 한도에 닿았을 때 승격한 모델에게는 10회를 더 줍니다. 그래서 반복 한도로 멈추는 일이 줄어듭니다.
3. **다른 모델로 넘어가기**: 요청 한도 초과(429), 배포 없음, 서버 오류, 시간 초과일 때 다른 모델로 다시 시도합니다. 대화가 너무 길면 입력 한도가 더 큰 모델로 갑니다.
4. **잡일은 싼 모델**: 대화 압축용 요약은 `summary_model`로 합니다. 하위 에이전트는 `subagent_max_tier`(기본 mid) 이하에서 시작합니다.
5. **직접 고르기**: 채팅 위 **모델** 목록에서 "자동 (비용 절약)" 또는 특정 모델을 고릅니다(브라우저에 기억). CLI에서는 `/model terra`, `/model auto`를 씁니다.
6. **비용 보기**: 실행이 끝나면 "모델 luna→terra · 비용 $0.0012 (85% 절약)"이 표시됩니다.
   - 절약은 가장 비싼 모델만 썼을 때와 비교한 값입니다.
   - `/agent_usage`와 상태 탭의 사용 이력에서 모델별 호출, 토큰, 비용, 절약액을 볼 수 있습니다.
7. **모델 목록 보기**: `list_models`로 등급, 단가, 입력 한도, 이미지 지원 여부를 봅니다.

**반복 한도에 자주 닿는다면** `/agent_usage`의 "한도·되풀이로 끝난 실행" 항목을 보세요.

- 멈춘 실행에서 어떤 도구를 많이 불렀는지, 평균 몇 번 왕복했는지, 어떤 요청이었는지가 나옵니다.
- 같은 사이트 도구를 여러 번 부르다 멈췄다면 두 가지로 줄일 수 있습니다.
  - 한 번에 더 많이 가져오는 MCP 도구를 만들거나 스킬 `steps`로 만들기
  - `run_subagent`로 나눠 맡기기

### 업무 지식 그래프 (온톨로지) — 사람들이 한 업무에서 배우기

사용자들이 실제로 한 업무 기록에서 에이전트의 지식을 만듭니다. **LLM을 부르지 않으므로 토큰 비용이 없습니다.**

- **입력**: 모든 사용자의 대화 기록입니다. 요청 문장, 도구 순서, 도구 인자, 오류 내용, 만든 파일, 성공 여부, 스킬을 씁니다.
- **만드는 것 (온톨로지)**

  | 클래스 | 관계 |
  |---|---|
  | 업무 유형 | `hasConcept` → 개념(매출·공지·부서·기간:월 …), `solvedBy` → 검증된 작업 순서, `produces` → 파일 종류, `implementedBy` → 스킬 |
  | 작업 순서 | `uses` → 도구 (인자 형태 포함: `download_sales_excel(month=<월>, wait=true)`) |
  | 도구 | `partOf` → 사이트, `followedBy` → 도구(직접 이어짐 그래프), `hasPitfall` → 자주 난 오류 |

  - 모든 노드와 관계에 본 횟수와 처음·마지막 본 시각이 붙습니다.
- **근거가 된 연구**
  - Agent Workflow Memory(ICML 2025): 성공한 실행에서 되풀이되는 순서를 기억으로 다시 쓰기
  - 프로세스 마이닝 DFG: 무엇 다음에 무엇을 하는지 세기
  - Zep/Graphiti: 시간 정보가 붙은 지식 그래프
  - AutoSchemaKG: 스키마를 미리 정하지 않고 데이터에서 유형을 만들기
- **만드는 방법**
  1. 요청 문장에서 개념을 뽑습니다. 조사·어미를 떼고, "3월·지난달"은 "기간:월"로, "엑셀"은 "xlsx"로 바꿉니다.
  2. 개념과 도구가 비슷한 요청끼리 업무 유형으로 묶습니다. 스킬 실행은 스킬별로 묶고, 채팅창에서 도구를 직접 실행한 기록은 도구 흐름에만 씁니다.
  3. 성공한 실행에서 가장 많이 쓴 도구 순서를 "검증된 순서"로 둡니다. 보조 도구(현재 시각, 계획 등)는 뺍니다.
  4. 오류는 숫자·경로·따옴표 안 값을 지워 같은 종류끼리 셉니다.
- **쓰는 곳**
  - **자동 참고**: 새 요청이 2번 이상 성공한 업무와 비슷하면, 요청 끝에 그 순서와 주의할 오류를 짧게 붙입니다(`[참고: 지식 그래프 …]`).
    요청 끝에 붙으므로 프롬프트 캐시를 깨지 않습니다. 화면에는 "🧭 비슷한 과거 업무의 검증된 순서를 참고로 붙였습니다"가 표시됩니다.
  - **도구**: `knowledge_search`(비슷한 과거 업무 찾기), `knowledge_build`(다시 만들기와 개요), `knowledge_export`(ttl/graphml/json)
  - **화면**: 상태 탭의 "업무 지식"에서 개요를 보고, 업무로 찾고, 다시 만듭니다.
  - **CLI**: `inhouse-agent knowledge build | show | search <업무> | export ttl|graphml|json`
- **내보내기**
  - `ttl`: RDF/OWL 온톨로지(스키마와 사실)입니다. Protégé·GraphDB·rdflib로 엽니다.
  - `graphml`: Gephi·yEd·networkx·Neo4j(APOC)로 엽니다.
  - 외부 라이브러리 없이 만들고, 테스트에서 rdflib와 networkx로 열리는지 확인합니다.
- **자동 갱신**: 그래프가 `rebuild_minutes`(기본 60분)보다 오래되면 찾을 때 다시 만듭니다. 설정은 `config.example.yaml`의 `knowledge:`입니다.
- **스킬 초안 제안** (AWM·SkillWeaver 방식): 같은 순서로 3번 이상 성공했지만 스킬이 없는 업무를 찾아 `SKILL.md` 초안을 만듭니다.
  - 만드는 곳: `knowledge_suggest_skills` 도구, 상태 탭의 "스킬 초안 만들기", CLI `inhouse-agent knowledge suggest`
  - 초안은 `knowledge/skill_drafts/<이름>/`에 생깁니다. 편집자가 확인한 뒤 스킬 가져오기(폴더)로 설치합니다.
  - 인자는 형태대로 바꿉니다.

    | 기록된 형태 | 초안에서 |
    |---|---|
    | `<월>`, `<날짜>` | 입력값 (기본 지난달·어제) |
    | 둘째 단계 이후의 `<파일>` | 앞 단계 파일 `{steps.s1.file}` |
    | 저장 경로 | "저장할 파일 이름" 입력값 |
    | 매번 같았던 값 | 그대로 |
    | 그때그때 다른 값 | 입력값 |

  - 확인할 점은 초안의 "설치 전에 확인할 것"에 적힙니다.
- **효과 측정**: 참고를 붙인 실행은 기록에 `knowledge_hint`(업무 유형)가 남습니다.
  - `/agent_usage`와 상태 탭의 사용 이력에 "업무 지식 참고 효과" 표가 나옵니다.
  - 같은 업무 유형끼리 참고가 붙은 실행과 붙지 않은 실행의 성공률, 평균 도구 왕복, 토큰, 비용을 비교합니다.
  - 효과가 없거나 나빠지면 `knowledge.hint_min_score`를 올리세요.

### 파일 안전장치 (opencode에서 가져옴)

- **덮어쓰기 전 원본 보관**: 에이전트가 기존 파일을 덮어쓰면(엑셀 쓰기, PPT 수정, 파일 저장 등) 원본을 작업폴더 `.versions/<경로>/`에 남깁니다.
  - 파일마다 최근 10개를 보관합니다.
  - `file_versions`로 목록을 보고, `restore_file_version`으로 되돌립니다. 되돌리기 전 내용도 보관하므로 되돌리기를 다시 되돌릴 수 있습니다.
- **읽은 뒤 바뀐 파일 덮어쓰기 막기**: 이번 실행에서 읽은 파일이 그사이 다른 곳(다른 사람, 엑셀 프로그램)에서 바뀌었으면 쓰기를 멈춥니다.
  - "다시 읽어 반영하거나 다른 이름으로 저장하세요"라고 알려 줍니다.
  - 같은 실행에서 에이전트가 직접 쓴 파일은 다시 써도 됩니다.
- **작성 후 자동 검증**(`agent.auto_verify`, 기본 켬): 결과 폴더(`files/`)에 PPT·엑셀을 만들거나 고치면 그 도구 결과 끝에 검사 결과를 붙입니다.
  - PPT: 글 넘침, 겹침, 슬라이드 밖 개체, 작은 글씨, 빈 칸
  - 엑셀: 열리는지, 빈 시트, `#REF!` 같은 오류값
  - 문제가 있으면 모델이 같은 차례에 고칩니다. 문제가 없으면 "이상 없음" 한 줄만 붙이고, LLM 비용은 없습니다.

### 도구 묶음 실행 (run_tool_batch)

여러 도구 호출을 LLM 왕복 한 번으로 처리합니다. 모델이 스크립트로 여러 도구를 부르고 결과를 걸러 요약만 받는 pi의 codemode를 옮긴 것입니다.
다만 코드를 실행하지 않는 선언형이고, 스킬 `steps`와 같은 템플릿 문법을 씁니다.

- **연쇄 호출**: 검색 → 상세 조회 → 다운로드처럼 앞 결과가 다음 인자가 되는 일. 원래는 LLM 왕복 3번이 들던 일이 1번으로 줄어듭니다.
- **항목마다 실행**: `for_each`로 목록의 항목마다 같은 도구를 동시에(4개씩, 최대 20개) 부릅니다.
- **결과 줄이기**: `select`(경로) → `where`(포함 글자) → `fields`(남길 키) → `limit`(개수)로 필요한 부분만 남깁니다.
  - 줄인 결과가 그 단계의 `data`이고, 다음 단계도 이 값을 받습니다. 원본은 `raw`로 참조합니다.
  - `hide: true`면 결과를 모델에게 보여주지 않고 다음 단계 입력으로만 씁니다.

```json
{"steps": [
  {"id": "list", "tool": "groupware__search_notices", "args": {"keyword": "인사", "date_from": "{last_month_start}"},
   "select": "items", "where": {"dept": "인사"}, "fields": ["id", "title"], "hide": true},
  {"id": "detail", "tool": "groupware__get_notice", "for_each": "{steps.list.data}", "args": {"id": "{item.id}"},
   "fields": ["title", "body"], "limit": 10}
]}
```

- **변수**: `{steps.<id>.data.<키>.<번호>}`, `{steps.<id>.raw…}`, `{steps.<id>.text|file|files|error}`, `{item}`, `{item.<키>}`, `{index}`, `{prev}`, 날짜 변수(`{today}` 등)
- **실패 처리**: 단계마다 `on_error: stop`(기본) 또는 `continue`를 정하고, `when`으로 조건을 겁니다. 실패로 멈추면 앞 단계 결과는 남기고 오류로 표시합니다.
- **틀렸을 때 안내**: 경로가 틀리면 "값을 찾지 못한 변수: … (data 최상위 키: id, body)"처럼 실제 키를 알려 줍니다.
  도구 이름이 틀리면 비슷한 이름을 제안합니다.
- **화면**: 안쪽 도구 호출도 하나씩 표시되고, 내려받은 파일은 실행 결과 파일 목록에 들어갑니다.
- **직접 실행**: 채팅창에서 `/run_tool_batch steps='[...]'`로 실행하거나 스킬 `steps`에서 부를 수 있습니다.
  이때 바깥은 스킬 입력값과 날짜만 채우고, `{steps…}`, `{item}`은 묶음이 채웁니다.
- **끄기**: `agent.tool_batch: false`

### 프롬프트 캐시로 입력 비용 줄이기

Azure는 요청의 앞부분(1,024 토큰 이상)이 직전 요청과 같으면 그 부분을 **캐시된 입력**으로 처리합니다.
캐시된 입력은 일반 입력의 약 10% 가격입니다(예: 6.1 sol 입력 $2 → 캐시 $0.1, 100만 토큰당).
에이전트는 도구를 부를 때마다 앞의 대화를 다시 보냅니다. 그래서 앞부분이 그대로 유지되기만 하면 입력 대부분이 캐시로 처리됩니다.

에이전트는 캐시가 잘 맞도록 다음처럼 동작합니다(opencode·pi 방식).

- **대화별 캐시 키**: 대화마다 `prompt_cache_key`를 보내 같은 대화의 요청이 같은 캐시로 가게 합니다.
  - 배포가 이 인자를 모른다고 거절하면 자동으로 빼고 다시 보냅니다. 끄려면 `llm.prompt_cache_key: false`로 둡니다.
- **시스템 프롬프트 고정**: 대화가 이어지는 동안 시스템 프롬프트는 글자 하나 바뀌지 않습니다.
  - 시각은 날짜까지만 넣습니다. 분 단위 시각은 사용자 메시지마다 끝에 "(보낸 시각)"으로 붙습니다.
  - 최근 작업 목록은 대화를 시작할 때의 값으로 고정합니다.
  - 서버·스킬·규칙집이 바뀌었거나 날짜가 바뀌면 그때만 새로 만듭니다.
- **마지막 정리에서도 도구 목록 유지**: 반복 한도 등으로 멈출 때 도구 목록을 빼지 않고 `tool_choice: none`으로만 막습니다.
  도구 목록을 빼면 앞부분이 달라져 캐시를 못 쓰기 때문입니다.
- **비용 계산**: 응답의 `cached_tokens`를 기록하고 `cost_cached` 단가로 계산합니다(설정 예시는 `config/config.example.yaml`).
  - `cost_cached`를 비우면 할인 없이 `cost_in`으로 계산합니다.
  - 실행이 끝나면 "토큰 12,345 · 캐시 70%"처럼 표시됩니다.
  - `/agent_usage`에서는 모델별 "그중 캐시" 열과 전체 캐시 비율을 볼 수 있습니다.

캐시가 깨지는 경우는 이렇습니다. 실행 중에 모델이 바뀌면(승격·다른 모델로 넘어가기) 새 모델은 캐시가 없습니다.
대화 압축(오래된 도구 결과 줄이기·요약)도 그 시점에 한 번 앞부분을 바꿉니다.

### 하위 에이전트 (run_subagent)

큰 작업을 나눠 하위 에이전트에게 맡기고, 결과 요약만 돌려받습니다.

- **쓰는 경우**: 첨부 파일이나 과거 보고서가 여러 개라 각각 깊게 분석할 때, 여러 사이트에서 각각 자료를 모을 때
  - 한 번에 여러 개를 맡기면 동시에 실행됩니다.
  - 각자 별도 대화를 쓰므로 본 대화가 커지지 않습니다.
- **쓰지 않는 경우**: 단순 조회 하나, LLM이 없는 상태. 에이전트가 직접 처리합니다.
- **규칙**:
  - 하위 에이전트는 다시 하위 에이전트를 부를 수 없습니다.
  - 사용자에게 질문하지 않습니다. 그래서 맡길 때 조건(날짜, 부서, 형식)을 모두 적습니다.
  - 같은 사용자 작업폴더를 씁니다.
  - 쓴 토큰은 상위 실행의 사용량과 사용 이력에 합쳐집니다.
- **화면 표시**: 채팅에 "🤖 하위 작업 … 진행 중 · 도구 N회 → 완료"로 작업마다 한 줄씩 나옵니다.
- **설정**: `agent.subagents`(끄려면 false), `agent.subagent_max_iterations`(기본 12)

### 사내 문서 도구

#### 기존 엑셀 양식 채우기 (`inspect_excel_template`, `fill_excel`)

회사 보고 양식을 복사해 값만 채웁니다. 서식, 수식, 병합 셀, 열 너비, 차트는 그대로 두고, 원본 양식은 바꾸지 않습니다.

1. **`inspect_excel_template`**: 양식의 구조를 봅니다.
   - 시트별 `{{자리표시}}`, 라벨 옆 입력칸(예: `작성자:` → B3)
   - 표 머리글과 첫 빈 행, 병합 셀, 수식 수, 이름 정의
2. **`fill_excel`**: 값을 채웁니다.
   - `cells`: `{"B3": "김영업", "요약!B2": 1234000, "이름정의": 값}`
   - `replace`: `{"{{month}}": "2026년 10월"}`. 셀 전체가 자리표시면 숫자는 숫자로 넣습니다.
   - `tables`: `[{"sheet": "보고서", "header": "품목", "rows": [{"품목": "모니터", "수량": 3}]}]`
     - 머리글 이름으로 열을 맞추고, 첫 데이터 행의 서식(테두리, 숫자 형식)을 아래 행에도 적용합니다.
     - `insert: true`면 행을 끼워 넣어 합계 행을 아래로 밉니다.
   - **값을 넣는 규칙**:
     - 숫자 문자열("1,234,000")은 숫자로 넣습니다. 단, `00123` 같은 코드는 문자 그대로 둡니다.
     - 날짜 칸에 `YYYY-MM-DD`를 주면 날짜로 넣습니다.
     - `=`로 시작하면 수식으로 넣습니다.
   - 저장할 때 "열면 다시 계산" 표시를 하므로, 엑셀에서 열면 합계 등이 새 값으로 바뀝니다.
   - 양식에 없는 자리표시나 머리글, 덮어쓴 셀, 빠진 차트는 경고로 알려줍니다.
3. **지원 형식**: xlsx/xlsm만 됩니다. xls 양식은 엑셀에서 xlsx로 저장한 뒤 쓰세요.

#### 엑셀 데이터 규칙 (`excel_rules_learn`, `excel_rules_validate`, `excel_rules_update`, `excel_rules_list`)

과거의 정상 자료(엑셀/CSV 여러 개 또는 폴더)에서 열마다 규칙을 배우고, 새 자료를 검사합니다. 장별 구성을 보는 PPT 규칙집과 달리 표 안의 값을 봅니다.

| 배우는 규칙 | 기준 | 검사 결과 |
|---|---|---|
| 형식 | 값의 95% 이상이 숫자 / 날짜 / 코드(`A-001`, `00123`) / 글 | ❌ 형식이 다른 값 |
| 필수 | 98% 이상의 행에 값이 있음 | ❌ 빈 칸 |
| 중복 금지 | 파일마다 값이 겹치지 않음 (문서번호 같은 키) | ❌ 중복 값 |
| 허용값 | 종류가 적은 글·코드 열 (부서, 상태) | ⚠️ 처음 보는 값 |
| 범위 · 음수 금지 · 정수 | 숫자·날짜의 관측 범위 | ⚠️ 평소 범위를 크게 벗어난 값, ❌ 음수 |
| 코드 모양 | 코드가 일정한 모양 (`S-2026-0001` → `^[A-Z]\-\d{4}\-\d{4}$`) | ⚠️ 모양이 다른 값 |

- **검사 결과**: 문제마다 엑셀 행 번호와 예시 값을 보여주고, 점수와 통과 여부를 매깁니다.
- **규칙 고치기**: 정상 자료인데 걸렸다면 `excel_rules_update`로 고칩니다(허용값 추가, 범위 넓히기, `ignore`).
  - 고치는 이유를 꼭 적어야 하며, 변경 기록에 남습니다.
  - 고친 규칙은 다시 배워도 유지됩니다.
- **저장 위치**: `rulebooks/_data_rules/<이름>.json`. 모든 사용자가 같이 씁니다.

#### 기존 PPT 수정 (`edit_pptx`)

지난달 보고서를 복사해 이번 달 숫자와 문구만 바꿉니다. 원본은 그대로 두고, 바꾼 글의 글꼴·크기·색도 유지합니다.

```json
{"path": "files/10월_보고.pptx", "output": "11월_보고.pptx",
 "replace": {"10월": "11월", "12.3억": "13.1억"},
 "slides": [{"no": 3, "title": "부서별 현황 (11월)", "table": {"rows": [["영업", "850"], ["기술", "460"]]}},
            {"no": 4, "chart": {"categories": ["9월", "10월", "11월"], "series": {"매출": [11, 12.3, 13.1]}}},
            {"no": 2, "notes": "11월 발표 메모"}],
 "delete_slides": [5]}
```

- **`replace`**: 모든 장의 글, 표, 발표자 노트에서 바꿉니다.
  - 한 단어가 서식이 다른 조각에 걸쳐 있어도(예: "10"은 굵게, "월"은 보통) 찾아서 첫 조각의 서식으로 바꿉니다.
- **`slides`**: 장마다 제목, 노트, 표 칸(`start_row`/`start_col`), 차트 데이터(항목·계열 값)를 바꿉니다.
- **`delete_slides`**: 지정한 장을 삭제합니다.
- **경고**: 찾지 못한 글, 없는 장·표·차트, 표의 행이 부족한 경우를 알려줍니다.
- 장 번호는 `doc_structure`로 확인하고, 고친 뒤에는 `check_pptx_layout`으로 글 넘침을 검사하세요.

#### PPT 레이아웃 검사와 미리보기 (`check_pptx_layout`, `render_preview`)

- **`check_pptx_layout`**: 설치할 것 없이 파일만 읽어 검사합니다.
  - 글 넘침: 글꼴 크기와 칸 크기로 줄 수를 어림합니다. 자동 줄임·늘림 설정도 반영합니다.
  - 슬라이드 밖으로 나간 개체, 겹친 글상자
  - 10pt 미만 글씨, 비어 있는 입력칸, 글이 너무 많은 장(160단어 초과), 표 칸 넘침
  - 결과는 장 번호와 개체 이름으로 알려줍니다. 보고서를 만들거나(`write_pptx`) 고친(`edit_pptx`) 뒤 확인용으로 씁니다.
- **`render_preview`**: PPT·엑셀·Word를 PDF와 장별 PNG로 만듭니다.
  - 에이전트가 `view_image`로 이미지를 보고 디자인, 넘침, 겹침을 직접 확인할 수 있습니다.
  - **준비물**: 서버 PC에 [LibreOffice](https://www.libreoffice.org/)를 설치해야 합니다. 설치 경로를 자동으로 찾지 못하면 `config.yaml`의 `office.soffice`에 적으세요.
  - PNG 변환에는 poppler의 `pdftoppm`이나 PyMuPDF가 필요합니다. 둘 다 없으면 PDF만 만듭니다.
  - 결과는 `files/_preview/<파일이름>/`에 저장됩니다.
  - 여러 사람이 동시에 변환해도 충돌하지 않도록 변환할 때마다 LibreOffice 설정 폴더를 따로 씁니다.

#### 영업일 계산 (`business_days`)

한국 공휴일과 회사 휴무일을 반영해 영업일을 계산합니다.

- **공휴일**: `holidays` 패키지로 설날·추석 같은 음력 공휴일, 대체공휴일, 선거일까지 반영합니다.
- **회사 설정**: `config.yaml`의 `calendar.holidays`(회사 휴무일), `calendar.workdays`(휴일이지만 근무하는 날)
- **할 수 있는 계산 (`op`)**:
  - `check`: 영업일인지와 그 이유
  - `prev`/`next`/`add`: n 영업일 전후
  - `count`: 기간의 영업일 수
  - `month`: 그 달의 영업일과 휴일 목록
  - `first`/`last`: 그 달의 첫·마지막 영업일
  - `holidays`: 그해의 휴일 목록
- 같은 기준을 날짜 변수(`{prev_business_day}` 등)와 예약작업 조건(`business_days_only`, `business_day`)에도 씁니다.

## 13. 결과 파일 받기 / 공유 링크

- **웹 화면**: 답변이나 tool 결과에 실제로 있는 파일/폴더 경로가 나오면 📎 링크로 바뀝니다. 파일명에 공백이나 한글이 있어도 됩니다.
  - 경로 클릭: 내려받기 (폴더는 zip으로 묶음)
  - 🔗: 공유 링크 복사
  - 🔍: 파일 탭에서 상태와 분석 보기
  - 아래 파일 칩의 ⬇도 바로 내려받기입니다.
- **서명 링크** `/d/<토큰>/<파일명>`:
  - HMAC 서명과 만료 시간(기본 7일, `server.link_expire_hours`)이 있습니다.
  - API 토큰 없이 열리고, 허용 폴더 밖의 파일은 링크를 만들 수도 열 수도 없습니다.
  - 서명 키는 `workspace/.link_secret`에 자동 생성됩니다. 서버가 여러 대라면 `AGENT_LINK_SECRET` 환경변수로 같은 키를 쓰세요.
- **외부 공유**: `server.public_url`(예: `http://agent.company.com:8080`)을 설정하면 다음 결과에 절대 URL 링크가 붙습니다.
  - `get_download_link`, `send_email`, `send_message`
  - 예약작업 웹훅
  - CLI 결과 (`[파일 받기]`)
- **CLI로 링크 만들기**: `inhouse-agent link files/reports/10월_매출.xlsx --hours 24`

## 14. 범용 자동화 도구

공개 자동화 에이전트(OpenAI Agents SDK, smolagents, Manus, n8n, LangChain, Claude Code)의 도구를 비교한 결과와,
무엇을 넣고 무엇을 뺐는지(예: 셸 명령)는 [docs/agent_tools_analysis.md](docs/agent_tools_analysis.md)에 정리했습니다.

| tool | 용도 | 활성 조건 / 안전장치 |
|---|---|---|
| `web_fetch` | 웹·사내 포털 페이지 본문과 링크 읽기 (EUC-KR 지원) | `downloads.allowed_hosts` 적용 |
| `web_search` | 검색 (SearXNG 사내 설치형 / Tavily) | `web_search.provider`를 설정해야 활성화 |
| `http_request` | 사내 REST API 호출 | GET 이외의 요청은 승인 필요. 무인 실행에서는 `http.allow_unattended_write`일 때만 허용 |
| `sql_query` | 엑셀/CSV 여러 개를 SQL로 조인·집계하고 결과를 csv/xlsx로 저장 | SELECT만 허용. 코드값(00123)은 문자로 유지 |
| `compare_tables` | 이전 자료와 비교 (추가/삭제/값 변경) | |
| `write_excel` | 서식이 적용된 xlsx 보고서 (머리글, 틀 고정, 필터, 천단위) | |
| `make_chart` | 막대/선/원 차트 PNG | matplotlib 필요. 한글은 맑은 고딕/나눔고딕이 있어야 표시됨 |
| `create_zip` / `get_download_link` | 결과 묶기, 공유 링크 | |
| `search_files` | 파일 내용 검색 (엑셀/PDF/Word 포함) | |
| `remember` / `recall` / `forget` | 실행 간 기억. 키 목록은 시스템 프롬프트에 자동으로 들어감 | `workspace/memory.json` |
| `update_plan` | 작업 계획과 진행 상황을 화면에 체크리스트로 표시 | |
| `send_email` | 사내 SMTP 메일 (첨부와 다운로드 링크) | `notify.email.smtp_host`와 `allowed_recipients`가 필요. 대화형에서는 승인 |
| `send_message` | Teams/Slack 웹훅 | `notify.webhooks`에 등록된 채널로만 발송 |

## 15. 구조

```
src/inhouse_agent/
  config.py     설정 로딩 (${ENV} 치환, .env)
  llm.py        Azure OpenAI: Chat Completions 또는 Responses API (추론 내용 이어 받기) + function calling
  mcp_hub.py    MCP 서버 연결, tool 변환, 호출 (재연결, 순차 실행, 제한 시간, 다운로드 파일 자동 감지)
  files.py      작업폴더(sandbox), 다운로드 감지, 파일 상태 점검, 다운로드 대기
  analyzers.py  엑셀/CSV/HTML표/PDF/DOCX/PPTX/JSON/ZIP 분석, query_table
  skills.py     SKILL.md 파싱, 입력값 처리, 저장소(버전 백업)
  interaction.py ask_user 를 실행 중인 UI(웹/CLI)와 연결
  builtins.py   내장 tool (파일, 분석, 스킬, ask_user, 저장, download_url, create_tool, run_python)
  downloader.py 다운로드 URL 받기 / 일반 HTTP 요청 (한글 파일명, 프록시/CA, 쿠키, 호스트 제한)
  links.py      서명된 다운로드 링크 (/d/...)
  users.py      사용자별 작업폴더(WorkspaceProxy), 대화 세션 저장/이어서 하기
  docstruct.py  PPT/엑셀/Word/PDF 장(슬라이드/시트) 단위 구조 추출
  compare.py    문서 비교 (PPT 장별 차이, 엑셀 합계·행 변경, PPT 숫자↔엑셀 교차 확인), 첨부 안내문
  skill_scripts.py 스킬 폴더 스크립트 실행 (run_skill_script: 명령줄 그대로, 화면 문서 요약, 환경변수)
  skill_install.py 스킬 가져오기 (zip / GitHub·Git / 폴더 / SKILL.md → 여러 스킬 찾기 → 골라 설치 → 원본에서 갱신)
  usage.py      사용 이력(agent_usage) / 모델 목록(list_models)
  rulebook.py   장별 구성 도출, 규칙 후보 발굴, 규칙 검사·지지율·상태 자동 변경, 검증, 점검(reflect), 버전/변경기록
  rules_tools.py 규칙집 tool (learn/add/update/validate/judge/outline/reflect/record_product/export) + write_pptx
  pptxgen.py    PPT 생성 (과거 보고서 템플릿 디자인 사용)
  workflow.py   스킬 steps 를 LLM 없이 실행 (템플릿 변수, 조건, 필터)
  images.py     채팅 이미지 → 비전 모델 입력 (축소/변환, 이전 이미지 정리)
  remote_files.py 원격 MCP 서버 파일 가져오기 (path_map, fetch_tool, resource, localhost 링크 변환)
  toolkit.py    범용 도구 (web_fetch, http_request, sql_query, write_excel, make_chart, send_email ...)
  dynamic_tools.py 동적 도구 저장소, 정적 검사, 승인, 자식 프로세스 실행(+tool 호출 중계)
  _tool_runner.py  동적 도구를 실행하는 자식 프로세스
  agent.py      LLM ↔ tool 루프, 시스템 프롬프트, 이력 관리
  errors.py     종료 상태 이름, LLM 오류 원인 분류와 조치 안내
  routing.py    비용 기준 모델 선택 (난이도 판별, 승격, 다른 모델로 넘어가기, 모델별 비용), 배포별 연결(ModelPool)
  context.py    토큰 기준 자동 압축 (오래된 도구 결과 줄이기 → 앞 대화 요약, 컨텍스트 초과 재시도)
  subagent.py   하위 에이전트 run_subagent (별도 대화, 동시 실행, 깊이 1)
  batch.py      도구 묶음 실행 run_tool_batch (연쇄 호출, 항목마다 실행, 결과 줄이기)
  runtime.py    조립, 예약작업 실행, 결과 저장, 웹훅
  scheduler.py  APScheduler cron 등록
  server.py     FastAPI: 실행 관리(SSE 스트리밍, 질문/답변, 중단), 스킬/파일 API
  web/index.html 웹 UI (조사 / 스킬 / 파일 / 상태) - 외부 CDN 없이 단일 파일
  cli.py        명령행
skills/                         스킬 (샘플: monthly_sales_report, notice_digest)
tools/                          에이전트/사용자가 만든 동적 도구 (실행 시 생성)
scripts/                        start_server.bat/.sh (내 PC 에서 서비스), windows_firewall.bat (포트 허용)
examples/sample_mcp_server.py   데모 MCP 서버 (stdio, 또는 --http 8001). 백그라운드 다운로드, blob 첨부, 세션 만료 HTML 저장 시나리오 포함
tests/                          통합 테스트 (실제 MCP 서버, 가짜 Azure 엔드포인트)
```

- 시스템 프롬프트는 `config.yaml`의 `agent.system_prompt`로 바꿀 수 있습니다. 기본값은 `agent.py`의 `DEFAULT_SYSTEM_PROMPT`이고,
  현재 시각과 서버 목록, 서버 설명은 자동으로 붙습니다.
- 결과를 디버깅할 때는 `outputs/<작업>/_trace/*.json`에서 LLM이 어떤 tool을 어떤 인자로 불렀는지 확인할 수 있습니다.

## 16. 테스트

```bash
pip install -e ".[server,dev]"
pytest -q
```

샘플 MCP 서버를 실제로 띄우고, 가짜 LLM과 가짜 Azure HTTP 엔드포인트로 다음을 검증합니다.

- tool 호출, 병렬 실행, 오류 전달
- 서버가 죽었을 때 재연결
- 반복 한도 초과 시 마무리 답변
- 예약작업 실행과 결과 저장
- Azure 요청 형식 (배포 이름 경로, api-version, api-key, 추가 헤더, max_completion_tokens)
- 파일 상태 판정 (정상/빈 파일/의심 HTML/잘린 PDF/깨진 ZIP/다운로드 중/재다운로드 중), 다운로드 대기, 경로 sandbox
- 분석 (cp949 CSV 제목행, HTML표 xls, docx, 다중 시트 xlsx, zip slip 차단)
- 스킬 파싱/검증/버전 백업, 스킬 실행 중 ask_user 답변 반영과 다운로드 감지
- 웹 API 흐름: 스킬 생성/저장 → 실행 → SSE 이벤트 → 질문 답변 → 후속 지시 → 파일 업로드/분석
- 다운로드: 한글 파일명 복원, 리다이렉트, 404, 크기 제한, 호스트 제한, 링크로컬 차단, 사이트별 헤더/쿠키, 로그인 페이지 감지, 링크 감지/자동 받기
- 여러 사용자 서비스: 스킬 작성자·수정자 기록, 다른 사용자 스킬이 / 목록에 바로 보임, 편집 권한(skills.editors: 화면/API/create_tool), 서버 시작 시 사내망 주소 표시·공유 링크 주소 자동 설정, CLI / 스킬 메뉴·실행
- / 명령·토큰 없이: 템플릿/필터/조건, 샘플 스킬 4개 워크플로 실행(규칙 학습 포함), 워크플로 모드는 LLM 미호출, /api/commands, 스킬/도구 직접 실행·세션 기록,
  LLM 없는 자유 질문 안내, 스킬 대화 후속 말은 워크플로 재실행 안 함, 건너뛴 단계 표시, 잘못된 요청 거절
- 규칙집: 슬라이드 구조(표/차트/노트, python-pptx 없이도), 제목 유사도, 장별 구성(공통 이름), 후보 발굴·중복 방지, PPT/엑셀 분리,
  검증 체크리스트, 판단형 판정, 승인 자료 학습 → 자동 재검토, 잠금 우선, 점검 제안, 장별 계획, PPT 생성(템플릿) → 검증 100점, tool/API 경로
- 사용자: ID 검증, 사용자 간 파일/기억/세션/실행 분리, 공용 다운로드 → 사용자 폴더 이동, ID 필수/허용 목록, 예약작업 user
- 이어서 하기: 세션 저장(이미지 데이터 대신 경로) → 서버 재시작 후 같은 세션으로 이어서(이전 대화 전달), 이름 변경/삭제, list/read_past_work
- 이미지: 업로드 검증, 큰 이미지 축소, content 배열 구성, 이전 턴 이미지 정리, view_image 주입, 내부 키 제거, vision=false
- 원격 MCP: 별도 프로세스 HTTP 서버로 원격 PC 흉내 → fetch_tool / path_map(공유폴더, URL) / MCP resource / 설정 없음 안내 / fetch_remote_file 대기 / localhost 링크 변환(LAN IP 접속)
- 링크: 서명/위조/만료/허용폴더, 경로 인식(공백·한글 파일명, 폴더), 토큰 없이 서명 링크로 받기, 폴더 zip
- 범용 도구: web_fetch(EUC-KR, 상대 링크), http_request 승인, web_search, sql_query 조인/저장, compare_tables, write_excel 서식, make_chart, create_zip, search_files, 기억/계획 이벤트, 메일(수신자 제한/첨부/링크/거부), 메신저
- 동적 도구: 정적 검사, 승인/거부/비대화형 거절, 테스트 실패 시 미등록, 같은 실행 안에서 생성→호출, 재시작 후 재사용, 재귀 호출 금지, 제한 시간, 비밀 환경변수 미전달, mode off, 웹 API
