@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo ========================================
echo 播报 Agent 测试脚本
echo ========================================
echo.

set PYTHONPATH=src
python scripts\run_broadcast_test.py

echo.
pause