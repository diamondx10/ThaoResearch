@echo off
REM ============================================================================
REM  ThaoResearch — one-click release build
REM  Produces:
REM    - dist\ThaoResearch.exe                  (single-file PyInstaller EXE)
REM    - dist\ThaoResearch-BypassLicense-1.0.0.exe (bypass-license build)
REM    - installer_output\ThaoResearch-Setup-1.0.0.exe  (Inno Setup installer)
REM
REM  Usage:
REM    build_release.bat                  (normal build + installer)
REM    build_release.bat /skip-installer  (normal build only)
REM    build_release.bat /bypass-license           (bypass build + installer)
REM    build_release.bat /bypass-license /skip-installer
REM
REM  The /bypass-license build embeds BYPASS_LICENSE=1 so the exe starts
REM  without any license key or file.
REM ============================================================================

setlocal enabledelayedexpansion

cd /d "%~dp0"

REM ---- Parse flags ----
set "SKIP_INSTALLER=0"
set "BYPASS_LICENSE=0"
:param_loop
if "%~1"=="" goto param_done
if /i "%~1"=="/skip-installer" set "SKIP_INSTALLER=1" & shift & goto param_loop
if /i "%~1"=="/bypass-license" set "BYPASS_LICENSE=1" & shift & goto param_loop
echo [WARN] Unknown flag "%~1" — ignored.
shift
goto param_loop
:param_done

if "%BYPASS_LICENSE%"=="1" (
    echo ============================================================
    echo  ThaoResearch BYPASS-LICENSE build
    echo ============================================================
) else (
    echo ============================================================
    echo  ThaoResearch release build
    echo ============================================================
)
echo.

REM ---- 0. Detect Python ----
REM Priority: py launcher > Python312 full path > python on PATH.
REM The WindowsApps stub (C:\Users\...\WindowsApps\python.exe) is not a real Python.
set "PYBIN="
where py.exe >nul 2>nul
if "!ERRORLEVEL!"=="0" goto have_py
if exist "C:\Users\nmphu\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PYBIN=C:\Users\nmphu\AppData\Local\Programs\Python\Python312\python.exe"
    goto python_done
)
where python.exe >nul 2>nul
if "!ERRORLEVEL!"=="0" goto have_python
echo [ERROR] Python not found. Install Python 3.10+ from python.org.
pause
exit /b 1

:have_py
set "PYBIN=py"
goto python_done

:have_python
set "PYBIN=python"

:python_done
echo Using Python: !PYBIN!
!PYBIN! --version
echo.

REM ---- 0b. Detect Inno Setup Compiler ----
set "ISCC_PATH="
where ISCC.exe >nul 2>nul
if "!ERRORLEVEL!"=="0" goto have_iscc
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"          set "ISCC_PATH=C:\Program Files\Inno Setup 6\ISCC.exe"
goto have_iscc_done

:have_iscc
for /f "delims=" %%I in ('where ISCC.exe') do set "ISCC_PATH=%%I"

:have_iscc_done
if "%SKIP_INSTALLER%"=="0" (
    if defined ISCC_PATH (
        echo Inno Setup compiler: !ISCC_PATH!
    ) else (
        echo [WARN] Inno Setup Compiler (ISCC.exe) not found.
        echo        Skipping installer build. Install from https://jrsoftware.org/isdl.php
        echo.
        set "SKIP_INSTALLER=1"
    )
)
echo.

REM ---- 1. Kill stale process ----
echo [1/4] Killing any stale ThaoResearch.exe process...
taskkill /F /IM ThaoResearch.exe 2>nul
echo       done.
echo.

REM ---- 2. Clean previous artifacts ----
echo [2/4] Cleaning previous build artifacts...
if exist build  rmdir /S /Q build
if exist dist   rmdir /S /Q dist
if exist installer_output rmdir /S /Q installer_output
echo       done.
echo.

REM ---- 3. Build the EXE with PyInstaller ----
echo [3/4] Building ThaoResearch.exe via PyInstaller...
echo       (this can take 1-3 minutes)
echo.

if "%BYPASS_LICENSE%"=="1" (
    echo NOTE: Building bypass-license version.
    echo       - BYPASS_LICENSE=1 is embedded in the exe.
    echo       - No license key or file required at runtime.
    echo       - Output: dist\ThaoResearch-BypassLicense-1.0.0.exe
    echo.
    !PYBIN! build_exe.py --bypass-license
) else (
    !PYBIN! -m PyInstaller --noconfirm --clean ThaoResearch.spec
)

if "!ERRORLEVEL!" NEQ "0" (
    echo.
    echo [ERROR] Build failed. See log above.
    pause
    exit /b 1
)

if "%BYPASS_LICENSE%"=="1" (
    if not exist "dist\ThaoResearch-BypassLicense-1.0.0.exe" (
        echo [ERROR] dist\ThaoResearch-BypassLicense-1.0.0.exe not produced.
        pause
        exit /b 1
    )
    echo.
    echo       OK - dist\ThaoResearch-BypassLicense-1.0.0.exe produced.
    echo.
) else (
    if not exist "dist\ThaoResearch.exe" (
        echo [ERROR] dist\ThaoResearch.exe not produced.
        pause
        exit /b 1
    )
    echo.
    echo       OK - dist\ThaoResearch.exe produced.
    echo.
)

REM ---- 4. Build the Inno Setup installer ----
if "%SKIP_INSTALLER%"=="1" goto skip_installer

echo [4/4] Building ThaoResearch-Setup-*.exe via Inno Setup...
echo.
"%ISCC_PATH%" installer.iss
if "!ERRORLEVEL!" NEQ "0" (
    echo.
    echo [ERROR] Inno Setup build failed.
    pause
    exit /b 1
)
goto done

:skip_installer
echo [4/4] Skipping Inno Setup installer.
goto done

:done
echo.
echo ============================================================
echo  Build complete.
echo ============================================================
echo.
echo  Artifacts:
if exist "dist\ThaoResearch.exe" (
    echo    dist\ThaoResearch.exe
)
if exist "dist\ThaoResearch-BypassLicense-1.0.0.exe" (
    echo    dist\ThaoResearch-BypassLicense-1.0.0.exe
)
if exist "installer_output\ThaoResearch-Setup-1.0.0.exe" (
    echo    installer_output\ThaoResearch-Setup-1.0.0.exe
)
echo.
if "%BYPASS_LICENSE%"=="1" (
    echo  BYPASS-LICENSE build: dist\ThaoResearch-BypassLicense-1.0.0.exe
    echo  - No license key or file needed.
    echo  - Just double-click and use.
    echo.
)
echo  After install:
echo    C:\Program Files\ThaoResearch\
echo      +- ThaoResearch.exe
echo      +- .env
echo      +- config.yaml
echo      +- locales^
echo      +- assets^
echo      +- data\      (auto-created on first run)
echo.
echo  The whole folder is portable: zip it, copy it, run it.
echo.
pause
