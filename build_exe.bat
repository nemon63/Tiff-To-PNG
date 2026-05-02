@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "APP_NAME=TexturePipelineWorkbench"
set "ROOT_DIR=%~dp0"
set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "BUILD_ROOT=D:\_BUILD"
set "DIST_DIR=%BUILD_ROOT%"
set "DIST_APP_DIR=%DIST_DIR%\%APP_NAME%"
set "WORK_DIR=%BUILD_ROOT%\_pyinstaller_work"
set "SPEC_DIR=%BUILD_ROOT%\_pyinstaller_spec"
set "ASSET_DIR=%BUILD_ROOT%\_assets"
set "ICON_PNG=%ROOT_DIR%\pic\ChatGPT Image 27 февр. 2026 г., 21_36_32.png"
set "ICON_ICO=%ASSET_DIR%\favicon.ico"
set "ENTRY_POINT=%ROOT_DIR%\image_converter\main.py"

if exist "%ROOT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT_DIR%\.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

echo.
echo === %APP_NAME% Windows build ===
echo Project: %ROOT_DIR%
echo Output : %DIST_APP_DIR%
echo Python : %PYTHON%
echo.

if /I "%BUILD_ROOT%"=="D:\" (
    echo Refusing to use D:\ as build root.
    exit /b 1
)

if not exist "%ENTRY_POINT%" (
    echo Entry point not found: %ENTRY_POINT%
    exit /b 1
)

if not exist "%BUILD_ROOT%" mkdir "%BUILD_ROOT%"
if not exist "%ASSET_DIR%" mkdir "%ASSET_DIR%"
if not exist "%SPEC_DIR%" mkdir "%SPEC_DIR%"

echo Installing runtime/build dependencies...
"%PYTHON%" -m pip install -r "%ROOT_DIR%\requirements.txt"
if errorlevel 1 exit /b 1

"%PYTHON%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    "%PYTHON%" -m pip install pyinstaller
    if errorlevel 1 exit /b 1
)

echo Preparing icon...
if exist "%ICON_PNG%" (
    "%PYTHON%" -c "from pathlib import Path; from PIL import Image; src=Path(r'%ICON_PNG%'); dst=Path(r'%ICON_ICO%'); dst.parent.mkdir(parents=True, exist_ok=True); im=Image.open(src).convert('RGBA'); im.save(dst, sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
    if errorlevel 1 exit /b 1
) else if exist "%ROOT_DIR%\ico\favicon.ico" (
    copy /Y "%ROOT_DIR%\ico\favicon.ico" "%ICON_ICO%" >nul
) else (
    echo Icon source not found.
    exit /b 1
)

echo Cleaning previous build output...
if exist "%DIST_APP_DIR%" rmdir /S /Q "%DIST_APP_DIR%"
if exist "%WORK_DIR%" rmdir /S /Q "%WORK_DIR%"

echo Running PyInstaller...
"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --windowed ^
    --name "%APP_NAME%" ^
    --icon "%ICON_ICO%" ^
    --distpath "%DIST_DIR%" ^
    --workpath "%WORK_DIR%" ^
    --specpath "%SPEC_DIR%" ^
    --add-data "%ICON_ICO%;ico" ^
    --collect-all PIL ^
    --hidden-import PyQt6.sip ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtGui ^
    --hidden-import PyQt6.QtWidgets ^
    --hidden-import PIL._tkinter_finder ^
    "%ENTRY_POINT%"

if errorlevel 1 exit /b 1

echo.
echo Build complete:
echo %DIST_APP_DIR%\%APP_NAME%.exe
echo.
endlocal
