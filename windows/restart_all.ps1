# Restart all: stop Dashboard, Tracker, Workflow Runner; start again.
# Run: .\windows\restart_all.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot
$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

function Get-ProcByCmdLike([string]$needle) {
  try { return Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -as [string]) -like "*$needle*" } } catch { return @() }
}

Write-Host "Stopping existing..."
foreach ($needle in @("streamlit run dashboard.py", "tracker.py", "run_tracker.ps1", "workflow_runner.py", "run_workflow_runner.ps1")) {
  foreach ($p in (Get-ProcByCmdLike $needle)) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
}
Start-Sleep -Seconds 2

Write-Host "Starting Dashboard..."
Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",(Join-Path $ScriptDir "run_dashboard.ps1") -WorkingDirectory $RepoRoot -WindowStyle Hidden
Start-Sleep -Seconds 2
Write-Host "Starting Tracker..."
Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",(Join-Path $ScriptDir "run_tracker.ps1") -WorkingDirectory $RepoRoot -WindowStyle Hidden
Start-Sleep -Seconds 2
Write-Host "Starting Workflow Runner..."
Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",(Join-Path $ScriptDir "run_workflow_runner.ps1") -WorkingDirectory $RepoRoot -WindowStyle Hidden
Write-Host "Done. Dashboard: http://localhost:8501  Logs: $LogsDir"
