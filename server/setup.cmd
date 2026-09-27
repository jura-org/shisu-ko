@echo off
setlocal
REM One-time setup: creates an isolated Python environment under %USERPROFILE%\.shisu-ko
REM and installs faster-whisper, yt-dlp and the CUDA runtime libraries.

set "ROOT=%USERPROFILE%\.shisu-ko"
set "VENV=%ROOT%\venv"

REM Explorer shows a zip as a folder and, on a double-click, extracts only the clicked file
REM into a temporary place: this script then runs alone, and every sibling is missing.
if not exist "%~dp0server.py" (
  echo This file is running on its own, from inside a zip or a folder without the rest
  echo of Shisu-ko. Extract the whole zip first ^(right-click it, "Extract All..."^), then
  echo start server\setup.cmd from the extracted folder.
  pause
  exit /b 1
)

REM find-python.cmd sets PY to a Python 3.10+ that really runs (Windows answers "python" with a
REM Microsoft Store shortcut when none is on the PATH, and `where` cannot tell the two apart).
call "%~dp0find-python.cmd"
if not defined PY (
  echo Python 3.10 or newer is required, and none that runs was found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
  echo then run this script again. If Python is installed and this message still appears,
  echo Windows is answering "python" with its Store shortcut: turn python.exe off under
  echo Settings ^> Apps ^> Advanced app settings ^> App execution aliases, or repair the
  echo installation with "Add python.exe to PATH" ticked.
  pause
  exit /b 1
)

if not exist "%ROOT%\cache" mkdir "%ROOT%\cache"
if not exist "%ROOT%\models" mkdir "%ROOT%\models"

if not exist "%VENV%\Scripts\python.exe" (
  echo Creating virtual environment in %VENV% ...
  %PY% -m venv "%VENV%"
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

echo Installing Python packages (this downloads about 1.5 GB the first time) ...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo Package installation failed.
  pause
  exit /b 1
)
echo Installing NVIDIA CUDA libraries for GPU inference (harmless on CPU-only machines) ...
"%VENV%\Scripts\python.exe" -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

REM Register the native-messaging host behind the extension's "Start server" button before
REM the environment check, which reports whether it is registered.
"%VENV%\Scripts\python.exe" "%~dp0native_host.py" --register --verbose
echo.
echo Environment check:
"%VENV%\Scripts\python.exe" "%~dp0server.py" --check

REM The model the server starts with, downloaded now so the first start is not the wait; the
REM popup can switch to another one later. The check above said whether there is a CUDA
REM device. choice waits for one of the two keys, so a wrong key is impossible, and its
REM errorlevel is the number of the key, or 255 when it cannot read one (stdin closed or
REM empty: an unattended run). "if errorlevel N" means N or more, so the one line below tests
REM 255 first and takes large-v3, as setup.sh does at an EOF, then 2; it stays one line, right
REM after choice, since a set inside an if-block resets errorlevel to 0 for any test after it.
echo.
echo Which Whisper model should the server use? (the popup can switch later)
echo   1  large-v3  best quality, about 3 GB, wants a GPU with 4 GB or more free
echo   2  small     about 500 MB, fine on a CPU, less accurate
choice /c 12 /n /m "Type 1 or 2: "
if errorlevel 3 (set "MODEL=large-v3") else if errorlevel 2 (set "MODEL=small") else (set "MODEL=large-v3")
REM YouTube refuses some downloads ("Sign in to confirm you're not a bot") until they carry a
REM signed-in browser's cookies. server.py asks, only where Firefox keeps a profile, and reads
REM nothing before a yes; the answer goes to config.json, the default of every start, the
REM popup's Start button included. No key at all (stdin closed) leaves config.json as it is.
echo.
"%VENV%\Scripts\python.exe" "%~dp0server.py" --setup-cookies
echo.
"%VENV%\Scripts\python.exe" "%~dp0server.py" --download-model %MODEL%
if errorlevel 1 (
  echo The model could not be downloaded. Check the connection and run setup.cmd again,
  echo or start run.cmd: the server then downloads %MODEL% itself, without a progress bar.
  pause
  exit /b 1
)
echo.
echo Setup is complete: the %MODEL% model is downloaded and everything is ready.
echo Close this window and start run.cmd.
pause
