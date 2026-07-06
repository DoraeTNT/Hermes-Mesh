@echo off
setlocal enabledelayedexpansion
title Hermes Launcher Build

:: ============================================================
::  HermesLauncher Build Script (ASCII only)
::  Output: dist\HermesLauncher.exe + dist\modules\
::  Hot update: replace modules\ files, exe stays unchanged
:: ============================================================

cd /d "%~dp0"

echo.
echo ========================================
echo  HermesLauncher Build
echo ========================================
echo.

:: -- 1. Check Python --
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH
    pause
    exit /b 1
)
echo [OK] Python found
python --version

:: -- 2. Check/install deps --
echo [INFO] Checking dependencies...
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing pyinstaller + Pillow + tinyaes...
    pip install pyinstaller Pillow tinyaes -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo [OK] Dependencies ready

:: -- 3. Encrypted config (pre-generated on server, no plain IP) --
if not exist "_enc_config.py" (
    echo [ERROR] _enc_config.py not found!
    echo   Copy it from the server: client/_enc_config.py
    pause
    exit /b 1
)
echo [OK] Using pre-encrypted _enc_config.py

:: -- 5. Build --
echo.
echo [STEP 1/2] Building exe + modules...
python build_client.py
if %errorlevel% neq 0 (
    echo [ERROR] Build failed
    pause
    exit /b 1
)

:: -- 6. Verify output --
echo.
echo [STEP 2/2] Verifying output...
set "EXE=..\dist\HermesLauncher.exe"
set "MOD=..\dist\modules"

if exist "%EXE%" (
    for %%F in ("%EXE%") do echo [OK] HermesLauncher.exe (%%~zF bytes)
) else (
    echo [ERROR] %EXE% not found
)

if exist "%MOD%" (
    dir /b "%MOD%" | find /c /v "" >nul
    echo [OK] modules\ directory ready
) else (
    echo [WARN] modules\ not found
)

:: -- 7. Cleanup --
echo.
echo [STEP] Cleaning temp files...
if exist "_enc_config.py" del /q "_enc_config.py"
if exist "__pycache__"   rmdir /s /q "__pycache__"
if exist "build"         rmdir /s /q "build"
if exist "*.spec"        del /q "*.spec"
echo [OK] Temp files cleaned

echo.
echo ========================================
echo  Build Complete!
echo  Output: dist\HermesLauncher.exe
echo          dist\modules\
echo ========================================
echo.
echo Deploy: copy everything under dist\ to target machine
echo Hot update: replace files under modules\, exe unchanged
echo.

pause
