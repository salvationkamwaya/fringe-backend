@echo off
REM Fringe Business System - Owner Creation Script (Batch)
REM Usage:
REM   create_owner.bat
REM   create_owner.bat --non-interactive
REM   create_owner.bat --username owner --email owner@example.com --password "MySecret@123" --pin 1234

cd /d "%~dp0"

if exist "%~dp0fringe\Scripts\python.exe" (
    "%~dp0fringe\Scripts\python.exe" manage.py create_owner %*
) else (
    python manage.py create_owner %*
)
