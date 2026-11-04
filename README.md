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
| 다운로드 파일 감지 | MCP tool을 호출하기 전과 후의 다운로드 폴더를 비교하고, 결과 텍스트에 적힌 경로와 바이너리(blob)로 받은 파일도 찾아서 자동으로 상태를 점검합니다. |
| 파일 상태 점검 | 다운로드 중(`.crdownload`, 크기 변화, 재다운로드), 빈 파일, 손상(ZIP/PDF 무결성), 의심(엑셀인데 실제로는 로그인 HTML 페이지) 상태를 구분합니다. |
| 파일 분석 | xlsx/xls/HTML표 xls/CSV(cp949)/PDF/DOCX/PPTX/JSON/ZIP을 분석합니다. 표는 제목행을 건너뛰고 열별 통계를 내며, `query_table`로 필터·그룹 집계를 정확하게 계산합니다. |
| 스킬 | `skills/<이름>/SKILL.md`에 업무 절차와 입력값을 정의합니다. 에이전트가 알아서 불러 쓰고, 예약작업에서도 실행할 수 있습니다. |
| 다운로드 URL | MCP 결과에 있는 다운로드 링크를 감지해서 `download_url`로 받습니다. 한글 파일명(`filename*=`, 깨진 인코딩)을 복원하고, 리다이렉트, 사내 프록시/CA, 쿠키/헤더, 사이트별 인증 설정을 지원합니다. 링크인데 로그인 페이지가 받아지면 의심 상태로 표시합니다. |
| 동적 도구 | 필요한 tool이 없으면 에이전트가 파이썬 코드로 tool을 만들고(`create_tool`) 테스트해서 바로 사용합니다. 일회성 계산은 `run_python`으로 처리합니다. 만든 도구는 `ctx.call_tool_data()`로 기존 MCP tool을 조합할 수 있고, `tools/`에 저장되어 계속 재사용됩니다. 승인 모드를 지원합니다. |
| 파일 받기 / 공유 | 답변과 tool 결과에 나온 파일·폴더 경로를 클릭하면 바로 받습니다(폴더는 zip). 🔗를 누르면 API 토큰 없이 열리는 기한부 서명 링크가 복사됩니다. 메일, 메신저, 웹훅, CLI 결과에도 링크가 붙습니다. |
| 범용 자동화 도구 | 공개 자동화 에이전트들의 도구를 분석해서 추가했습니다([분석 문서](docs/agent_tools_analysis.md)): 웹 읽기/검색, HTTP API, SQL, 표 비교, 엑셀·차트·zip 생성, 파일 내용 검색, 기억, 작업 계획, 메일/메신저 알림 |
| 사용자별 작업폴더 / 이어서 하기 | 사용자 ID별로 `workspace/users/<ID>/` 폴더를 따로 씁니다. 대화는 자동으로 저장되어 "내 작업" 목록에서 골라 이어서 진행할 수 있고, 서버를 다시 켜도 유지됩니다. 에이전트가 최근 작업을 기억합니다. |
| 채팅 이미지 | 🖼 버튼, 붙여넣기(Ctrl+V), 끌어놓기로 첨부합니다. 비전 모델이 직접 보고, 저장된 이미지(MCP 캡처 등)는 `view_image`로 봅니다. |
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

`.env`에 `AGENT_API_TOKEN`을 설정하면 모든 `/api/*` 요청에 `Authorization: Bearer <토큰>` 헤더가 필요합니다.

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
> 신뢰할 수 있는 사내 서버에서 운영하고, 웹 UI를 다른 사람과 같이 쓰면 반드시 `AGENT_API_TOKEN`을 설정하세요.
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
downloads/  uploads/images/  files/  results/  sessions/  memory.json
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

- 🖼 버튼, 붙여넣기(Ctrl+V, 화면 캡처), 채팅창에 끌어놓기로 첨부합니다. 한 번에 최대 10장이고, 썸네일을 클릭하면 크게 보입니다.
- 이미지는 `uploads/images/`에 저장되고, 긴 변 2048px로 줄여 비전 모델에 보냅니다.
- 다음 요청부터는 이전 이미지를 경로 텍스트로 바꿔서 보내 토큰을 아낍니다. 다시 봐야 하면 에이전트가 `view_image`로 불러옵니다.
- `view_image`로는 MCP 화면 캡처나 다운로드한 이미지도 에이전트가 직접 볼 수 있습니다.
- 배포 모델이 이미지를 지원하지 않으면 `llm.vision: false`로 설정하세요. 그러면 이미지 대신 안내 문구를 보냅니다.

## 9. 결과 파일 받기 / 공유 링크

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

## 10. 범용 자동화 도구

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

## 11. 구조

