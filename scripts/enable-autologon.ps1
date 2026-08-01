#Requires -Version 5.1
<#
.SYNOPSIS
  Enable Windows auto-login for the current user (homelab / gaming PC).

  Prefers Sysinternals Autologon (stores password in LSA secrets).
  Falls back to launching Autologon64.exe GUI if present under tools\.

  Security: anyone with physical access can use this PC without a password.
  Disable later with Autologon -> Disable, or:
    Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' AutoAdminLogon 0
#>
$ErrorActionPreference = "Stop"

$tools = Join-Path (Split-Path $PSScriptRoot -Parent) "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$exe = Join-Path $tools "Autologon64.exe"

if (-not (Test-Path $exe)) {
    Write-Host "Downloading Sysinternals Autologon64.exe ..."
    Invoke-WebRequest -Uri "https://live.sysinternals.com/Autologon64.exe" -OutFile $exe -UseBasicParsing
}

Write-Host @"
Launching Autologon.

1. Accept the EULA if prompted
2. Username should be: $env:USERNAME
3. Enter your Windows password
4. Click Enable

After the next reboot you will land on the desktop without signing in.
Ollama Startup + InferenceGate (At logon) will then start automatically.
"@

Start-Process $exe -WorkingDirectory $tools
