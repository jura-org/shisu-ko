@echo off
setlocal
REM Starts the phone portal (portal\portal.py) with the server's Python environment, which has
REM yt-dlp and PyAV. Arguments are passed through, e.g.
REM       portal.cmd                   this computer only (http://127.0.0.1:8791/)
REM       portal.cmd --mode lan        phones and tablets on the local network, once paired
REM       portal.cmd --mode tailscale  your own devices on your tailnet

if not exist "%~dp0..\portal\portal.py" (
  echo The portal folder is missing. Extract the whole zip first, then start
  echo server\portal.cmd from the extracted folder.
  pause
  exit /b 1
)
set "VENV=%USERPROFILE%\.shisu-ko\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo The Python environment is missing. Run setup.cmd first.
  pause
  exit /b 1
)
REM UTF-8 for the pairing QR code, drawn with block characters.
set "PYTHONIOENCODING=utf-8"
"%VENV%\Scripts\python.exe" "%~dp0..\portal\portal.py" %*
if errorlevel 1 pause
