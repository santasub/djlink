@echo off
cd /d "%~dp0"
.venv\Scripts\python midiclock-qt.py --loglevel info --iface Wi-Fi %*
pause
