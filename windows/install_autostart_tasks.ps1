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

$userId = $env:USERNAME
if ($env:USERDOMAIN) { $userId = "$env:USERDOMAIN\$env:USERNAME" }

function To-IsoDelay {
  param([int]$Seconds)
  $s = [Math]::Max(0, [int]$Seconds)
  $h = [Math]::Floor($s / 3600)
  $s = $s % 3600
  $m = [Math]::Floor($s / 60)
  $s = $s % 60
  $out = "PT"
  if ($h -gt 0) { $out += "${h}H" }
  if ($m -gt 0) { $out += "${m}M" }
  if ($s -gt 0 -or ($h -eq 0 -and $m -eq 0)) { $out += "${s}S" }
  return $out
}

function Register-LogonTask {
  param(
    [string]$TaskName,
    [string]$ScriptPath,
    [int]$DelaySeconds = 30
  )

  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ("-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $ScriptPath)
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  if ($DelaySeconds -gt 0) { $trigger.Delay = (To-IsoDelay -Seconds $DelaySeconds) }
  $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

  try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null } catch {}
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}

Register-LogonTask -TaskName $taskTracker -ScriptPath $TrackerScript -DelaySeconds 10
Register-LogonTask -TaskName $taskRunner -ScriptPath $RunnerScript -DelaySeconds 20
Register-LogonTask -TaskName $taskDash -ScriptPath $DashScript -DelaySeconds 30

Write-Host "Installed logon tasks (interactive user session):"
Write-Host " - $taskTracker"
Write-Host " - $taskRunner"
Write-Host " - $taskDash"
Write-Host ""
Write-Host "Logs are written under: $(Join-Path $RepoRoot 'logs')"
