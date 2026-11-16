@echo off
REM Builds (first time) and starts the Shisu-ko server container in the background.
REM Uses Docker Desktop if "docker" is on the PATH, otherwise Docker Engine inside WSL (Ubuntu).
REM Extra arguments are passed to "docker compose up", e.g.  up.cmd --build
setlocal
cd /d "%~dp0.."
where docker >nul 2>nul
if not errorlevel 1 (
  docker compose up -d %*
  if errorlevel 1 goto failed
) else (
  wsl -d Ubuntu -- docker compose up -d %*
  if errorlevel 1 goto failed
  REM WSL stops the distro (and with it Docker) a few seconds after the last WSL session ends.
  REM A minimized keep-alive session holds it open; close that window or run down.cmd to release it.
  tasklist /fi "WINDOWTITLE eq Shisu-ko WSL keep-alive" 2>nul | find /i "wsl.exe" >nul
  if errorlevel 1 start "Shisu-ko WSL keep-alive" /min wsl -d Ubuntu --exec sleep infinity
)
echo.
echo The Shisu-ko server container is starting. The first start downloads the model unless DATA_DIR already has it.
echo Follow progress with docker\logs.cmd ; stop with docker\down.cmd
pause
exit /b 0

:failed
echo.
echo Starting the container failed. If Docker is not installed yet, see docs\install.md, section "Docker".
pause
exit /b 1
