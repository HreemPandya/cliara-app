@echo off
REM Natural Language Macros - Quick Start Script for Windows

echo.
echo ===============================================
echo   Natural Language Macros - Quick Start
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

echo [1/3] Checking dependencies...
pip show thefuzz >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -q thefuzz python-Levenshtein
)

echo [2/3] Setting up demo macros...
python setup_demo.py

echo.
echo [3/3] Starting Natural Language Macros CLI...
echo.
echo Press Ctrl+C or type 'exit' to quit
echo.

python -m app.main
