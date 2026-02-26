Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# Prefer local venv if present
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$outLog = Join-Path $LogsDir "workflow_runner.out.log"
$errLog = Join-Path $LogsDir "workflow_runner.err.log"
$superLog = Join-Path $LogsDir "workflow_runner.supervisor.log"

function Get-ProcByCmdLike([string]$needle) {
  try {
    return Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -as [string]) -like "*$needle*" }
  } catch { return @() }
}

function Stop-ExistingRunner {
  $procs = Get-ProcByCmdLike "workflow_runner.py"
  foreach ($p in $procs) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
  }
}

function Get-HeartbeatAgeMinutes {
  try {
    $code = @'
from sqlalchemy import create_engine, text
from db_config import DB_CONNECTION_STRING
import datetime
eng=create_engine(DB_CONNECTION_STRING)
with eng.connect() as c:
    row=c.execute(text("SELECT last_finished_at_utc FROM tracker_heartbeat WHERE name='workflow_runner' LIMIT 1")).fetchone()
    v=row[0] if row else None
if not v:
    print(999999)
else:
    if isinstance(v, str):
        v=v.replace('Z','').replace('T',' ')
    try:
        # MySQL TIMESTAMP comes as datetime already
        dt=v
        if isinstance(dt, datetime.datetime):
            age=(datetime.datetime.utcnow()-dt).total_seconds()/60.0
        else:
            age=999999
    except Exception:
        age=999999
    print(age)
'@
    $age = & $Py -c $code 2>$null
    return [double]::Parse(($age | Select-Object -First 1))
  } catch { return 999999 }
}

Stop-ExistingRunner

# Supervisor loop: restart on crash, and if heartbeat stops updating.
while ($true) {
  try {
    Add-Content -Path $superLog -Value ("[{0}] starting workflow_runner" -f (Get-Date).ToString("s"))
    $p = Start-Process -FilePath $Py -ArgumentList @("workflow_runner.py", "--quiet") -WorkingDirectory $RepoRoot `
      -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru

    while (-not $p.HasExited) {
      Start-Sleep -Seconds 60
      $age = Get-HeartbeatAgeMinutes
      if ($age -ge 6) {
        Add-Content -Path $superLog -Value ("[{0}] heartbeat stale ({1:n1}m) -> restarting" -f (Get-Date).ToString("s"), $age)
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
        break
      }
    }

    Add-Content -Path $superLog -Value ("[{0}] workflow_runner exited/restarted" -f (Get-Date).ToString("s"))
  } catch {
    Add-Content -Path $superLog -Value ("[{0}] supervisor error: {1}" -f (Get-Date).ToString("s"), $_)
  }

  Start-Sleep -Seconds 10
}
