Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot

$DashScript = Join-Path $PSScriptRoot "run_dashboard.ps1"
$TrackerScript = Join-Path $PSScriptRoot "run_tracker.ps1"

if (-not (Test-Path $DashScript)) { throw "Missing $DashScript" }
if (-not (Test-Path $TrackerScript)) { throw "Missing $TrackerScript" }

$taskDash = "ScanPerfDashboard"
$taskTracker = "ScanPerfTracker"

function Test-TaskExists {
  param([string]$TaskName)
  # schtasks.exe prints "ERROR: The system cannot find the file specified." for missing tasks.
  # In newer PowerShell, native command failures can be treated as terminating errors.
  # Use cmd.exe and redirect output to avoid hard-failing the script.
  cmd.exe /c "schtasks.exe /Query /TN ""$TaskName"" >nul 2>nul"
  return ($LASTEXITCODE -eq 0)
}

function Register-StartupTask {
  param(
    [string]$TaskName,
    [string]$ScriptPath,
    [int]$DelaySeconds = 30
  )

  $cmd = "powershell.exe"
  $cmdArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

  # Remove existing task if present (idempotent)
  if (Test-TaskExists -TaskName $TaskName) {
    schtasks.exe /Delete /TN $TaskName /F | Out-Null
  }

  # schtasks.exe expects /DELAY in mmmm:ss format (4-digit minutes)
  $delay = ("{0:0000}:{1:00}" -f [Math]::Floor($DelaySeconds / 60), ($DelaySeconds % 60))
  # Run at startup, run whether user is logged on or not, with highest privileges
  schtasks.exe /Create /TN $TaskName /SC ONSTART /DELAY $delay /RL HIGHEST /F `
    /TR "`"$cmd`" $cmdArgs" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create task: $TaskName (exit=$LASTEXITCODE)"
  }
}

Register-StartupTask -TaskName $taskTracker -ScriptPath $TrackerScript -DelaySeconds 20
Register-StartupTask -TaskName $taskDash -ScriptPath $DashScript -DelaySeconds 30

Write-Host "Installed startup tasks:"
Write-Host " - $taskTracker"
Write-Host " - $taskDash"
Write-Host ""
Write-Host "Logs are written under: $(Join-Path $RepoRoot 'logs')"

