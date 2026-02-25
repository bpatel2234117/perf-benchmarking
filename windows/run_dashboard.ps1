Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

# Prefer local venv if present
$Py = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

$outLog = Join-Path $LogsDir "dashboard.out.log"
$errLog = Join-Path $LogsDir "dashboard.err.log"

# Install deps best-effort (won't fail autostart if pip is transient)
try {
  & $Py -m pip install -r (Join-Path $RepoRoot "requirements.txt") *>> $outLog
} catch {}

$args = @(
  "-m", "streamlit", "run", "dashboard.py",
  "--server.headless", "true",
  "--server.port", ($env:STREAMLIT_PORT ? $env:STREAMLIT_PORT : "8501"),
  "--server.address", ($env:STREAMLIT_ADDRESS ? $env:STREAMLIT_ADDRESS : "0.0.0.0")
)

Start-Process -FilePath $Py -ArgumentList $args -WorkingDirectory $RepoRoot `
  -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog

