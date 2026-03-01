@echo off
REM Cliara - Quick Start Script for Windows

echo.
echo ===============================================
echo   Cliara - Quick Start
echo ===============================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from python.org
    pause
    exit /b 1
)

echo [1/2] Checking dependencies...
pip show cliara >nul 2>&1
if errorlevel 1 (
    echo Installing Cliara...
    pip install -q -e .
)

echo.
echo [2/2] Starting Cliara...
echo.
echo Press Ctrl+C or type 'exit' to quit
echo.

python -m cliara.main
