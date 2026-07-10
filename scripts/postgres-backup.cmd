@echo off
REM Launcher for Task Scheduler and double-click (no PowerShell execution-policy block).
REM Keep this .cmd next to postgres-backup.ps1 (%~dp0 resolves the script folder).
setlocal
set "PS1=%~dp0postgres-backup.ps1"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%PS1%" %*
exit /b %ERRORLEVEL%
