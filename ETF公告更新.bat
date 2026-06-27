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
set "NO_PROXY=localhost,127.0.0.1,::1,api.tushare.pro,tushare.pro,www.cninfo.com.cn,static.cninfo.com.cn,query.sse.com.cn,www.sse.com.cn,www.szse.cn,disc.static.szse.cn"
set "no_proxy=%NO_PROXY%"
set "DATA_SOURCE_IGNORE_PROXY=1"
set "TUSHARE_IGNORE_PROXY=1"
chcp 65001 >nul
cd /d "%~dp0"
title ETF Announcement Update

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
echo [1/4] Reviewing previous manual confirmations...
"%PYTHON_EXE%" -X utf8 -u "%~dp0tools\review_lifecycle_pending_confirmations.py" --config "%CONFIG_PATH%"
if errorlevel 1 (
    echo(
    echo [ERROR] Manual confirmation review failed.
    echo(
    pause
    endlocal
    exit /b 1
)

echo(
set "START_DATE="
set /p "START_DATE=Announcement start date YYYYMMDD, blank = config default: "
set "END_DATE="
set /p "END_DATE=Announcement end date YYYYMMDD, blank = today: "

echo(
echo [2/4] Building lifecycle request plan...
if defined START_DATE (
    if defined END_DATE (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --start-date "%START_DATE%" --end-date "%END_DATE%" --skip-if-current
    ) else (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --start-date "%START_DATE%" --skip-if-current
    )
) else (
    if defined END_DATE (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --end-date "%END_DATE%" --skip-if-current
    ) else (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --skip-if-current
    )
)
if errorlevel 1 (
    echo(
    echo [ERROR] Lifecycle request plan failed.
    echo(
    pause
    endlocal
    exit /b 1
)

echo(
echo [3/4] Updating ETF announcements...
"%PYTHON_EXE%" -X utf8 -u "%~dp0tools\update_etf_announcements.py" --config "%CONFIG_PATH%" --heartbeat-seconds 10
if errorlevel 1 (
    echo(
    echo [WARN] Announcement update failed. Will still build lifecycle audit from local CSV.
)

echo(
echo [4/4] Rebuilding lifecycle audit...
if defined START_DATE (
    if defined END_DATE (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --start-date "%START_DATE%" --end-date "%END_DATE%" --skip-if-current
    ) else (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --start-date "%START_DATE%" --skip-if-current
    )
) else (
    if defined END_DATE (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --end-date "%END_DATE%" --skip-if-current
    ) else (
        "%PYTHON_EXE%" -X utf8 -u "%~dp0tools\build_etf_lifecycle_table.py" --config "%CONFIG_PATH%" --skip-if-current
    )
)
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo(
    echo [ERROR] Lifecycle audit failed.
    echo(
    pause
    endlocal
    exit /b %EXIT_CODE%
)

echo(
echo [summary] Lifecycle run summary...
"%PYTHON_EXE%" -X utf8 -u "%~dp0tools\print_lifecycle_run_summary.py" --config "%CONFIG_PATH%"
if errorlevel 1 (
    echo [WARN] Lifecycle run summary failed.
)

echo(
echo [OK] Announcement update and lifecycle audit finished.
echo(
pause
endlocal
exit /b 0

:try_python
set "CANDIDATE_PYTHON=%~1"
if not defined CANDIDATE_PYTHON exit /b 0
"%CANDIDATE_PYTHON%" -X utf8 -c "import pandas" >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=%CANDIDATE_PYTHON%"
exit /b 0
