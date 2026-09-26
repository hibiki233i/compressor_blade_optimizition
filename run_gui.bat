@echo off
rem Launch the blade-shape active-learning GUI on Windows.
rem Double-click this file, or run it from a terminal to pass extra options.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

python -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo [i] PySide6 not found. Installing GUI dependencies...
    python -m pip install -r requirements-gui.txt
    if errorlevel 1 (
        echo [!] Install failed. Run: python -m pip install PySide6
        pause
        exit /b 1
    )
)

python -m blade_gui %*
if errorlevel 1 pause
endlocal
