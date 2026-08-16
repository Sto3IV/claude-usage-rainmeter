@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
set "SCRIPT=%HERE%fetch.py"

if exist "%SystemRoot%\py.exe" (
  "%SystemRoot%\py.exe" -3 "%SCRIPT%" %*
  exit /b 0
)

"%SystemRoot%\System32\where.exe" python >nul 2>&1
if %ERRORLEVEL%==0 (
  python "%SCRIPT%" %*
  exit /b 0
)

set "MSG=Python 3 not found. Install from python.org and enable the py launcher."
echo {"ok": false, "error": "%MSG%"}
> "%HERE%snapshot.json" echo {"ok": false, "error": "%MSG%"}
exit /b 0
