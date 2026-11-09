@echo off
chcp 65001 >nul
title Valocheck - strumenti di ricerca
cd /d "%~dp0"
call :trova_python
if not defined PY goto nopython
:menu
cls
echo ==============================================
echo   VALOCHECK - strumenti di ricerca
echo ==============================================
echo   1  Fase 0  - verifica refunded_rr con Henrik  (gia' fatta)
echo   2  Fase 1  - triangola i cheater confermati  (Valorant aperto)
echo   3  Fase 2  - ricontrolla chi e' ancora attivo dopo 60 giorni  (Valorant aperto)
echo   4  Esporta il dataset in CSV  (per la Fase 4)
echo   5  Demo con dati simulati  (non serve Valorant)
echo   6  Test automatico dei calcoli
echo   7  Diagnosi del collegamento a Riot  (Valorant aperto)
echo   8  Fase 0b - rimborsi nei dati ufficiali Riot  (Valorant aperto)
echo   9  Sincronizza adesso con la cartella condivisa (Google Drive)
echo   0  Esci
echo.
set "scelta="
set /p scelta=Scrivi il numero e premi INVIO: 
if "%scelta%"=="1" %PY% valocheck.py fase0
if "%scelta%"=="2" %PY% valocheck.py fase1
if "%scelta%"=="3" %PY% valocheck.py fase2
if "%scelta%"=="4" %PY% valocheck.py esporta
if "%scelta%"=="5" %PY% valocheck.py demo
if "%scelta%"=="6" (%PY% valocheck.py test & pause)
if "%scelta%"=="7" %PY% valocheck.py diagnosi
if "%scelta%"=="8" %PY% valocheck.py fase0b
if "%scelta%"=="9" %PY% valocheck.py sincronizza
if "%scelta%"=="0" exit /b
goto menu

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
