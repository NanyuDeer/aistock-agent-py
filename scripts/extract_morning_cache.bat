@echo off
chcp 65001 >nul
cd /d "D:\ai_stock_app\aistock-agent-py"
set PYTHONPATH=src
.venv\Scripts\python.exe scripts\extract_morning_cache.py %*
