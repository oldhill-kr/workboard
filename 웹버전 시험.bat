@echo off
chcp 65001 >nul
rem 서버 없이 웹 모드로 시험하려고 정적 서버를 켜고 브라우저를 엽니다. 끄려면 이 창을 닫으세요.
cd /d "%~dp0"
start "" http://127.0.0.1:8800/index.html
python -m http.server 8800 --bind 127.0.0.1