```
src/inhouse_agent/
  config.py     설정 로딩 (${ENV} 치환, .env)
  llm.py        Azure OpenAI Chat Completions + function calling
  mcp_hub.py    MCP 서버 연결, tool 변환, 호출 (재연결, 순차 실행, 제한 시간, 다운로드 파일 자동 감지)
  files.py      작업폴더(sandbox), 다운로드 감지, 파일 상태 점검, 다운로드 대기
  analyzers.py  엑셀/CSV/HTML표/PDF/DOCX/PPTX/JSON/ZIP 분석, query_table
  skills.py     SKILL.md 파싱, 입력값 처리, 저장소(버전 백업)
  interaction.py ask_user 를 실행 중인 UI(웹/CLI)와 연결
  builtins.py   내장 tool (파일, 분석, 스킬, ask_user, 저장, download_url, create_tool, run_python)
  downloader.py 다운로드 URL 받기 / 일반 HTTP 요청 (한글 파일명, 프록시/CA, 쿠키, 호스트 제한)
  links.py      서명된 다운로드 링크 (/d/...)
  users.py      사용자별 작업폴더(WorkspaceProxy), 대화 세션 저장/이어서 하기
  images.py     채팅 이미지 → 비전 모델 입력 (축소/변환, 이전 이미지 정리)
  remote_files.py 원격 MCP 서버 파일 가져오기 (path_map, fetch_tool, resource, localhost 링크 변환)
  toolkit.py    범용 도구 (web_fetch, http_request, sql_query, write_excel, make_chart, send_email ...)
  dynamic_tools.py 동적 도구 저장소, 정적 검사, 승인, 자식 프로세스 실행(+tool 호출 중계)
  _tool_runner.py  동적 도구를 실행하는 자식 프로세스
  agent.py      LLM ↔ tool 루프, 시스템 프롬프트, 이력 관리
  runtime.py    조립, 예약작업 실행, 결과 저장, 웹훅
  scheduler.py  APScheduler cron 등록
  server.py     FastAPI: 실행 관리(SSE 스트리밍, 질문/답변, 중단), 스킬/파일 API
  web/index.html 웹 UI (조사 / 스킬 / 파일 / 상태) - 외부 CDN 없이 단일 파일
  cli.py        명령행
skills/                         스킬 (샘플: monthly_sales_report, notice_digest)
tools/                          에이전트/사용자가 만든 동적 도구 (실행 시 생성)
examples/sample_mcp_server.py   데모 MCP 서버 (stdio, 또는 --http 8001). 백그라운드 다운로드, blob 첨부, 세션 만료 HTML 저장 시나리오 포함
tests/                          통합 테스트 (실제 MCP 서버, 가짜 Azure 엔드포인트)
```

- 시스템 프롬프트는 `config.yaml`의 `agent.system_prompt`로 바꿀 수 있습니다. 기본값은 `agent.py`의 `DEFAULT_SYSTEM_PROMPT`이고,
  현재 시각과 서버 목록, 서버 설명은 자동으로 붙습니다.
- 결과를 디버깅할 때는 `outputs/<작업>/_trace/*.json`에서 LLM이 어떤 tool을 어떤 인자로 불렀는지 확인할 수 있습니다.

## 12. 테스트

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
- 사용자: ID 검증, 사용자 간 파일/기억/세션/실행 분리, 공용 다운로드 → 사용자 폴더 이동, ID 필수/허용 목록, 예약작업 user
- 이어서 하기: 세션 저장(이미지 데이터 대신 경로) → 서버 재시작 후 같은 세션으로 이어서(이전 대화 전달), 이름 변경/삭제, list/read_past_work
- 이미지: 업로드 검증, 큰 이미지 축소, content 배열 구성, 이전 턴 이미지 정리, view_image 주입, 내부 키 제거, vision=false
- 원격 MCP: 별도 프로세스 HTTP 서버로 원격 PC 흉내 → fetch_tool / path_map(공유폴더, URL) / MCP resource / 설정 없음 안내 / fetch_remote_file 대기 / localhost 링크 변환(LAN IP 접속)
- 링크: 서명/위조/만료/허용폴더, 경로 인식(공백·한글 파일명, 폴더), 토큰 없이 서명 링크로 받기, 폴더 zip
- 범용 도구: web_fetch(EUC-KR, 상대 링크), http_request 승인, web_search, sql_query 조인/저장, compare_tables, write_excel 서식, make_chart, create_zip, search_files, 기억/계획 이벤트, 메일(수신자 제한/첨부/링크/거부), 메신저
- 동적 도구: 정적 검사, 승인/거부/비대화형 거절, 테스트 실패 시 미등록, 같은 실행 안에서 생성→호출, 재시작 후 재사용, 재귀 호출 금지, 제한 시간, 비밀 환경변수 미전달, mode off, 웹 API
