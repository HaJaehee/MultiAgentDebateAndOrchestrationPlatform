# MADO: Multi-Agent Debate & Orchestration Platform - Source & Config Only Packaging Script
# 런타임(포터블 파이썬 / node / wheel / MCP 서버)은 빼고 소스와 설정만 dist/ 에 압축합니다.
$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$RootDir = $PSScriptRoot
if (-not $RootDir) { $RootDir = (Get-Location).Path }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# 인자는 그대로 전달됩니다. 예: .\package_source.ps1 --out-dir dist
python (Join-Path $RootDir "package_source.py") @args

# 패키징이 멈췄으면(필수 파일 누락, 키로 보이는 값 등) 그 실패를 호출한 쪽에 돌려줍니다.
# 이 줄이 없으면 PowerShell 은 python 의 종료 코드와 상관없이 성공으로 끝납니다.
exit $LASTEXITCODE
