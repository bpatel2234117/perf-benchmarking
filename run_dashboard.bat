@echo off
REM Windows launcher for BigID Benchmark Dashboard
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m streamlit run dashboard.py
