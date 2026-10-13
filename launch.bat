@echo off
setlocal EnableDelayedExpansion
title ArbForge - Triangular Arbitrage Terminal

cd /d "%~dp0"

echo =====================================================================
echo                     ARBFORGE QUANTITATIVE SYSTEMS
echo         Real-Time Orderbook Depth Scanner and Arbitrage Engine
echo =====================================================================
echo.

set "PY_CMD="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :PYTHON_FOUND
)

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
    goto :PYTHON_FOUND
)

echo [ERROR] Python 3.10+ was not found on your system PATH.
echo Please install Python 3.11 from https://www.python.org/
echo Make sure to check 'Add Python to PATH' during installation.
echo.
pause
exit /b 1

:PYTHON_FOUND
for /f "tokens=*" %%v in ('!PY_CMD! --version 2^>^&1') do set "PY_VER=%%v"
echo [INFO] Detected: !PY_VER!

if exist ".venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment .venv...
    call ".venv\Scripts\activate.bat"
    set "PY_CMD=python"
) else if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment venv...
    call "venv\Scripts\activate.bat"
    set "PY_CMD=python"
)

!PY_CMD! -c "import fastapi, uvicorn, aiohttp, httpx, pydantic" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Required dependencies missing. Installing from requirements.txt...
    !PY_CMD! -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Dependency installation failed.
        pause
        exit /b 1
    )
)

echo [INFO] Environment verified successfully.

:: Free port 8000 if occupied by previous session
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 "') do (
    if not "%%a"=="" (
        echo [INFO] Reclaiming port 8000 from stale process PID %%a...
        taskkill /F /PID %%a >nul 2>&1
    )
)

echo [INFO] Starting ArbForge Engine at http://127.0.0.1:8000 ...
echo [INFO] Opening Financial Terminal Dashboard in your browser...
echo.
echo [TIP]  Press Ctrl+C to stop the server at any time.
echo ---------------------------------------------------------------------

start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'" >nul 2>&1

!PY_CMD! app.py

echo.
echo [INFO] ArbForge engine has stopped.
pause
