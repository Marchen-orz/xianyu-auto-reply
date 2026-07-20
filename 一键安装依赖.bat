@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHONUTF8=1"

echo [init] checking uv...
where uv >nul 2>nul
if errorlevel 1 (
  echo [error] uv was not found. Install uv and add it to PATH.
  pause
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
    pause
    exit /b 1
  )
) else (
  echo [init] frontend node_modules exists, skip npm install.
)
popd

echo [init] done.
pause
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
    pause
    exit /b 1
  )
)

uv pip install --python .venv\Scripts\python.exe -e .
if errorlevel 1 (
  popd
  echo [error] uv dependency install failed: %SERVICE_DIR%
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 (
  popd
  echo [error] playwright chromium install failed: %SERVICE_DIR%
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m patchright install chrome
if errorlevel 1 (
  popd
  echo [error] patchright chrome install failed: %SERVICE_DIR%
  pause
  exit /b 1
)

popd
exit /b 0
