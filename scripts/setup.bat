@echo off
echo Running the Setup in Powershell (Please wait)
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
pause