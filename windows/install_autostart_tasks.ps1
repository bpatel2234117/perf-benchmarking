Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot

$DashScript = Join-Path $PSScriptRoot "run_dashboard.ps1"
$TrackerScript = Join-Path $PSScriptRoot "run_tracker.ps1"
$RunnerScript = Join-Path $PSScriptRoot "run_workflow_runner.ps1"

if (-not (Test-Path $DashScript)) { throw "Missing $DashScript" }
if (-not (Test-Path $TrackerScript)) { throw "Missing $TrackerScript" }
if (-not (Test-Path $RunnerScript)) { throw "Missing $RunnerScript" }

$taskDash = "ScanPerfDashboard"
$taskTracker = "ScanPerfTracker"
$taskRunner = "ScanPerfWorkflowRunner"

function Register-LogonTask {
  param(
    [string]$TaskName,
    [string]$ScriptPath,
    [int]$DelaySeconds = 30
  )

  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $ScriptPath)
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  if ($DelaySeconds -gt 0) {
    $trigger.Delay = (New-TimeSpan -Seconds $DelaySeconds)
  }
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType InteractiveToken -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

  try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null } catch {}
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}

Register-LogonTask -TaskName $taskTracker -ScriptPath $TrackerScript -DelaySeconds 10
Register-LogonTask -TaskName $taskRunner -ScriptPath $RunnerScript -DelaySeconds 20
Register-LogonTask -TaskName $taskDash -ScriptPath $DashScript -DelaySeconds 30

Write-Host "Installed logon tasks (interactive, VPN-friendly):"
Write-Host " - $taskTracker"
Write-Host " - $taskRunner"
Write-Host " - $taskDash"
Write-Host ""
Write-Host "Logs are written under: $(Join-Path $RepoRoot 'logs')"
