Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$outLog = Join-Path $LogsDir "tracker.out.log"
$errLog = Join-Path $LogsDir "tracker.err.log"
$superLog = Join-Path $LogsDir "tracker.supervisor.log"

function Get-ProcByCmdLike([string]$needle) {
  try {
    return Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -as [string]) -like "*$needle*" }
  } catch { return @() }
}

function Stop-ExistingTracker {
  $procs = Get-ProcByCmdLike "tracker.py"
  foreach ($p in $procs) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
  }
}

function Get-SlotMinutes {
  try {
    $envPath = Join-Path $RepoRoot ".env"
    if (Test-Path $envPath) {
      $lines = Get-Content $envPath -ErrorAction SilentlyContinue
      foreach ($line in $lines) {
        $s = ($line -as [string]).Trim()
        if ($s -and -not $s.StartsWith('#') -and $s.StartsWith('TRACKER_SLOT_MINUTES=')) {
          $v = $s.Split('=',2)[1].Trim(); $v = $v.Trim('\"'); $v = $v.Trim("'")
          $m = [int]$v
          if ($m -ge 1 -and $m -le 60) { return $m }
        }
      }
    }
  } catch {}
  return 20
}

function Get-HeartbeatAgeMinutes {
  try {
    $code = @'
from sqlalchemy import create_engine, text
from db_config import DB_CONNECTION_STRING
import datetime
eng=create_engine(DB_CONNECTION_STRING)
with eng.connect() as c:
    row=c.execute(text("SELECT last_started_at_utc, last_finished_at_utc, last_status FROM tracker_heartbeat WHERE name='default' LIMIT 1")).fetchone()
if not row:
    print(-1)
else:
    started, finished, st = row
    ref = finished or started
    if not ref:
        print(-1)
    else:
        try:
            dt = ref
            if isinstance(dt, datetime.datetime):
                age=(datetime.datetime.utcnow()-dt).total_seconds()/60.0
            else:
                age=-1
        except Exception:
            age=-1
        print(age)
'@
    $out = & $Py -c $code 2>&1
    if ($LASTEXITCODE -ne 0) {
      Add-Content -Path $superLog -Value ("[{0}] heartbeat query error: {1}" -f (Get-Date).ToString("s"), ($out | Select-Object -First 6 | Out-String).Trim())
      return -1
    }
    return [double]::Parse(($out | Select-Object -First 1))
  } catch { return -1 }
}

Stop-ExistingTracker

while ($true) {
  try {
    $slot = Get-SlotMinutes
    $stale = (2 * $slot) + 5
    Add-Content -Path $superLog -Value ("[{0}] starting tracker (slot={1} stale>{2}m)" -f (Get-Date).ToString("s"), $slot, $stale)

    $p = Start-Process -FilePath $Py -ArgumentList @("tracker.py", "--quiet") -WorkingDirectory $RepoRoot `
      -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    $procStart = Get-Date

    while (-not $p.HasExited) {
      Start-Sleep -Seconds 60
      # Grace period after start to avoid false positives
      $sinceStart = ((Get-Date) - $procStart).TotalMinutes
      if ($sinceStart -lt 3) { continue }
      $age = Get-HeartbeatAgeMinutes
      if ($age -lt 0) {
        Add-Content -Path $superLog -Value ("[{0}] heartbeat check failed (DB unreachable or no row yet)" -f (Get-Date).ToString("s"))
        continue
      }
      if ($age -ge $stale) {
        Add-Content -Path $superLog -Value ("[{0}] heartbeat stale ({1:n1}m) -> restarting" -f (Get-Date).ToString("s"), $age)
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
        break
      }
    }

    Add-Content -Path $superLog -Value ("[{0}] tracker exited/restarted" -f (Get-Date).ToString("s"))
  } catch {
    Add-Content -Path $superLog -Value ("[{0}] supervisor error: {1}" -f (Get-Date).ToString("s"), $_)
  }

  Start-Sleep -Seconds 10
}
