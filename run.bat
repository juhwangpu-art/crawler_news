@echo off
REM 뉴스 모니터링 대시보드 실행
cd /d "%~dp0"
python -m streamlit run app.py
pause
