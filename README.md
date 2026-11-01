# inhouse-agent — 사내 사이트 자료수집 에이전트

사내 사이트별로 만들어 둔 **웹컨트롤 MCP 서버**들을 하나로 묶고, **Azure OpenAI** LLM이
사용자 명령이나 **예약작업**에 따라 필요한 MCP tool을 골라 호출하고, 결과를 정리하고 저장하는 에이전트입니다.

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

inhouse-agent serve --port 8080              # 웹 채팅 화면(/)과 REST API, 예약작업을 함께 실행
```

`python -m inhouse_agent ...`으로도 실행할 수 있습니다.

### REST API (`serve`)

`.env`에 `AGENT_API_TOKEN`을 설정하면 모든 `/api/*` 요청에 `Authorization: Bearer <토큰>` 헤더가 필요합니다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/api/chat` | `{"prompt": "...", "session_id": null, "servers": null}`를 보내면 `{session_id, output, tools_used, ...}`를 돌려줍니다. 받은 session_id를 다시 보내면 대화가 이어집니다. |
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

## 4. 구조

```
src/inhouse_agent/
  config.py     설정 로딩 (${ENV} 치환, .env)
  llm.py        Azure OpenAI Chat Completions + function calling
  mcp_hub.py    MCP 서버 연결, tool 변환, 호출 (재연결, 순차 실행, 제한 시간)
  builtins.py   내장 tool: get_current_time, save_file, read_file, list_files (outputs/files 안으로 제한)
  agent.py      LLM ↔ tool 루프, 시스템 프롬프트, 이력 관리
  runtime.py    조립, 예약작업 실행, 결과 저장, 웹훅
  scheduler.py  APScheduler cron 등록
  server.py     FastAPI 웹 채팅과 REST API
  cli.py        명령행
examples/sample_mcp_server.py   데모 MCP 서버 (stdio, 또는 --http 8001)
tests/                          통합 테스트 (실제 MCP 서버, 가짜 Azure 엔드포인트)
```

- 시스템 프롬프트는 `config.yaml`의 `agent.system_prompt`로 바꿀 수 있습니다. 기본값은 `agent.py`의 `DEFAULT_SYSTEM_PROMPT`이고,
  현재 시각과 서버 목록, 서버 설명은 자동으로 붙습니다.
- 결과를 디버깅할 때는 `outputs/<작업>/_trace/*.json`에서 LLM이 어떤 tool을 어떤 인자로 불렀는지 확인할 수 있습니다.

## 5. 테스트

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
