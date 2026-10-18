@echo off
rem ============================================================
rem  YSM Pet launcher
rem  Finds a Python that actually has the pet dependencies
rem  (numpy scipy PySide6 PIL yaml pybullet) and starts
rem  scripts\run_pet.py with it.  See scripts\launch_pet.ps1
rem
rem    run_pet.bat                  start the pet (no console window)
rem    run_pet.bat --list           list candidate Pythons + what they miss
rem    run_pet.bat --console        start with a console (to see errors)
rem    run_pet.bat --redetect       ignore the remembered interpreter
rem    run_pet.bat --python <exe>   force an interpreter
rem    run_pet.bat --smoke ...      extra args go to scripts\run_pet.py
rem ============================================================
setlocal
set "ROOT=%~dp0"
set "PS=pwsh"
where pwsh >nul 2>nul
if errorlevel 1 set "PS=powershell"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\launch_pet.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [launcher exit %RC%]
  pause
)
exit /b %RC%
