@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PYTHONPATH=src
.venv\Scripts\python.exe scripts\run_wind_leader_test.py