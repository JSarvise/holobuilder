@echo off
setlocal
title holobuilder
cd /d "%~dp0"
rem Starts holobuilder on this PC (tools\local\run_local.py start) and opens it in the browser.
rem The server runs in this window and prints what it does; close the window or press Ctrl+C
rem to stop it.
rem   start.cmd                     http://127.0.0.1:8731/optimizer
rem   start.cmd --port 8740         another port
rem   start.cmd --no-browser        do not open the browser
rem HB_NO_PAUSE=1 skips the closing pause.

if not exist ".venv\Scripts\python.exe" (
  echo   [X] This folder is not set up yet: run setup.cmd first.
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)
".venv\Scripts\python.exe" -u tools\local\run_local.py start %*
set "CODE=%ERRORLEVEL%"
echo.
echo   holobuilder stopped ^(exit code %CODE%^).
if not defined HB_NO_PAUSE pause
exit /b %CODE%
