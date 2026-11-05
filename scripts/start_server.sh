#!/usr/bin/env bash
# 내 PC(리눅스/맥)에서 에이전트 서버를 켜서 다른 사람들이 브라우저로 쓰게 합니다.
# 사용법: scripts/start_server.sh [포트]   (기본 8080)
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8080}"
if [ ! -x .venv/bin/python ]; then
  echo "[설치] 가상환경을 만듭니다..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -e ".[server]"
fi
if [ ! -f config/config.yaml ]; then
  echo "[설정] config/config.yaml 이 없어 예시를 복사합니다. 필요한 값을 채워 주세요."
  cp config/config.example.yaml config/config.yaml
fi
exec .venv/bin/inhouse-agent serve --host 0.0.0.0 --port "$PORT"
