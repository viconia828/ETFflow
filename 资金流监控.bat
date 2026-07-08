@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "FTP_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "ftp_proxy="
set "NO_PROXY=localhost,127.0.0.1,::1,api.tushare.pro,tushare.pro,github.com,ssh.github.com"
set "no_proxy=%NO_PROXY%"
set "DATA_SOURCE_IGNORE_PROXY=1"
set "TUSHARE_IGNORE_PROXY=1"
set "GIT_SSH_COMMAND=ssh -o ProxyCommand=none -o ProxyJump=none"
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
set "CONFIG_PATH=%~dp0config.txt"
if exist "%~dp0config.local.txt" set "CONFIG_PATH=%~dp0config.local.txt"

echo(
echo Select run mode:
echo   1 = Build one daily report (default)
echo   2 = Build every trading-day report in a date range
set "RUN_MODE="
set /p "RUN_MODE=Mode 1/2, blank = 1: "
if not defined RUN_MODE set "RUN_MODE=1"

if "%RUN_MODE%"=="2" goto range_report
if not "%RUN_MODE%"=="1" (
    echo(
    echo [ERROR] Unknown mode: %RUN_MODE%
    echo(
    pause
    endlocal
    exit /b 1
)

:single_report
echo(
set "TRADE_DATE="
set /p "TRADE_DATE=Trade date YYYYMMDD, blank = default: "
echo(
echo [1/3] Updating ETF data cache...
if defined TRADE_DATE (
    "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\update_flow_cache.py" --config "%CONFIG_PATH%" --trade-date "%TRADE_DATE%"
) else (
    "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\update_flow_cache.py" --config "%CONFIG_PATH%"
)
set "CACHE_EXIT_CODE=%ERRORLEVEL%"
if "%CACHE_EXIT_CODE%"=="2" (
    echo(
    echo [ERROR] 当天单市场数据为空，ETF 数据没有完整拉取成功，无法生成当天报表。
    echo(
    set "EXIT_CODE=%CACHE_EXIT_CODE%"
    goto finish
)
if not "%CACHE_EXIT_CODE%"=="0" (
    echo(
    echo [WARN] Cache helper failed. Will try local cache only.
)

echo(
echo [2/3] Building ETF flow dashboard...
if defined TRADE_DATE (
    "%PYTHON_EXE%" -X utf8 -u -m etf_flow_monitor.cli --config "%CONFIG_PATH%" --trade-date "%TRADE_DATE%" --cache-only
) else (
    "%PYTHON_EXE%" -X utf8 -u -m etf_flow_monitor.cli --config "%CONFIG_PATH%" --cache-only
)
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo(
    echo [ERROR] Dashboard build failed. Skip GitHub Pages publish.
    echo(
    pause
    endlocal
    exit /b %EXIT_CODE%
)

echo(
echo [3/3] Publishing GitHub Pages...
if defined TRADE_DATE (
    "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\publish_pages.py" --config "%CONFIG_PATH%" --trade-date "%TRADE_DATE%"
) else (
    "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\publish_pages.py" --config "%CONFIG_PATH%"
)
if errorlevel 1 (
    echo(
    echo [WARN] Pages publish failed. Local dashboard was generated; check network, GitHub SSH, or temp publish directory permissions.
) else (
    echo(
    echo [OK] Pages publish finished.
)
set "EXIT_CODE=0"
goto finish

:range_report
echo(
set "RANGE_START="
set "RANGE_END="
set /p "RANGE_START=Range start YYYYMMDD: "
set /p "RANGE_END=Range end YYYYMMDD: "
if not defined RANGE_START (
    echo(
    echo [ERROR] Range start is required for mode 2.
    echo(
    pause
    endlocal
    exit /b 1
)
if not defined RANGE_END (
    echo(
    echo [ERROR] Range end is required for mode 2.
    echo(
    pause
    endlocal
    exit /b 1
)

echo(
echo [1/3] Updating ETF data cache to range end...
"%PYTHON_EXE%" -X utf8 -u "%~dp0tools\update_flow_cache.py" --config "%CONFIG_PATH%" --trade-date "%RANGE_END%"
set "CACHE_EXIT_CODE=%ERRORLEVEL%"
if "%CACHE_EXIT_CODE%"=="2" (
    echo(
    echo [ERROR] 区间结束日单市场数据为空，ETF 数据没有完整拉取成功，无法生成本次区间报表。
    echo(
    set "EXIT_CODE=%CACHE_EXIT_CODE%"
    goto finish
)
if not "%CACHE_EXIT_CODE%"=="0" (
    echo(
    echo [WARN] Cache helper failed. Will try local cache only.
)

echo(
echo [2/3] Building ETF flow dashboards for trading days in range...
"%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_flow_reports_range.py" --config "%CONFIG_PATH%" --start-date "%RANGE_START%" --end-date "%RANGE_END%" --cache-only --detail-output today --heartbeat-seconds 10
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo(
    echo [ERROR] Range dashboard build failed. Skip GitHub Pages publish.
    echo(
    pause
    endlocal
    exit /b %EXIT_CODE%
)

echo(
echo [3/3] Publishing GitHub Pages for generated range...
"%PYTHON_EXE%" -X utf8 -u "%~dp0tools\publish_pages.py" --config "%CONFIG_PATH%" --range-start "%RANGE_START%" --range-end "%RANGE_END%"
if errorlevel 1 (
    echo(
    echo [WARN] Pages publish failed. Local dashboards were generated; check network, GitHub SSH, or temp publish directory permissions.
) else (
    echo(
    echo [OK] Pages publish finished.
)
set "EXIT_CODE=0"

:finish
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
