# MADO: Multi-Agent Debate & Orchestration Platform - Offline Launch Script
$ErrorActionPreference = "Stop"

# 콘솔 입출력 인코딩 UTF-8 설정 (한글 깨짐 방지)
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$RootDir = $PSScriptRoot
if (-not $RootDir) { $RootDir = (Get-Location).Path }

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  MADO: Multi-Agent Debate & Orchestration Platform (오프라인 / 폐쇄망 모드)" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# --- 실행 런타임 설정 (conf.json 의 ${PYTHON_BIN} / ${NODE_BIN} 치환에도 사용됩니다) ---
# 폐쇄망 배포 번들에는 포터블 런타임(python_runtime, node_runtime)이 동봉되어 있습니다.
# 개발 환경 등 내장 런타임이 없는 경우에는 PATH 에 등록된 런타임으로 대체하여 실행합니다.
# 존재하지 않는 폴더를 PYTHONHOME 으로 지정하면 시스템 Python 조차 기동되지 않으므로,
# 내장 런타임이 존재할 때에만 PYTHONHOME 을 설정합니다.
$PythonRuntime = Join-Path $RootDir "python_runtime"
$NodeRuntime = Join-Path $RootDir "node_runtime"

if (Test-Path (Join-Path $PythonRuntime "python.exe")) {
    $env:PYTHONHOME = $PythonRuntime
    $env:PYTHONPATH = "$RootDir;$(Join-Path $PythonRuntime 'Lib');$(Join-Path $PythonRuntime 'Lib\site-packages')"
    $env:PATH = "$PythonRuntime;$(Join-Path $PythonRuntime 'Scripts');" + $env:PATH
    $env:PYTHON_BIN = Join-Path $PythonRuntime "python.exe"
    $RuntimeLabel = "내장 포터블 Python 런타임"
} else {
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $SystemPython) {
        Write-Host "[X] python_runtime\python.exe 및 PATH 의 python 을 찾을 수 없습니다." -ForegroundColor Red
        exit 1
    }
    $env:PYTHON_BIN = $SystemPython.Source
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$RootDir;$env:PYTHONPATH" } else { $RootDir }
    Write-Warning "python_runtime 이 존재하지 않아 PATH 에 등록된 Python($($env:PYTHON_BIN))을 사용합니다. requirements.txt 에 명시된 패키지가 미리 설치되어 있어야 합니다."
    $RuntimeLabel = "PATH 의 Python"
}

if (Test-Path (Join-Path $NodeRuntime "node.exe")) {
    $env:PATH = "$NodeRuntime;" + $env:PATH
    $env:NODE_BIN = Join-Path $NodeRuntime "node.exe"
} else {
    $SystemNode = Get-Command node -ErrorAction SilentlyContinue
    if ($SystemNode) {
        $env:NODE_BIN = $SystemNode.Source
        Write-Warning "node_runtime 이 존재하지 않아 PATH 에 등록된 Node($($env:NODE_BIN))를 사용합니다."
    } else {
        Write-Warning "node_runtime\node.exe 및 PATH 의 node 를 찾을 수 없습니다. filesystem 및 memory MCP 서버가 비활성화됩니다."
    }
}

# --- MCP 서버 위치 (conf.json 의 ${VAR:-기본값} 치환에 사용됩니다) ---
$env:MCP_NODE_HOME = Join-Path $RootDir "mcp_node"
$env:MCP_SANDBOX_HOME = Join-Path $RootDir "mcp_sandbox"
$env:WORKSPACE_DIR = Join-Path $RootDir "workspace"
if (-not $env:SANDBOX_KERNEL_PYTHON) { $env:SANDBOX_KERNEL_PYTHON = $env:PYTHON_BIN }

# 접속 주소는 conf.json 의 app 설정을 그대로 참조합니다 (하드코딩 금지)
$AppUrl = try {
    & $env:PYTHON_BIN -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"
} catch { "conf.json 의 app 설정 참조" }

# 서버가 응답하면 브라우저를 엽니다. 서버는 콘솔을 점유하므로 대기 및
# 실행 작업은 별도 프로세스가 수행합니다 (MADO_NO_BROWSER=1 인 경우 생략됩니다). 동일한
# 인수를 그대로 전달해야 --port 로 포트를 변경하더라도 올바른 주소로 접속합니다.
if (Test-Path (Join-Path $RootDir "open_browser.py")) {
    Start-Process -FilePath $env:PYTHON_BIN -ArgumentList (@("open_browser.py") + $args) -WorkingDirectory $RootDir -WindowStyle Hidden | Out-Null
}

Write-Host "[*] ${RuntimeLabel} 환경에서 서버를 시작합니다 ($AppUrl)..." -ForegroundColor Green
& $env:PYTHON_BIN -m app.main $args
