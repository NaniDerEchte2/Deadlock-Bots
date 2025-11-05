@echo off
echo Restarting Steam Bridge System...

REM Kill any existing Node.js processes running the Steam Bridge
taskkill /f /im node.exe 2>nul

REM Wait a moment for cleanup
timeout /t 2 /nobreak >nul

REM Start the new modular Steam Bridge
echo Starting Steam Bridge...
cd /d "%~dp0"
node index.js

pause