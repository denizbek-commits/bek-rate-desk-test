@echo off
:: Bek Rate Desk — Windows launcher
:: Credentials are loaded automatically from .env
cd /d "%~dp0"
start python app.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5001
exit
