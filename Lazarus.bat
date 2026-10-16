@echo off
rem Lazarus launcher for a source (development) run.
rem Python: %LAZARUS_VENV%\Scripts\pythonw.exe if LAZARUS_VENV is set, else .venv next to this file.
set "LAZ_SRC=%~dp0"
if defined LAZARUS_VENV (set "LAZ_PYW=%LAZARUS_VENV%\Scripts\pythonw.exe") else (set "LAZ_PYW=%~dp0.venv\Scripts\pythonw.exe")
cd /d "%LAZ_SRC%"
start "" "%LAZ_PYW%" "%LAZ_SRC%run_lazarus.pyw"
