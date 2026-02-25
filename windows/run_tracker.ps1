Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

$Py = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$outLog = Join-Path $LogsDir "tracker.out.log"
$errLog = Join-Path $LogsDir "tracker.err.log"

Start-Process -FilePath $Py -ArgumentList @("tracker.py", "--quiet") -WorkingDirectory $RepoRoot `
  -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog

