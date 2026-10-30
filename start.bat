@echo off
rem Desktop-style launcher: starts the services, then opens a standalone app window
rem (Chrome --app, falling back to Edge --app, then to the default browser).
rem
rem ASCII-only on purpose: cmd parses .bat as GBK, Chinese comments garble into
rem garbage commands and print a wall of "not recognized" errors.
rem
rem Everything actually lives in scripts\dev.py -- this file is only an entry point,
rem so there is exactly ONE copy of the startup logic to maintain.

chcp 65001 >nul
title SecondWill - app window
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8

rem Prefer the interpreter this project already uses; fall back to PATH python.
set "PY=C:\Users\Y7000P\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting SecondWill ...  app window :5173  backend :8000
echo (Ctrl+C or close this window to stop the servers)
echo.

"%PY%" scripts\dev.py --app
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" echo Finished with exit code %RC%
echo Press any key to close.
pause >nul
