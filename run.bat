@echo off
title 🛡️ AI Surveillance & Attendance System Launcher
color 0B
cls

echo ===================================================================
echo   🛡️ AI SURVEILLANCE & ATTENDANCE SYSTEM LAUNCHER
echo ===================================================================
echo.
echo [INFO] Checking Python environment installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found in your system's PATH.
    echo Please install Python and ensure "Add Python to PATH" is checked.
    echo.
    pause
    exit /b
)

echo [INFO] Launching local Flask application...
echo [INFO] Your system console is now active.
echo [INFO] Dashboard url: http://127.0.0.1:5000
echo.
echo [INFO] Automatically launching web browser interface...
timeout /t 2 /nobreak >nul
start http://127.0.0.1:5000

echo [INFO] Starting face recognition engine backend...
echo ===================================================================
echo To stop the server at any time, press [CTRL+C] or close this window.
echo ===================================================================
echo.
python app.py
pause
