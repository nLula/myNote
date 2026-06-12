@echo off
setlocal
cd /d "%~dp0"

echo ================================================================
echo  myNote build  ^(PRODUCTION - no console^)
echo ================================================================
echo.

:: Make sure PyInstaller is up-to-date in the venv
echo [1/3] Checking PyInstaller...
venv\Scripts\pip install --quiet --upgrade pyinstaller
if errorlevel 1 (
    echo ERROR: could not install PyInstaller.
    pause & exit /b 1
)

:: Remove previous build artefacts and stale spec/cache
if exist build       rmdir /s /q build
if exist dist        rmdir /s /q dist
if exist myNote.spec del /f /q myNote.spec

echo [2/3] Running PyInstaller...
echo.

venv\Scripts\pyinstaller ^
    --name myNote ^
    --onedir ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --icon "static\images\myNote.ico" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --collect-all telethon ^
    --collect-all cryptography ^
    --collect-all webview ^
    --hidden-import=flask ^
    --hidden-import=werkzeug ^
    --hidden-import=werkzeug.serving ^
    --hidden-import=werkzeug.routing ^
    --hidden-import=jinja2 ^
    --hidden-import=jinja2.ext ^
    --hidden-import=requests ^
    --hidden-import=fpdf ^
    --hidden-import=sqlite3 ^
    --hidden-import=ctypes ^
    --hidden-import=ctypes.wintypes ^
    --hidden-import=webview.platforms.winforms ^
    --hidden-import=webview.platforms.edgechromium ^
    --hidden-import=clr ^
    --hidden-import=bottle ^
    myNote.py

if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    pause & exit /b 1
)

echo.
echo [3/3] Done.
echo.
echo  Executable : dist\myNote\myNote.exe
echo  Run it     : dist\myNote\myNote.exe
echo  ^(no console window - production build^)
echo.
pause
