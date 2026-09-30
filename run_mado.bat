@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   MADO: Multi-Agent Debate ^& Orchestration Platform (오프라인 / 폐쇄망 모드)
echo ==========================================================

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem --- 실행기 (conf.json 의 ${PYTHON_BIN} / ${NODE_BIN} 치환에도 쓰입니다) ---
rem     폐쇄망 번들에는 포터블 런타임(python_runtime, node_runtime)이 함께 들어 있습니다.
rem     개발 PC 처럼 없으면 PATH 의 것으로 물러섭니다. 없는 폴더를 PYTHONHOME 으로 잡으면
rem     시스템 파이썬도 기동하지 못하므로, 내장 런타임이 있을 때만 PYTHONHOME 을 건드립니다.
set "PYTHON_BIN="
set "RUNTIME_LABEL=내장 포터블 파이썬 런타임"
if exist "%~dp0python_runtime\python.exe" goto :bundled_python
for /f "delims=" %%p in ('where python 2^>nul') do if not defined PYTHON_BIN set "PYTHON_BIN=%%p"
if not defined PYTHON_BIN (
    echo [X] python_runtime\python.exe 도 PATH 의 python 도 없습니다.
    pause
    exit /b 1
)
echo [!] python_runtime 이 없어 PATH 의 파이썬을 씁니다: %PYTHON_BIN%
echo     requirements.txt 의 패키지가 설치되어 있어야 합니다.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "RUNTIME_LABEL=PATH 의 파이썬"
goto :python_ready
:bundled_python
set "PYTHONHOME=%~dp0python_runtime"
set "PYTHONPATH=%~dp0;%~dp0python_runtime\Lib;%~dp0python_runtime\Lib\site-packages"
set "PATH=%~dp0python_runtime;%~dp0python_runtime\Scripts;%PATH%"
set "PYTHON_BIN=%~dp0python_runtime\python.exe"
:python_ready

set "NODE_BIN="
if exist "%~dp0node_runtime\node.exe" goto :bundled_node
for /f "delims=" %%n in ('where node 2^>nul') do if not defined NODE_BIN set "NODE_BIN=%%n"
if defined NODE_BIN (
    echo [!] node_runtime 이 없어 PATH 의 node 를 씁니다: %NODE_BIN%
) else (
    echo [!] node_runtime\node.exe 도 PATH 의 node 도 없습니다. filesystem / memory MCP 가 비활성화됩니다.
)
goto :node_ready
:bundled_node
set "NODE_BIN=%~dp0node_runtime\node.exe"
set "PATH=%~dp0node_runtime;%PATH%"
:node_ready

rem --- MCP 서버 위치 (conf.json 의 ${VAR:-기본값} 치환에 사용) ---
set "MCP_NODE_HOME=%~dp0mcp_node"
set "MCP_SANDBOX_HOME=%~dp0mcp_sandbox"
set "WORKSPACE_DIR=%~dp0workspace"
if not defined SANDBOX_KERNEL_PYTHON set "SANDBOX_KERNEL_PYTHON=%PYTHON_BIN%"

rem --- 접속 주소는 conf.json 의 app 값을 그대로 읽습니다 (하드코딩 금지) ---
rem     앞의 call 은 지우지 마세요. 명령이 따옴표로 시작하면 cmd 가 맨 앞과 맨 뒤 따옴표를
rem     떼어 내어 명령이 깨집니다.
set "APP_URL="
for /f "usebackq delims=" %%i in (`call "%PYTHON_BIN%" -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"`) do set "APP_URL=%%i"
if not defined APP_URL set "APP_URL=conf.json 의 app 참조"

rem --- 서버가 응답하면 브라우저를 엽니다. 서버는 콘솔을 붙잡고 있으므로
rem     기다리는 일은 별도 프로세스가 합니다 (MADO_NO_BROWSER=1 이면 건너뜁니다).
rem     같은 인자를 그대로 넘겨야 --port 로 포트를 바꿔도 맞는 주소를 엽니다.
if exist "%~dp0open_browser.py" start "" /b "%PYTHON_BIN%" open_browser.py %*

echo [*] %RUNTIME_LABEL%으로 서버를 시작합니다 (%APP_URL%)...
"%PYTHON_BIN%" -m app.main %*

pause
