@echo off
setlocal EnableDelayedExpansion
title holobuilder setup
cd /d "%~dp0"
rem Prepares this folder to run holobuilder on this PC (tools\local\run_local.py setup): creates
rem .venv, installs the packages from requirements.txt, then checks what else the local route
rem needs (the C compiler, the game install, the game's account file). Safe to run again.
rem HB_NO_PAUSE=1 skips the closing pause.

rem Python 3.12 or newer, 3.15 first (the search runs faster on it), then 3.13: the versions the
rem project is tested on. With 3.15, setup also makes .venv-assets with a 3.13, 3.12 or 3.14 for
rem the refresh steps that read the game's asset bundles. The py launcher knows every installed
rem Python; plain "python" is the fallback (the Microsoft Store stub fails the version test and
rem is skipped). When only an older Python is here, the message names its version, so it says
rem why that one was not used.
set "PY="
for %%V in (3.15 3.13 3.12 3.14) do if not defined PY py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
if not defined PY py -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1 && set "PY=py"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1 && set "PY=python"
if not defined PY (
  set "OLD="
  for /f "tokens=2" %%O in ('python -V 2^>nul') do set "OLD=%%O"
  if not defined OLD for /f "tokens=2" %%O in ('py -V 2^>nul') do set "OLD=%%O"
  echo.
  if defined OLD (
    echo   [X] Python !OLD! is too old: holobuilder needs Python 3.12 or newer.
  ) else (
    echo   [X] Python 3.12 or newer was not found.
  )
  echo       Install Python 3.15 and Python 3.13 from https://www.python.org/downloads/windows/
  echo       ^(tick "Add python.exe to PATH"^), open a new window and run setup.cmd again.
  echo       ^(3.15 runs holobuilder; 3.13 reads the game's pictures and models in the refresh^)
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)
%PY% -u tools\local\run_local.py setup
set "CODE=%ERRORLEVEL%"
echo.
if not defined HB_NO_PAUSE pause
exit /b %CODE%
