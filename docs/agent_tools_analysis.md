# 공개 자동화 에이전트 도구 분석 → 사내 에이전트 반영 내역

작성일: 2026-11-03

목적: 공개된 자동화 에이전트들이 기본으로 제공하는 도구를 비교해서, 사내 에이전트에 무엇이 비어 있는지 찾고,
사내 환경(내부망, Azure OpenAI, 웹컨트롤 MCP)에 맞는 것만 골라 추가합니다.

> 각 제품의 도구 이름과 구성은 버전마다 바뀝니다. 아래 표는 2026-11 시점의 공개 문서와 공개 자료를 바탕으로 정리했습니다.
> Manus의 도구 목록은 공식 문서가 아니라 커뮤니티가 추출한 시스템 프롬프트 기준이라 정확하지 않을 수 있습니다.
> LangChain과 Claude Code 항목은 작성자의 사전 지식 기준입니다.

## 1. 조사 대상과 도구

| 에이전트 | 기본 도구 (요약) |
|---|---|
| OpenAI Agents SDK | Web search, File search(벡터 저장소), Code interpreter(샌드박스 코드 실행), Computer use, Hosted MCP |
| Hugging Face smolagents | 웹 검색(ApiWebSearchTool / DuckDuckGoSearchTool), VisitWebpageTool, PythonInterpreterTool, final_answer |
| Manus (추출 프롬프트 기준) | message_notify_user / message_ask_user, shell, browser_*, file read/write/append/edit, 검색, deploy_* |
| n8n AI Agent | HTTP Request, Code, Calculator, 검색(SerpAPI 등), 통합 노드(Gmail/Sheets/Slack/DB)를 tool로 연결, Memory |
| LangChain 커뮤니티 툴킷 | Requests(GET/POST), SQL Database, File management, Python REPL, Gmail/Office365, 검색 |
| Claude Code | Bash, Read/Write/Edit, Glob/Grep, WebFetch/WebSearch, TodoWrite(작업 계획), Task(하위 에이전트) |

## 2. 기능별 비교와 반영

| 기능 | 공개 에이전트 | 반영 전 | 반영 후 (tool) |
|---|---|---|---|
| 사용자에게 질문 / 확인 | Manus message_ask_user | ✅ `ask_user` | 유지 |
| 진행 상황 공유 / 작업 계획 | Manus notify, Claude TodoWrite | ❌ | ✅ **`update_plan`**: 화면에 체크리스트로 실시간 표시 |
| 브라우저 조작 | Computer use, Manus browser_* | ✅ 사내 웹컨트롤 MCP | 유지 (사내 MCP가 담당) |
| 웹 페이지 읽기 (조작 없이) | smolagents VisitWebpage, WebFetch | ❌ | ✅ **`web_fetch`**: 본문, 링크 목록, 이어 읽기, 쿠키, EUC-KR 처리 |
| 웹 검색 | 대부분 | ❌ | ✅ **`web_search`**: SearXNG(사내 설치형) / Tavily. 설정했을 때만 활성화 |
| HTTP / REST API 호출 | n8n HTTP Request, LangChain Requests | 부분 (`download_url`) | ✅ **`http_request`**: GET 이외의 요청은 승인 필요 |
| 코드 실행 | Code interpreter, Python REPL, n8n Code | ✅ `run_python`, `create_tool` | 유지 |
| 셸 명령 | Manus shell, Claude Bash | ❌ | ❌ **제외** (아래 3절) |
| 파일 읽기/쓰기 | 모두 | ✅ `save_file`, `read_file_text` 등 | 유지 |
| 파일 내용 검색 | Claude Grep, File search | ❌ (파일명만 검색) | ✅ **`search_files`**: 엑셀/PDF/Word 내용까지 검색 |
| SQL / 데이터 질의 | LangChain SQL toolkit | 부분 (`query_table`) | ✅ **`sql_query`**: 여러 파일 JOIN/집계, 결과 저장 |
| 이전 자료와 비교 | (모니터링 워크플로) | ❌ | ✅ **`compare_tables`**: 추가/삭제/값 변경 |
| 보고서 파일 생성 | n8n Sheets, Code interpreter 산출물 | ❌ (텍스트/CSV만) | ✅ **`write_excel`**(서식 적용 xlsx), **`make_chart`**(PNG), **`create_zip`** |
| 결과 전달 / 공유 | Manus deploy/expose, n8n Gmail/Slack | ❌ | ✅ **`get_download_link`**(서명 링크), **`send_email`**, **`send_message`**(Teams/Slack 웹훅) |
| 장기 기억 | n8n Memory, LangChain memory | ❌ | ✅ **`remember` / `recall` / `forget`**: 저장된 키 목록이 시스템 프롬프트에 자동으로 들어감 |
| 하위 에이전트 위임 | Claude Task, 멀티 에이전트 | ❌ | ⏸ 보류 (4절) |

