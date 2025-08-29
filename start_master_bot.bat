@echo off
REM Master Bot Auto-Start Script
echo Starting Deadlock Master Bot...

REM Change to project directory
cd /d "C:\Users\Nani-Admin\Documents\Deadlock"

REM Activate virtual environment and start bot
call "C:\Users\Nani-Admin\Documents\.venv\Scripts\activate.bat"
python main_bot.py

REM If bot crashes, wait 10 seconds and restart
timeout /t 10
goto :eof