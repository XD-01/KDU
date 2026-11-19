@echo off
setlocal EnableExtensions
chcp 65001 >nul
title KDU Local Build

rem ---------------------------------------------------------------------------
rem  Double-click this file to build KDU with the default platform profile.
rem  Optional arguments are forwarded to build\build_kdu.py, for example:
rem      build_kdu.bat --platform windows-x64-debug
rem      build_kdu.bat --platform windows-x64 --toolset v143
rem ---------------------------------------------------------------------------

set "RC=1"
set "SCRIPT_DIR=%~dp0"
set "PUSHED="
pushd "%SCRIPT_DIR%.." >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Cannot enter the repository directory.
    goto :finish
)
set "PUSHED=1"

rem Locate Python 3. The "py" launcher is preferred on Windows.
set "PYTHON="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
    echo [ERROR] Python 3 was not found.
    echo         Install Python 3.9+ from https://www.python.org/downloads/
    echo         and enable "Add python.exe to PATH" during installation.
    goto :finish
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ===========================================================================
echo  KDU local build
echo  Repository : %CD%
echo  Arguments  : %*
if "%~1"=="" echo  Platform   : default profile ^(windows-x64, Release^)
echo ===========================================================================
echo.

if "%~1"=="" (
    %PYTHON% "build\build_kdu.py"
) else (
    %PYTHON% "build\build_kdu.py" %*
)
set "RC=%ERRORLEVEL%"
echo.

if not "%RC%"=="0" (
    echo [FAILED] Build failed with exit code %RC%.
    echo          Check the messages above. Common causes:
    echo            - Visual Studio C++ workload / MSBuild is not installed
    echo            - the pinned v145 toolset is missing ^(try --toolset v143^)
    echo            - GenAsIo2Unlock is missing ^(try adding --no-postbuild^)
    goto :finish
)

echo [OK] Build succeeded.
echo      Artifacts: %CD%\dist
echo      Open the folder and use kdu.exe together with drv64.dll.

:finish
if defined PUSHED popd >nul 2>nul

rem Keep the window open only when the script was launched by double-click.
echo "%cmdcmdline%" | find /i "%~nx0" >nul
if not errorlevel 1 pause

endlocal & exit /b %RC%
