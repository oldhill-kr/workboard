@echo off
rem 서버가 꺼져 있으면 켜고, 켜져 있으면 브라우저만 엽니다.
cd /d "%~dp0"
start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" server.py
