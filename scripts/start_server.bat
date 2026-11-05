@echo off
REM 내 PC 에서 에이전트 서버를 켜서 다른 사람들이 브라우저로 쓰게 합니다.
REM 사용법: scripts\start_server.bat [포트]   (기본 8080)
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set PORT=%1
if "%PORT%"=="" set PORT=8080
if not exist .venv\Scripts\python.exe (
  echo [설치] 가상환경을 만듭니다...
  python -m venv .venv || goto :error
  .venv\Scripts\python.exe -m pip install -e ".[server]" || goto :error
)
if not exist config\config.yaml (
  echo [설정] config\config.yaml 이 없어 예시를 복사합니다. 필요한 값을 채워 주세요.
  copy config\config.example.yaml config\config.yaml >nul
)
.venv\Scripts\inhouse-agent.exe serve --host 0.0.0.0 --port %PORT%
goto :eof
:error
echo 설치/실행에 실패했습니다. 위 오류를 확인하세요.
pause
