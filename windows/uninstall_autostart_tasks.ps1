Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskDash = "ScanPerfDashboard"
$taskTracker = "ScanPerfTracker"

function Test-TaskExists {
  param([string]$TaskName)
  cmd.exe /c "schtasks.exe /Query /TN ""$TaskName"" >nul 2>nul"
  return ($LASTEXITCODE -eq 0)
}

foreach ($t in @($taskTracker, $taskDash)) {
  if (Test-TaskExists -TaskName $t) {
    schtasks.exe /Delete /TN $t /F | Out-Null
    Write-Host "Removed: $t"
  } else {
    Write-Host "Not found: $t"
  }
}

