@echo off
REM Schneller Bot-Start mit Performance-Optimierungen

echo 🚀 Starting Discord Bot with Performance Optimizations...
echo.

REM Setze Performance-Environment-Variablen
set QUIET_STARTUP=1
set QUIET_COG_LOADS=1
set QUIET_DISCORD=1
set FAST_LOGS=1
set COG_LOAD_CONCURRENCY=6
set HEALTH_CHECK_INTERVAL=900
set QUIET_HEALTH_CHECKS=1

REM WorkerProxy ist nicht mehr vorhanden - kein Setup nötig

REM Benchmark vor dem Start (optional)
if "%1"=="--benchmark" (
    echo 📊 Running startup benchmark...
    python tmp_rovodev_benchmark_startup.py
    echo.
    pause
)

REM Starte den Bot
echo ⚡ Starting bot with optimized settings...
python main_bot.py

pause