@echo off
setlocal
title holobuilder setup
cd /d "%~dp0"
rem Prepares this folder to run holobuilder on this PC: creates .venv, installs the packages
rem from requirements.txt, then checks what else the local route needs (the C compiler, the
rem game install, the game's account file). Safe to run again at any time.
rem HB_NO_PAUSE=1 skips the closing pause.

echo.
echo   holobuilder setup
echo   =================
echo.

rem Python 3.11 or newer, 3.13 first: the version the project is tested on. The py launcher
rem knows every installed Python; plain "python" is the fallback (the Microsoft Store stub
rem fails the version test and is skipped).
set "PY="
for %%V in (3.13 3.12 3.11 3.14) do if not defined PY py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo   [X] Python 3.11 or newer was not found.
  echo       Install Python 3.13 from https://www.python.org/downloads/windows/
  echo       ^(tick "Add python.exe to PATH"^), open a new window and run setup.cmd again.
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% -c "import platform; print(platform.python_version())"') do set "PYVER=%%v"
echo   Python %PYVER%  ^(%PY%^)

if exist ".venv\Scripts\python.exe" (
  echo   .venv is already here: bringing its packages up to date
) else (
  echo   creating .venv ...
  %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo   [X] .venv could not be created ^(see the message above^).
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)

echo.
echo   installing the packages ^(a few minutes the first time^) ...
echo.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e . -r requirements.txt
if errorlevel 1 (
  echo.
  echo   [X] The packages did not install ^(see pip's message above^).
  echo.
  if not defined HB_NO_PAUSE pause
  exit /b 1
)

echo.
".venv\Scripts\python.exe" tools\local\run_local.py check
set "CODE=%ERRORLEVEL%"
echo.
if not defined HB_NO_PAUSE pause
exit /b %CODE%
