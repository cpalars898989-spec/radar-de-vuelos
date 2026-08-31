@echo off
setlocal
cd /d "%~dp0"

where python.exe >nul 2>nul
if %errorlevel%==0 (
  python.exe -B app.py --open --with-bot
  goto :end
)

where py.exe >nul 2>nul
if %errorlevel%==0 (
  py.exe -3 -B app.py --open --with-bot
  goto :end
)

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PYTHON%" (
  "%CODEX_PYTHON%" -B app.py --open --with-bot
  goto :end
)

echo No se encontro Python.
echo Instala Python 3.11 o posterior y marca Add Python to PATH.

:end
echo.
echo El radar se detuvo. Presiona una tecla para cerrar esta ventana.
pause >nul

