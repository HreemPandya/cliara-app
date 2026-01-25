#!/bin/bash
# Natural Language Macros - Quick Start Script for Unix/Linux/macOS

echo ""
echo "==============================================="
echo "  Natural Language Macros - Quick Start"
echo "==============================================="
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed or not in PATH"
    echo "Please install Python 3.8+"
    exit 1
fi

echo "[1/3] Checking dependencies..."
if ! python3 -c "import thefuzz" 2>/dev/null; then
    echo "Installing dependencies..."
    pip3 install -q thefuzz python-Levenshtein
fi

echo "[2/3] Setting up demo macros..."
python3 setup_demo.py

echo ""
echo "[3/3] Starting Natural Language Macros CLI..."
echo ""
echo "Press Ctrl+C or type 'exit' to quit"
echo ""

python3 -m app.main
