@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo  GGE Equipment Manager - Windows build
echo ========================================
echo.

py -m pip install --upgrade pip
if errorlevel 1 goto :error

py -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

py -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "GGE Equipment Manager" ^
  --add-data "err.json;." ^
  gui.py
if errorlevel 1 goto :error

echo.
echo Build complete:
echo   dist\GGE Equipment Manager.exe
echo.
echo This EXE contains no saved passwords or account data.
pause
exit /b 0

:error
echo.
echo Build failed. See the messages above.
pause
exit /b 1
