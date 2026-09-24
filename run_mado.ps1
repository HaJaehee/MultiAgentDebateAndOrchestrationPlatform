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

$env:PYTHONHOME = Join-Path $RootDir "python_runtime"
$env:PYTHONPATH = "$RootDir;$(Join-Path $RootDir 'python_runtime\Lib');$(Join-Path $RootDir 'python_runtime\Lib\site-packages')"
$env:PATH = "$(Join-Path $RootDir 'python_runtime');$(Join-Path $RootDir 'python_runtime\Scripts');$(Join-Path $RootDir 'node_runtime');" + $env:PATH
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# --- MCP 서버 실행 경로 (conf.json 의 ${VAR:-기본값} 치환에 사용) ---
$env:PYTHON_BIN = Join-Path $RootDir "python_runtime\python.exe"
$env:NODE_BIN = Join-Path $RootDir "node_runtime\node.exe"
$env:MCP_NODE_HOME = Join-Path $RootDir "mcp_node"
$env:MCP_SANDBOX_HOME = Join-Path $RootDir "mcp_sandbox"
$env:WORKSPACE_DIR = Join-Path $RootDir "workspace"
if (-not $env:SANDBOX_KERNEL_PYTHON) { $env:SANDBOX_KERNEL_PYTHON = $env:PYTHON_BIN }

if (-not (Test-Path $env:NODE_BIN)) {
    Write-Warning "node_runtime\node.exe 가 없습니다. filesystem / memory MCP 가 비활성화됩니다."
}

# 접속 주소는 conf.json 의 app 값을 그대로 읽습니다 (하드코딩 금지)
$AppUrl = try {
    & $env:PYTHON_BIN -c "from app.config import get_config;c=get_config().app;print(f'http://{c.host}:{c.port}')"
} catch { "conf.json 의 app 참조" }

# 서버가 응답하면 브라우저를 엽니다. 서버는 이 콘솔을 붙잡고 있으므로 기다리는
# 일은 별도 프로세스가 합니다 (MADO_NO_BROWSER=1 이면 건너뜁니다). 같은 인자를
# 그대로 넘겨야 --port 로 포트를 바꿔도 맞는 주소를 엽니다.
if (Test-Path (Join-Path $RootDir "open_browser.py")) {
    Start-Process -FilePath $env:PYTHON_BIN -ArgumentList (@("open_browser.py") + $args) -WorkingDirectory $RootDir -WindowStyle Hidden | Out-Null
}

Write-Host "[*] 내장 파이썬 런타임으로 서버를 시작합니다 ($AppUrl)..." -ForegroundColor Green
& $env:PYTHON_BIN -m app.main $args
