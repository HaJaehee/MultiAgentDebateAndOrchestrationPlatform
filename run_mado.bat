@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   MADO: Multi-Agent Debate ^& Orchestration Platform (오프라인 / 폐쇄망 모드)
echo ==========================================================

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem --- 실행 런타임 설정 (conf.json 의 ${PYTHON_BIN} / ${NODE_BIN} 치환에도 사용됩니다) ---
rem     폐쇄망 배포 번들에는 포터블 런타임(python_runtime, node_runtime)이 동봉되어 있습니다.
rem     개발 환경 등 내장 런타임이 없는 경우에는 PATH 에 등록된 런타임으로 대체하여 실행합니다.
rem     존재하지 않는 폴더를 PYTHONHOME 으로 지정하면 시스템 Python 조차 기동되지 않으므로,
rem     내장 런타임이 존재할 때에만 PYTHONHOME 을 설정합니다.
set "PYTHON_BIN="
set "RUNTIME_LABEL=내장 포터블 Python 런타임"
if exist "%~dp0python_runtime\python.exe" goto :bundled_python
for /f "delims=" %%p in ('where python 2^>nul') do if not defined PYTHON_BIN set "PYTHON_BIN=%%p"
if not defined PYTHON_BIN (
    echo [X] python_runtime\python.exe 및 PATH 의 python 을 찾을 수 없습니다.
    pause
    exit /b 1
)
echo [!] python_runtime 이 존재하지 않아 PATH 에 등록된 Python 을 사용합니다: %PYTHON_BIN%
echo     requirements.txt 에 명시된 패키지가 미리 설치되어 있어야 합니다.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "RUNTIME_LABEL=PATH 의 Python"
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
    echo [!] node_runtime 이 존재하지 않아 PATH 에 등록된 Node 를 사용합니다: %NODE_BIN%
) else (
    echo [!] node_runtime\node.exe 및 PATH 의 node 를 찾을 수 없습니다. filesystem 및 memory MCP 서버가 비활성화됩니다.
)
goto :node_ready
:bundled_node
set "NODE_BIN=%~dp0node_runtime\node.exe"
set "PATH=%~dp0node_runtime;%PATH%"
:node_ready

rem --- MCP 서버 위치 (conf.json 의 ${VAR:-기본값} 치환에 사용됩니다) ---
set "MCP_NODE_HOME=%~dp0mcp_node"
set "MCP_SANDBOX_HOME=%~dp0mcp_sandbox"
set "WORKSPACE_DIR=%~dp0workspace"
if not defined SANDBOX_KERNEL_PYTHON set "SANDBOX_KERNEL_PYTHON=%PYTHON_BIN%"

rem --- 접속 주소는 conf.json 의 app 설정을 그대로 참조합니다 (하드코딩 금지) ---
rem     앞의 call 명령을 제거하지 마십시오. 명령이 큰따옴표로 시작하면 cmd.exe 가
rem     양 끝 따옴표를 임의로 제거하여 명령 구문이 손상될 수 있습니다.
set "APP_URL="
for /f "usebackq delims=" %%i in (`call "%PYTHON_BIN%" -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"`) do set "APP_URL=%%i"
if not defined APP_URL set "APP_URL=conf.json 의 app 설정 참조"

rem --- 서버가 응답하면 브라우저를 엽니다. 서버는 콘솔을 점유하므로
rem     대기 및 실행 작업은 별도 프로세스가 수행합니다 (MADO_NO_BROWSER=1 인 경우 생략됩니다).
rem     동일한 인수를 그대로 전달해야 --port 로 포트를 변경하더라도 올바른 주소로 접속합니다.
if exist "%~dp0open_browser.py" start "" /b "%PYTHON_BIN%" open_browser.py %*

echo [*] %RUNTIME_LABEL% 환경에서 서버를 시작합니다 (%APP_URL%)...
"%PYTHON_BIN%" -m app.main %*

pause
