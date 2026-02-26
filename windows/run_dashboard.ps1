Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null


function Get-ProcByCmdLike([string]$needle) {
  try {
    return Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -as [string]) -like "*$needle*" }
  } catch { return @() }
}

function Stop-ExistingDashboard {
  $procs = Get-ProcByCmdLike "streamlit run dashboard.py"
  foreach ($p in $procs) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
  }
}

Stop-ExistingDashboard

# Prefer local venv if present
$Py = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$outLog = Join-Path $LogsDir "dashboard.out.log"
$errLog = Join-Path $LogsDir "dashboard.err.log"

# Install deps best-effort (won't fail autostart if pip is transient)
try {
  & $Py -m pip install -r (Join-Path $RepoRoot "requirements.txt") *>> $outLog
} catch {}

$port = "8501"
if ($env:STREAMLIT_PORT) { $port = $env:STREAMLIT_PORT }
$addr = "0.0.0.0"
if ($env:STREAMLIT_ADDRESS) { $addr = $env:STREAMLIT_ADDRESS }

$args = @(
  "-m", "streamlit", "run", "dashboard.py",
  "--server.headless", "true",
  "--server.port", $port,
  "--server.address", $addr
)

Start-Process -FilePath $Py -ArgumentList $args -WorkingDirectory $RepoRoot `
  -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog

