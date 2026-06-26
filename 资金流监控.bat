@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
cd /d "%~dp0"
title ETF Flow Monitor

set "PYTHON_EXE="
if defined ETF_FLOW_PYTHON call :try_python "%ETF_FLOW_PYTHON%"
if defined MOMENTUM_PYTHON call :try_python "%MOMENTUM_PYTHON%"
if not defined PYTHON_EXE call :try_python "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PYTHON_EXE call :try_python "python"

if not defined PYTHON_EXE (
    echo(
    echo [ERROR] No usable Python found. The runtime must import pandas.
    echo(
    pause
    exit /b 1
)

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "CONFIG_PATH=%~dp0config.example.txt"
if exist "%~dp0config.local.txt" set "CONFIG_PATH=%~dp0config.local.txt"

echo(
set "TRADE_DATE="
set /p "TRADE_DATE=Trade date YYYYMMDD, blank = default: "

echo(
echo [1/2] Updating ETF data cache...
if defined TRADE_DATE (
    "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\update_flow_cache.py" --config "%CONFIG_PATH%" --trade-date "%TRADE_DATE%"
) else (
    "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\update_flow_cache.py" --config "%CONFIG_PATH%"
)
if errorlevel 1 (
    echo(
    echo [WARN] Cache helper failed. Will try local cache only.
)

echo(
echo [2/2] Building ETF flow dashboard...
if defined TRADE_DATE (
    "%PYTHON_EXE%" -X utf8 -u -m etf_flow_monitor.cli --config "%CONFIG_PATH%" --trade-date "%TRADE_DATE%" --cache-only
) else (
    "%PYTHON_EXE%" -X utf8 -u -m etf_flow_monitor.cli --config "%CONFIG_PATH%" --cache-only
)
set "EXIT_CODE=%ERRORLEVEL%"

echo(
pause
endlocal
exit /b %EXIT_CODE%

:try_python
set "CANDIDATE_PYTHON=%~1"
if not defined CANDIDATE_PYTHON exit /b 0
"%CANDIDATE_PYTHON%" -X utf8 -c "import pandas" >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=%CANDIDATE_PYTHON%"
exit /b 0
