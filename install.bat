@echo off
setlocal enabledelayedexpansion

rem javsubs.lua installer for mpv (Windows)
rem Copies the script into %APPDATA%\mpv\scripts and the config into script-opts.

set "SRC=%~dp0"
set "DEST=%APPDATA%\mpv"

echo.
echo   mpv-javsubs installer
echo   ---------------------
echo   target: %DEST%
echo.

if not exist "%DEST%\scripts" mkdir "%DEST%\scripts"
if not exist "%DEST%\script-opts" mkdir "%DEST%\script-opts"

if not exist "%SRC%scripts\javsubs.lua" (
    echo [ERROR] scripts\javsubs.lua not found next to this installer.
    goto :end
)

copy /Y "%SRC%scripts\javsubs.lua" "%DEST%\scripts\javsubs.lua" >nul
if errorlevel 1 (
    echo [ERROR] failed to copy javsubs.lua
    goto :end
)
echo [OK] %DEST%\scripts\javsubs.lua

if exist "%DEST%\script-opts\javsubs.conf" (
    echo [SKIP] javsubs.conf already exists, keeping your settings.
    echo        new default config left at: %SRC%script-opts\javsubs.conf
) else (
    copy /Y "%SRC%script-opts\javsubs.conf" "%DEST%\script-opts\javsubs.conf" >nul
    echo [OK] %DEST%\script-opts\javsubs.conf
)

echo.
echo   Done. Restart mpv, open a video, press Ctrl+Shift+s.
echo.
echo   Using portable mpv? Copy instead to:
echo     ^<mpv folder^>\portable_config\scripts\
echo     ^<mpv folder^>\portable_config\script-opts\
echo.

:end
pause
