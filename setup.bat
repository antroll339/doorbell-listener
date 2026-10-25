@echo off
setlocal
cd /d "%~dp0"
echo Doorbell Listener - local setup
echo Requires 64-bit Python 3.12 or newer, including Tcl/Tk.
echo.
set "PYTHON_CMD=py -3"
%PYTHON_CMD% -c "import sys" >nul 2>&1
if errorlevel 1 set "PYTHON_CMD=python"
%PYTHON_CMD% -c "import sys,struct,tkinter; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 (
  echo Install 64-bit Python 3.12 or newer from https://www.python.org/downloads/windows/
  echo Include Tcl/Tk and the Python launcher in the installer, then run setup again.
  goto failed
)
%PYTHON_CMD% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import tkinter,numpy,sounddevice,pystray; from PIL import Image; print('Dependencies ready.')"
if errorlevel 1 goto failed
echo.
echo Setup complete. Double-click run.vbs to start without a console.
echo You can also use run.bat or .venv\Scripts\python.exe widget.py
if not "%~1"=="--no-pause" pause
exit /b 0
:failed
echo.
echo Setup did not complete. Read the message above and try again.
if not "%~1"=="--no-pause" pause
exit /b 1
