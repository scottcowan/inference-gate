#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$uvicorn = Join-Path $Root ".venv\Scripts\uvicorn.exe"
if (-not (Test-Path $uvicorn)) {
    throw "uvicorn not found at $uvicorn - create the venv and pip install -e ."
}

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdout = Join-Path $logDir "gate-stdout.log"
$stderr = Join-Path $logDir "gate-stderr.log"

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -lt 500) { break }
    } catch {
        Start-Sleep -Seconds 2
    }
}

$existing = Get-NetTCPConnection -LocalPort 11435 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Add-Content -Path $stderr -Value "$(Get-Date -Format o) port 11435 already in use - not starting another gate"
    exit 0
}

$uvicornArgs = @(
    "app.main:app",
    "--host", "0.0.0.0",
    "--port", "11435",
    "--log-level", "info"
)

$p = Start-Process -FilePath $uvicorn `
    -ArgumentList $uvicornArgs `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Add-Content -Path (Join-Path $logDir "gate-start.log") `
    -Value "$(Get-Date -Format o) started pid=$($p.Id)"
