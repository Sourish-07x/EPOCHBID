@echo off
TITLE EpochBid Master Launcher
COLOR 0A

echo ========================================================
echo         LAUNCHING EPOCHBID HACKATHON ENGINE
echo ========================================================
echo.

:: 1. Start Uvicorn Backend in a new window
echo [+] Starting FastAPI Uvicorn Server...
start cmd /k "call venv\Scripts\activate && uvicorn main:app --reload"

:: Wait 3 seconds for Uvicorn to boot up cleanly
timeout /t 3 /nobreak > nul

:: 2. Start Locust Load Testing Engine in a new window
echo [+] Starting Locust Stress Test Engine...
start cmd /k "call venv\Scripts\activate && locust -f locustfile.py"

:: Wait 2 seconds for Locust to initialize
timeout /t 2 /nobreak > nul

:: 3. Start Localtunnel Public URL Generator in a new window
echo [+] Opening Public Tunnel for Judges...
start cmd /k "npx localtunnel --port 8000"

echo.
echo ========================================================
echo   ALL SYSTEMS ONLINE! 
echo   - Local Dashboard: http://127.0.0.1:8000
echo   - Locust UI:       http://localhost:8089
echo ========================================================
pause