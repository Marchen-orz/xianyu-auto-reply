@echo off
setlocal
chcp 65001 >nul

set "ROOT=%~dp0"
set "PYTHONUTF8=1"
set "NEED_SETUP=0"

echo [xianyu] checking local dependencies...
if not exist "%ROOT%backend-web\.venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "%ROOT%websocket\.venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "%ROOT%scheduler\.venv\Scripts\python.exe" set "NEED_SETUP=1"
if not exist "%ROOT%frontend\node_modules" set "NEED_SETUP=1"

if "%NEED_SETUP%"=="1" (
  echo [xianyu] missing dependencies, running setup...
  call :setup_all
  if errorlevel 1 (
    echo [xianyu] dependency setup failed.
    pause
    exit /b 1
  )
)

call :start_if_not_running "backend-web" "8089" "xianyu-backend-web-8089" "%ROOT%backend-web" "set PYTHONUTF8=1&& echo [backend-web] cwd: && cd && echo [backend-web] run: .venv\Scripts\python.exe main.py && .venv\Scripts\python.exe main.py" "3"
call :start_if_not_running "websocket" "8090" "xianyu-websocket-8090" "%ROOT%websocket" "set PYTHONUTF8=1&& echo [websocket] cwd: && cd && echo [websocket] run: .venv\Scripts\python.exe main.py && .venv\Scripts\python.exe main.py" "3"
call :start_if_not_running "scheduler" "8091" "xianyu-scheduler-8091" "%ROOT%scheduler" "set PYTHONUTF8=1&& echo [scheduler] cwd: && cd && echo [scheduler] run: .venv\Scripts\python.exe main.py && .venv\Scripts\python.exe main.py" "2"
call :start_if_not_running "frontend" "9000" "xianyu-frontend-9000" "%ROOT%frontend" "npm run dev" "0"

echo.
echo [xianyu] startup check finished.
echo [xianyu] open: http://localhost:9000
echo [xianyu] default login: admin / admin123
echo.
pause
exit /b 0

:is_port_listening
set "PORT=%~1"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  if not "%%P"=="0" exit /b 0
)
exit /b 1

:is_window_running
set "WINDOW_TITLE=%~1"
tasklist /v /fi "imagename eq cmd.exe" | findstr /I /C:"%WINDOW_TITLE%" >nul
if not errorlevel 1 exit /b 0
exit /b 1

:start_if_not_running
set "SERVICE_NAME=%~1"
set "SERVICE_PORT=%~2"
set "WINDOW_TITLE=%~3"
set "SERVICE_DIR=%~4"
set "SERVICE_CMD=%~5"
set "WAIT_SECONDS=%~6"

call :is_window_running "%WINDOW_TITLE%"
if not errorlevel 1 (
  echo [xianyu] %SERVICE_NAME% startup window already exists, skip start
  exit /b 0
)

call :is_port_listening "%SERVICE_PORT%"
if not errorlevel 1 (
  echo [xianyu] %SERVICE_NAME% is already running on http://localhost:%SERVICE_PORT%, skip start
  exit /b 0
)

echo [xianyu] starting %SERVICE_NAME%: http://localhost:%SERVICE_PORT%
start "%WINDOW_TITLE%" /D "%SERVICE_DIR%" cmd /d /k "%SERVICE_CMD%"
if not "%WAIT_SECONDS%"=="0" timeout /t %WAIT_SECONDS% /nobreak >nul
exit /b 0

:setup_all
echo [init] checking uv...
where uv >nul 2>nul
if errorlevel 1 (
  echo [error] uv was not found. Install uv and add it to PATH.
  exit /b 1
)

echo [init] backend-web dependencies...
call :setup_python_service "backend-web"
if errorlevel 1 exit /b 1

echo [init] websocket dependencies...
call :setup_python_service "websocket"
if errorlevel 1 exit /b 1

echo [init] scheduler dependencies...
call :setup_python_service "scheduler"
if errorlevel 1 exit /b 1

echo [init] frontend dependencies...
pushd "%ROOT%frontend"
if errorlevel 1 exit /b 1
if not exist node_modules (
  call npm install
  if errorlevel 1 (
    popd
    echo [error] npm install failed.
    exit /b 1
  )
) else (
  echo [init] frontend node_modules exists, skip npm install.
)
popd

echo [init] done.
exit /b 0

:setup_python_service
set "SERVICE_DIR=%~1"
pushd "%ROOT%%SERVICE_DIR%"
if errorlevel 1 exit /b 1

if not exist .venv\Scripts\python.exe (
  echo [init] creating venv: %SERVICE_DIR%\.venv
  uv venv .venv --python 3.11
  if errorlevel 1 (
    popd
    echo [error] failed to create venv: %SERVICE_DIR%
    exit /b 1
  )
)

uv pip install --python .venv\Scripts\python.exe -e .
if errorlevel 1 (
  popd
  echo [error] uv dependency install failed: %SERVICE_DIR%
  exit /b 1
)

.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
  popd
  echo [error] playwright chromium install failed: %SERVICE_DIR%
  exit /b 1
)

.venv\Scripts\python.exe -m patchright install chrome
if errorlevel 1 (
  popd
  echo [error] patchright chrome install failed: %SERVICE_DIR%
  exit /b 1
)

popd
exit /b 0
