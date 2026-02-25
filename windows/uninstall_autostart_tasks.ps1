Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskDash = "ScanPerfDashboard"
$taskTracker = "ScanPerfTracker"

foreach ($t in @($taskTracker, $taskDash)) {
  schtasks.exe /Query /TN $t *> $null
  if ($LASTEXITCODE -eq 0) {
    schtasks.exe /Delete /TN $t /F | Out-Null
    Write-Host "Removed: $t"
  } else {
    Write-Host "Not found: $t"
  }
}

