@echo off
REM Natural Language Macros - Interactive Demo
REM This script demonstrates the tool with automatic input

echo.
echo ===============================================
echo   Natural Language Macros - Live Demo
echo ===============================================
echo.
echo This demo will show you:
echo   1. Creating macros
echo   2. Running macros
echo   3. Using variables
echo   4. Managing macros
echo.
echo Press any key to start...
pause >nul

echo.
echo [Step 1] Installing dependencies...
pip install -q thefuzz python-Levenshtein

echo.
echo [Step 2] Setting up demo macros...
python setup_demo.py

echo.
echo ===============================================
echo   DEMO READY!
echo ===============================================
echo.
echo Now let's try some commands manually:
echo.
echo Try these commands:
echo   1. macros list
echo   2. hello world
echo   3. show info
echo   4. greet YourName
echo   5. repeat 5
echo.
echo Starting the CLI now...
echo Type 'exit' when done exploring!
echo.
pause

python -m app.main