## 3. 제외한 것과 이유

| 기능 | 이유 |
|---|---|
| 셸 명령 (`bash`) | 사내 서버에서 임의 명령을 실행하는 것은 위험이 가장 큽니다. 파일 가공, 계산, API 호출은 이미 `run_python`/`create_tool`로 정적 검사, 승인, 별도 프로세스를 거쳐 할 수 있으므로 셸까지 열 이유가 작습니다. |
| Computer use (화면 조작) | 사내 사이트 조작은 이미 만들어 둔 웹컨트롤 MCP가 더 안정적으로 합니다. |
| Deploy / 포트 공개 | 사내망에 서비스를 공개하는 일은 에이전트 권한 밖에 두는 것이 맞습니다. 결과물 공유는 서명된 다운로드 링크로 대신합니다. |
| 벡터 저장소 file search | 별도 임베딩 인프라가 필요합니다. 지금은 `search_files`(전문 검색)와 `analyze_file`/`sql_query`로 충분하고, 문서가 수천 건 이상으로 늘면 검토합니다. |
| 이미지 생성 | 업무 자료 수집/분석 목적과 거리가 멉니다. 차트는 `make_chart`로 만듭니다. |

## 4. 다음 단계 후보

- **하위 에이전트 위임**: 사이트별로 병렬 수집한 뒤 종합하는 기능. 현재도 서로 다른 MCP 서버는 병렬로 호출되므로, 실제 사용 패턴을 보고 결정합니다.
- **에이전트가 예약작업 등록**: "매주 월요일에 이거 해줘" 같은 요청을 받아 등록하는 기능. 지금은 `config.yaml`로만 등록하는데, 무인 실행 권한과 엮여 있어 승인 정책을 정한 뒤 추가합니다.
- **Office 문서 생성**: docx/pptx 보고서 작성.

## 5. 안전장치 요약 (새 도구)

| 도구 | 장치 |
|---|---|
| `http_request` | POST/PUT/PATCH/DELETE는 대화형에서 매번 승인. 예약작업에서는 `http.allow_unattended_write: true`일 때만 허용 |
| `send_email` | `allowed_recipients` 패턴에 맞는 수신자에게만 발송. 대화형에서는 승인 필요 |
| `send_message` | 설정에 등록된 채널 이름으로만 발송 (에이전트가 임의 URL로 보낼 수 없음) |
| `web_fetch` / `http_request` / `web_search` | `downloads.allowed_hosts`/`blocked_hosts`, 링크로컬 차단, 응답 크기 제한 적용 |
| 공유 링크 | HMAC 서명과 만료 시간. 허용 폴더 밖 경로는 링크를 만들 수도 열 수도 없음 |

## 출처

- [OpenAI Agents SDK built-in tools 가이드 (The New Stack)](https://thenewstack.io/developers-guide-to-the-built-in-tools-of-openai-agents-sdk/)
- [openai-agents-python tools 문서 변경](https://github.com/openai/openai-agents-python/pull/1538/files)
- [smolagents built-in tools 문서](https://huggingface.co/docs/smolagents/reference/default_tools)
- [smolagents PyPI](https://pypi.org/project/smolagents/)
- [Manus 도구/프롬프트 (커뮤니티 추출본)](https://gist.github.com/jlia0/db0a9695b3ca7609c9b1a08dcbf872c9)
- [n8n AI agents tutorial](https://techsy.io/blog/n8n-ai-agents-tutorial)
