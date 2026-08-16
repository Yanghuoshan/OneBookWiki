@echo off
:: OneBookWiki server startup script (Windows CMD)
cd /d "%~dp0"

echo === OneBookWiki Server Startup ===

:: Check for .env and note (CMD can't source it, user should use start.ps1 or set vars manually)
if exist .env (
    echo [onebookwiki] .env file found. For automatic loading, use start.ps1 instead.
    echo [onebookwiki] Ensure ONEBOOKWIKI_* environment variables are set before running.
)

:: Detect Python (try python first, then python3)
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON_CMD=python
) else (
    python3 --version >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set PYTHON_CMD=python3
    )
)

if "%PYTHON_CMD%"=="" (
    echo Error: Python not found. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

echo Python found.

if /I "%~1"=="--chat-worker" (
    echo Starting OneBookWiki durable chat worker ...
    %PYTHON_CMD% -m server.chat_worker
    exit /b %ERRORLEVEL%
)

if "%ONEBOOKWIKI_ENV%"=="production" (
    echo Starting OneBookWiki server [PRODUCTION] on http://0.0.0.0:8000 ...
    echo Start the chat worker separately: start_server.bat --chat-worker
    %PYTHON_CMD% -m uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips="*"
) else (
    echo Starting OneBookWiki server [DEVELOPMENT] on http://0.0.0.0:8000 ...
    echo Frontend: cd frontend ^&^& npm run dev -- --host 127.0.0.1
    echo Chat worker: start_server.bat --chat-worker
    %PYTHON_CMD% -m uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
)

pause
