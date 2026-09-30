@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 or newer is required. Install from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install PyInstaller==6.22.0
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name SubtitleCleaner app.py
if errorlevel 1 goto failed
echo Build complete: dist\SubtitleCleaner.exe
start "" "dist"
pause
exit /b 0
:failed
echo Build failed. See the error above.
pause
exit /b 1
