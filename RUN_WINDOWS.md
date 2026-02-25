# Run on Windows

1) Install Python 3.10+ and add it to PATH.
2) Open Command Prompt in this folder.
3) Run:

```
run_dashboard.bat
```

If you want to set DB credentials, edit `db_config.py` before running.

## Run in background on every reboot (recommended)

This installs two Windows Task Scheduler tasks:
- **ScanPerfTracker**: runs `tracker.py` in the background
- **ScanPerfDashboard**: runs Streamlit `dashboard.py` in the background

Steps (PowerShell as Administrator):

```powershell
cd C:\performanceBenchmark
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install_autostart_tasks.ps1
```

Logs:
- `C:\performanceBenchmark\logs\tracker.out.log`
- `C:\performanceBenchmark\logs\tracker.err.log`
- `C:\performanceBenchmark\logs\dashboard.out.log`
- `C:\performanceBenchmark\logs\dashboard.err.log`

To uninstall tasks:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\uninstall_autostart_tasks.ps1
```
