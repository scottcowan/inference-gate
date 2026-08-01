#Requires -Version 5.1
<#
.SYNOPSIS
  Register inference-gate to start at Windows logon (Scheduled Task).
  Ollama is expected via its own Startup shortcut (Ollama.lnk); this script
  only verifies that and registers the gate.
#>
$ErrorActionPreference = "Stop"

$Root = Split-Path $PSScriptRoot -Parent
$startScript = Join-Path $PSScriptRoot "start-gate.ps1"
$taskName = "InferenceGate"

if (-not (Test-Path $startScript)) {
    throw "Missing $startScript"
}

# Confirm Ollama is set to start with Windows (user-level Startup folder).
$ollamaStartup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Ollama.lnk"
if (Test-Path $ollamaStartup) {
    Write-Host "Ollama Startup shortcut: OK ($ollamaStartup)"
} else {
    Write-Warning @"
Ollama Startup shortcut not found at:
  $ollamaStartup
Enable 'Start Ollama on login' in the Ollama tray app, or copy Ollama.lnk into your Startup folder.
The gate waits ~90s for Ollama on boot; without it, first requests will fail until Ollama is started.
"@
}

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`""

$action = New-ScheduledTaskAction -Execute $ps -Argument $arg -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Slight delay so desktop / Ollama tray can come up first
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Start inference-gate (uvicorn :11435) at logon" `
    -Force | Out-Null

Write-Host "Scheduled task '$taskName' registered for user $env:USERNAME (AtLogOn + 30s delay)."
Write-Host "Logs: $Root\logs\"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start now:   Start-ScheduledTask -TaskName $taskName"
Write-Host "  Stop gate:   Get-Process uvicorn | Stop-Process"
Write-Host "  Unregister:  Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
Write-Host "  Status:      Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo"
