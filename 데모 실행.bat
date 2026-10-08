@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw "%~dp0app.py" --demo
    exit /b
)
where py >nul 2>nul
if %errorlevel% equ 0 (
    start "" pyw -3 "%~dp0app.py" --demo
    exit /b
)
echo Python 3.10 or later is required. Please install Python with Tk support.
pause
