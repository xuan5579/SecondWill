@echo off
rem Double-click launcher: backend :8000 + frontend :5173, opens browser when ready.
rem ASCII-only on purpose: cmd parses .bat as GBK, so Chinese comments would garble
rem into garbage commands and print a wall of "not recognized" errors.
chcp 65001 >nul
title SecondWill - dev server
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8

rem Prefer the interpreter this project already uses; fall back to PATH python.
set "PY=C:\Users\Y7000P\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting SecondWill ...  backend :8000  frontend :5173
echo (Ctrl+C to stop, or just close this window)
echo.

"%PY%" scripts\dev.py --open
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" echo Finished with exit code %RC%
echo Press any key to close.
pause >nul
