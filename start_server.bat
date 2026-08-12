@echo off
:: OneBookWiki server startup script (Windows CMD)
cd /d "%~dp0"

echo === OneBookWiki Server Startup ===

:: Check for .env and note (CMD can't source it, user should use start.ps1 or set vars manually)
if exist .env (
    echo [onebookwiki] .env file found. For automatic loading, use start.ps1 instead.
    echo [onebookwiki] Ensure ONEBOOKWIKI_* environment variables are set before running.
)

:: Detect Python (try python3 first, then python)
set PYTHON_CMD=python
where python3 >nul 2>&1
if %ERRORLEVEL% EQU 0 set PYTHON_CMD=python3

%PYTHON_CMD% --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Error: Python not found. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

echo Python found.
echo Starting FastAPI server on port 8000...

%PYTHON_CMD% -m uvicorn server.main:app --host 0.0.0.0 --port 8000

pause
