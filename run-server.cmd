@echo off
setlocal
cd /d "%~dp0"
set "NSO_HOST=0.0.0.0"
set "NSO_PORT=8765"
echo Starting NSO room server on TCP+UDP %NSO_HOST%:%NSO_PORT%
echo Detailed JSON event logs will appear below. Press Ctrl+C to stop.
python -u server.py --host %NSO_HOST% --port %NSO_PORT% --state-file online-server-state.json %*
set "EXIT_CODE=%ERRORLEVEL%"
echo NSO room server exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
