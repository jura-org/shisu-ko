@echo off
REM Native-messaging host for the extension's "Start server" button. Firefox and Chrome run
REM this file (registered by native_host.py --register), each with arguments of its own, and
REM talk to it over stdin/stdout, so nothing here may print: the venv's Python runs the host,
REM or the system Python before setup ran.
set "PY=%USERPROFILE%\.shisu-ko\venv\Scripts\python.exe"
if exist "%PY%" goto run
REM Before setup ran: a Python 3 that really runs, found the way setup.cmd finds it (the
REM helper prints nothing; Windows answers "python" with a Store shortcut otherwise).
call "%~dp0find-python.cmd"
if not defined PY exit /b 1
:run
%PY% "%~dp0native_host.py" %*
