#Requires -RunAsAdministrator
<#
  inference-gate Windows setup
  Disables GPU hardware acceleration for common background apps that burn VRAM
  while idle. Run once as Administrator before starting inference-gate.

  What this does:
  - Disables Chrome/Edge/Firefox hardware acceleration (registry)
  - Disables Discord hardware acceleration
  - Disables Windows hardware-accelerated GPU scheduling (optional, aggressive)
  - Prints a summary of what changed and what needs a restart

  What this does NOT do:
  - Kill any running processes (relaunch apps yourself after running this)
  - Touch games, creative tools, or anything that legitimately needs the GPU
  - Disable dwm.exe (Windows compositor — cannot be safely disabled)
#>

$changed = @()
$skipped = @()

function Set-RegValue {
    param($Path, $Name, $Value, $Type = "DWord")
    if (-not (Test-Path $Path)) {
        New-Item -Path $Path -Force | Out-Null
    }
    $current = (Get-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue).$Name
    if ($current -ne $Value) {
        Set-ItemProperty -Path $Path -Name $Name -Value $Value -Type $Type
        return $true
    }
    return $false
}

# ── Chrome ────────────────────────────────────────────────────────────────────
$chromePref = "$env:LOCALAPPDATA\Google\Chrome\User Data\Local State"
if (Test-Path $chromePref) {
    $state = Get-Content $chromePref -Raw | ConvertFrom-Json
    if ($state.hardware_acceleration_mode.enabled -ne $false) {
        $state.hardware_acceleration_mode.enabled = $false
        $state | ConvertTo-Json -Depth 20 | Set-Content $chromePref -Encoding UTF8
        $changed += "Chrome: hardware acceleration disabled (relaunch Chrome)"
    } else {
        $skipped += "Chrome: already disabled"
    }
} else {
    $skipped += "Chrome: not found"
}

# ── Microsoft Edge ────────────────────────────────────────────────────────────
$edgePref = "$env:LOCALAPPDATA\Microsoft\Edge\User Data\Local State"
if (Test-Path $edgePref) {
    $state = Get-Content $edgePref -Raw | ConvertFrom-Json
    if ($state.hardware_acceleration_mode.enabled -ne $false) {
        $state.hardware_acceleration_mode.enabled = $false
        $state | ConvertTo-Json -Depth 20 | Set-Content $edgePref -Encoding UTF8
        $changed += "Edge: hardware acceleration disabled (relaunch Edge)"
    } else {
        $skipped += "Edge: already disabled"
    }
} else {
    $skipped += "Edge: not found"
}

# ── Firefox ───────────────────────────────────────────────────────────────────
$firefoxProfiles = "$env:APPDATA\Mozilla\Firefox\Profiles"
if (Test-Path $firefoxProfiles) {
    Get-ChildItem $firefoxProfiles -Filter "prefs.js" -Recurse | ForEach-Object {
        $prefs = Get-Content $_.FullName -Raw
        $key = 'user_pref("layers.acceleration.disabled", true);'
        if ($prefs -notmatch 'layers\.acceleration\.disabled') {
            Add-Content $_.FullName "`n$key"
            $changed += "Firefox ($($_.Directory.Name)): hardware acceleration disabled (relaunch Firefox)"
        } else {
            $skipped += "Firefox ($($_.Directory.Name)): already disabled"
        }
    }
} else {
    $skipped += "Firefox: not found"
}

# ── Discord ───────────────────────────────────────────────────────────────────
$discordSettings = "$env:APPDATA\discord\settings.json"
if (Test-Path $discordSettings) {
    $settings = Get-Content $discordSettings -Raw | ConvertFrom-Json
    if ($settings.HARDWARE_ACCELERATION -ne $false) {
        $settings | Add-Member -MemberType NoteProperty -Name "HARDWARE_ACCELERATION" -Value $false -Force
        $settings | ConvertTo-Json -Depth 10 | Set-Content $discordSettings -Encoding UTF8
        $changed += "Discord: hardware acceleration disabled (relaunch Discord)"
    } else {
        $skipped += "Discord: already disabled"
    }
} else {
    $skipped += "Discord: not found"
}

# ── Spotify ───────────────────────────────────────────────────────────────────
$spotifyPref = "$env:APPDATA\Spotify\prefs"
if (Test-Path $spotifyPref) {
    $prefs = Get-Content $spotifyPref -Raw
    if ($prefs -notmatch 'ui\.hardware_acceleration=false') {
        if ($prefs -match 'ui\.hardware_acceleration=true') {
            $prefs = $prefs -replace 'ui\.hardware_acceleration=true', 'ui.hardware_acceleration=false'
        } else {
            $prefs += "`nui.hardware_acceleration=false"
        }
        Set-Content $spotifyPref $prefs -Encoding UTF8
        $changed += "Spotify: hardware acceleration disabled (relaunch Spotify)"
    } else {
        $skipped += "Spotify: already disabled"
    }
} else {
    $skipped += "Spotify: not found"
}

# ── Hardware-Accelerated GPU Scheduling (HAGS) ────────────────────────────────
# Optional: HAGS can increase VRAM overhead from background apps.
# Uncomment to disable. Requires a reboot.
#
# $hagsPath = "HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
# if (Set-RegValue $hagsPath "HwSchMode" 1) {
#     $changed += "HAGS: disabled (reboot required)"
# } else {
#     $skipped += "HAGS: already disabled"
# }

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== inference-gate Windows setup ===" -ForegroundColor Cyan

if ($changed.Count -gt 0) {
    Write-Host ""
    Write-Host "Changed:" -ForegroundColor Green
    $changed | ForEach-Object { Write-Host "  + $_" -ForegroundColor Green }
}

if ($skipped.Count -gt 0) {
    Write-Host ""
    Write-Host "Skipped (already configured or not installed):" -ForegroundColor Gray
    $skipped | ForEach-Object { Write-Host "  - $_" -ForegroundColor Gray }
}

Write-Host ""
Write-Host "Done. Relaunch any affected apps to free their GPU memory." -ForegroundColor Cyan
Write-Host "Suggested EXTERNAL_VRAM_THRESHOLD_MB=200 in your .env after relaunching." -ForegroundColor Cyan
Write-Host ""
