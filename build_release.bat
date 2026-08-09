@echo off
REM ============================================================================
REM  ThaoResearch — one-click release build
REM  Produces:
REM    - dist\ThaoResearch.exe                  (single-file PyInstaller EXE)
REM    - installer_output\ThaoResearch-Setup-1.0.0.exe  (Inno Setup installer)
REM
REM  Usage:
REM    1. Install Inno Setup 6 once: https://jrsoftware.org/isdl.php
REM    2. Double-click build_release.bat
REM
REM  Skip the Inno step by passing /skip-installer as the first arg.
REM ============================================================================

setlocal

cd /d "%~dp0"

echo.
echo ============================================================
echo  ThaoResearch release build
echo ============================================================
echo.

REM ---- 0. Detect Python (try 'py' then 'python.exe') ----
set "PYBIN="
where py.exe >nul 2>nul
if "%ERRORLEVEL%"=="0" goto have_py
where python.exe >nul 2>nul
if "%ERRORLEVEL%"=="0" goto have_python
echo [ERROR] Python not found on PATH. Install Python 3.10+ first.
pause
exit /b 1

:have_py
set "PYBIN=py"
goto have_py_done

:have_python
set "PYBIN=python"

:have_py_done
echo Using Python launcher: %PYBIN%
%PYBIN% --version
echo.

REM ---- 0b. Detect Inno Setup Compiler ----
set "ISCC_PATH="
where ISCC.exe >nul 2>nul
if "%ERRORLEVEL%"=="0" goto have_iscc
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC_PATH=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"          set "ISCC_PATH=C:\Program Files\Inno Setup 6\ISCC.exe"
goto have_iscc_done

:have_iscc
for /f "delims=" %%I in ('where ISCC.exe') do set "ISCC_PATH=%%I"

:have_iscc_done
set "BUILD_INSTALLER=0"
if /i "%~1"=="/skip-installer" goto build_exe
if defined ISCC_PATH goto have_iscc_ok
echo [WARN] Inno Setup Compiler (ISCC.exe) not found.
echo        Skipping installer build. Install from https://jrsoftware.org/isdl.php
echo        to produce ThaoResearch-Setup-*.exe.
goto build_exe

:have_iscc_ok
set "BUILD_INSTALLER=1"
echo Inno Setup compiler: %ISCC_PATH%
echo.

:build_exe
REM ---- 1. Kill any stale ThaoResearch.exe holding the dist/ folder ----
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
%PYBIN% -m PyInstaller --noconfirm --clean ThaoResearch.spec
if "%ERRORLEVEL%" NEQ "0" (
    echo.
    echo [ERROR] PyInstaller build failed. See log above.
    pause
    exit /b 1
)

if not exist "dist\ThaoResearch.exe" (
    echo [ERROR] dist\ThaoResearch.exe not produced.
    pause
    exit /b 1
)
echo.
echo       OK - dist\ThaoResearch.exe produced.
echo.

REM ---- 4. Build the Inno Setup installer ----
if /i "%~1"=="/skip-installer" goto skip_installer
if "%BUILD_INSTALLER%"=="0" goto skip_installer

echo [4/4] Building ThaoResearch-Setup-*.exe via Inno Setup...
echo.
"%ISCC_PATH%" installer.iss
if "%ERRORLEVEL%" NEQ "0" (
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
echo  Release build complete.
echo ============================================================
echo.
echo  Artifacts:
if exist "dist\ThaoResearch.exe" (
    echo    dist\ThaoResearch.exe
)
if exist "installer_output\ThaoResearch-Setup-1.0.0.exe" (
    echo    installer_output\ThaoResearch-Setup-1.0.0.exe
)
echo.
echo  Hand the installer to your customer. After install:
echo    C:\Program Files\ThaoResearch\
echo      +- ThaoResearch.exe
echo      +- .env
echo      +- config.yaml
echo      +- locales\
echo      +- assets\
echo      +- data\      (auto-created on first run)
echo.
echo  The whole folder is portable: zip it, copy it, run it.
echo.
pause