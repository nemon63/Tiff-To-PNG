@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
if "%VENV_DIR%"=="" set "VENV_DIR=.venv"
set "PYTHON_EXE=%ROOT%%VENV_DIR%\Scripts\python.exe"
if "%VENV_DIR:~1,1%"==":" set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo [ERROR] venv not found: "%PYTHON_EXE%"
  echo Activate or create the virtual environment first.
  exit /b 2
)

set "SCRIPT=%ROOT%tools\build_release.py"
if not exist "%SCRIPT%" (
  echo [ERROR] Script not found: "%SCRIPT%"
  exit /b 2
)

echo [INFO] Running release build from "%ROOT%"
"%PYTHON_EXE%" "%SCRIPT%" %*

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [ERROR] Release build failed with code %RC%.
  exit /b %RC%
)

echo [OK] Release build finished.
exit /b 0
