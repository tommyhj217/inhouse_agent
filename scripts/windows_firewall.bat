@echo off
REM 다른 PC 에서 이 PC 의 에이전트(기본 8080 포트)에 접속할 수 있게 Windows 방화벽을 엽니다.
REM 관리자 권한으로 실행하세요 (마우스 오른쪽 → 관리자 권한으로 실행).
REM 사용법: scripts\windows_firewall.bat [포트]
chcp 65001 >nul
set PORT=%1
if "%PORT%"=="" set PORT=8080
netsh advfirewall firewall delete rule name="inhouse-agent %PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="inhouse-agent %PORT%" dir=in action=allow protocol=TCP localport=%PORT% profile=domain,private
if errorlevel 1 (
  echo 실패했습니다. 관리자 권한으로 다시 실행하세요.
) else (
  echo 방화벽 허용 완료: TCP %PORT% (도메인/개인 네트워크)
  echo 되돌리기: netsh advfirewall firewall delete rule name="inhouse-agent %PORT%"
)
pause
