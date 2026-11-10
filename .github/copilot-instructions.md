# GitHub Copilot 안내

이 저장소의 작업 규칙·정책·구조는 저장소 루트의 `AGENTS.md`에 있습니다. 변경 이력과 이유는 `docs/CHANGELOG.md`에 있습니다. 코드를 바꾸기 전에 두 파일을 읽으세요.

특히 아래는 실제 장애·사용자 요청에서 나온 규칙이라 되돌리면 안 됩니다(자세한 이유는 AGENTS.md 5장).

- 인증 없음(사용자 ID만 받아 사용자별 폴더), 도구마다 권한 요청 없음, HWP 기능 추가 안 함
- 추론 모델은 추론 토큰도 `max_output_tokens`를 씀 → 기본 25000, 답 없이 잘리면 `llm.OutputBudget`이 자동으로 다시 요청. `incomplete` 응답을 예외로 바꾸지 말 것
- 프롬프트 캐시: 시스템 프롬프트는 대화 중 바뀌면 안 됨(분 단위 시각·바뀌는 목록 금지), 마지막 정리도 도구 목록 유지 + `tool_choice="none"`
- 큰 도구 결과는 `tool_results/`에 저장하고 앞·뒤만 대화에 넣음(`max_tool_result_chars`)
- Responses API 추론 항목은 같은 배포에만 다시 보냄
- 오류는 `errors.describe_error`로 원인·조치를 한국어로 보여줌 ("내부 오류"로 두지 말 것)
- 설정 추가 시 `config.py`, `config/config.example.yaml`, README를 함께 고침. 서버 API 변경 시 `API_VERSION`과 `UI_API`를 함께 올림
- 변경은 `pytest -q`로 확인
