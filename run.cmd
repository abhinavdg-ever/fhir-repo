@echo off
rem Starts run.ps1 even when PowerShell's execution policy blocks scripts.
rem   run.cmd    run.cmd -Dev    run.cmd -Mock    run.cmd -Test    run.cmd -Check
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
