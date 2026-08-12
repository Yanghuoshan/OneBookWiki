@echo off
echo === OneBookWiki Server Startup ===

REM Load environment variables from parent env.bat
call "%~dp0..\env.bat"

REM Start FastAPI server with conda book environment
echo Starting FastAPI server on port 8000...
call conda activate book && uvicorn server.main:app --host 0.0.0.0 --port 8000

pause
