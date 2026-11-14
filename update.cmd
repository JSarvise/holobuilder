@echo off
setlocal EnableDelayedExpansion
title holobuilder update
cd /d "%~dp0"
rem Brings this folder to the newest holobuilder on GitHub, then refreshes
rem (tools\local\run_local.py update).
rem   update.cmd                    git pull (safe for the cards a refresh added), setup's
rem                                 packages when they changed, then refresh.cmd
rem   update.cmd --no-refresh       the code only; refresh later
rem HB_NO_PAUSE=1 skips the closing pause.

if not exist ".venv\Scripts\python.exe" (
  echo   [X] This folder is not set up yet: run setup.cmd first.
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)
rem One block to the end: cmd reads a block whole before it runs it, so a pull that changes this
rem file cannot shift the lines still to run.
(
  ".venv\Scripts\python.exe" -u tools\local\run_local.py update %*
  set "CODE=!ERRORLEVEL!"
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b !CODE!
)
