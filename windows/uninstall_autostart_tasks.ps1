Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$tasks = @("ScanPerfTracker", "ScanPerfWorkflowRunner", "ScanPerfDashboard")
foreach ($t in $tasks) {
  try {
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop | Out-Null
    Write-Host "Removed: $t"
  } catch {
    Write-Host "Not found: $t"
  }
}
