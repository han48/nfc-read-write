@echo off
REM ===========================================================================
REM  build.bat - Build an executable (.exe) of the NFC Desktop App on Windows
REM
REM  Uses PyInstaller to bundle nfc_app.py into a single .exe (onefile), with no
REM  console, and embeds a requireAdministrator manifest (--uac-admin) so that
REM  running it requests Administrator rights via UAC. Then copies lang\ next
REM  to the .exe.
REM
REM  Usage (double-click or from cmd):
REM      build.bat
REM
REM  Output:  dist\nfc_app.exe
REM           dist\lang\*.json   (translations, placed next to the .exe)
REM ===========================================================================
setlocal

set APP_NAME=nfc_app
set ENTRY=nfc_app.py
if "%PYTHON%"=="" set PYTHON=python

echo [*] Python:
"%PYTHON%" --version

REM Install PyInstaller if missing
"%PYTHON%" -c "import PyInstaller" 1>nul 2>nul
if errorlevel 1 (
    echo [*] Installing PyInstaller...
    "%PYTHON%" -m pip install pyinstaller
)

REM Install runtime dependencies
if exist requirements.txt (
    echo [*] Installing requirements...
    "%PYTHON%" -m pip install -r requirements.txt
)

REM Clean previous build artifacts
echo [*] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /q "%APP_NAME%.spec"

REM Build: onefile, no console, embed admin manifest (UAC)
echo [*] Building with PyInstaller...
set ICON_ARG=
if exist icon.ico set ICON_ARG=--icon icon.ico
"%PYTHON%" -m PyInstaller ^
    --onefile ^
    --noconsole ^
    --uac-admin ^
    %ICON_ARG% ^
    --name "%APP_NAME%" ^
    "%ENTRY%"

if errorlevel 1 (
    echo [-] Build failed.
    exit /b 1
)

REM Copy lang\ next to the .exe
if exist lang (
    echo [*] Copying lang\ into dist\...
    xcopy /e /i /y lang dist\lang 1>nul
)

REM Copy icon files next to the .exe (app loads icon.ico/png at runtime)
if exist icon.ico copy /y icon.ico dist\icon.ico 1>nul
if exist icon.png copy /y icon.png dist\icon.png 1>nul

echo.
echo [+] Done! Executable: dist\%APP_NAME%.exe
echo     Run: double-click dist\%APP_NAME%.exe (it will request Administrator rights)
echo     Note: keep the dist\lang folder next to the .exe.
endlocal
