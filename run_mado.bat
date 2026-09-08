@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   MADO: Multi-Agent Debate & Orchestration Platform (오프라인 / 폐쇄망 모드)
echo ==========================================================

set "PYTHONHOME=%~dp0python_runtime"
set "PYTHONPATH=%~dp0;%~dp0python_runtime\Lib;%~dp0python_runtime\Lib\site-packages"
set "PATH=%~dp0python_runtime;%~dp0python_runtime\Scripts;%~dp0node_runtime;%PATH%"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem --- MCP 서버 실행 경로 (conf.json 의 ${VAR:-기본값} 치환에 사용) ---
set "PYTHON_BIN=%~dp0python_runtime\python.exe"
set "NODE_BIN=%~dp0node_runtime\node.exe"
set "MCP_NODE_HOME=%~dp0mcp_node"
set "MCP_SANDBOX_HOME=%~dp0mcp_sandbox"
set "WORKSPACE_DIR=%~dp0workspace"
if not defined SANDBOX_KERNEL_PYTHON set "SANDBOX_KERNEL_PYTHON=%PYTHON_BIN%"

if not exist "%NODE_BIN%" echo [!] node_runtime\node.exe 가 없습니다. filesystem / memory MCP 가 비활성화됩니다.

rem --- 접속 주소는 conf.json 의 app 값을 그대로 읽습니다 (하드코딩 금지) ---
set "APP_URL="
for /f "usebackq delims=" %%i in (`"%PYTHON_BIN%" -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"`) do set "APP_URL=%%i"
if not defined APP_URL set "APP_URL=conf.json 의 app 참조"

rem --- 서버가 응답하면 브라우저를 엽니다. 서버는 콘솔을 붙잡고 있으므로
rem     기다리는 일은 별도 프로세스가 합니다 (MAO_NO_BROWSER=1 이면 건너뜁니다).
rem     같은 인자를 그대로 넘겨야 --port 로 포트를 바꿔도 맞는 주소를 엽니다.
if exist "%~dp0open_browser.py" start "" /b "%PYTHON_BIN%" open_browser.py %*

echo [*] 내장 포터블 파이썬 런타임으로 서버를 시작합니다 (%APP_URL%)...
"%PYTHON_BIN%" -m app.main %*

pause
