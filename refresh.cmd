@echo off
setlocal
title holobuilder refresh
cd /d "%~dp0"
rem Reads the game and your account into this folder (tools\local\run_local.py refresh).
rem   refresh.cmd                   the account's region is found by itself the first time
rem   refresh.cmd --region as       us, as or jp: the server your account lives on
rem   refresh.cmd --skip chibi      any other option goes to tools\recon\proxy\refresh_all.py
rem HB_NO_PAUSE=1 skips the closing pause.

if not exist ".venv\Scripts\python.exe" (
  echo   [X] This folder is not set up yet: run setup.cmd first.
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)
".venv\Scripts\python.exe" -u tools\local\run_local.py refresh %*
set "CODE=%ERRORLEVEL%"
echo.
if not defined HB_NO_PAUSE pause
exit /b %CODE%
