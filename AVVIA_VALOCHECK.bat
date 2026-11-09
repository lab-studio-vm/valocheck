@echo off
chcp 65001 >nul
title Valocheck
cd /d "%~dp0"
call :trova_python
if not defined PY goto nopython
%PY% valocheck.py
pause
exit /b

:trova_python
set "PY="
py -3 -c "import sys" >nul 2>nul && (set "PY=py -3" & exit /b)
python -c "import sys" >nul 2>nul && (set "PY=python" & exit /b)
exit /b

:nopython
echo.
echo Python non e' installato su questo PC.
echo Provo a installarlo adesso (serve qualche minuto, potrebbe chiederti un permesso: rispondi Si).
echo.
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Se l'installazione e' andata a buon fine: CHIUDI questa finestra e rifai doppio clic.
echo Se non ha funzionato: scarica Python da https://www.python.org/downloads/
echo e durante l'installazione spunta la casella "Add python.exe to PATH".
pause
exit /b
