@echo off
chcp 65001 >nul
title SMC Trading Bot - Auto-Restart 24/7
setlocal enabledelayedexpansion

cd /d %~dp0

echo ============================================================
echo   SMC TRADING BOT - avvio automatico 24/7
echo   Dashboard: http://localhost:5000
echo ============================================================
echo.

:: ==================================================================
:: 1) TAKE-OVER: se un altro avvia_tutto.bat e' gia' aperto,
::    questo launcher lo chiude e prende il controllo. Niente piu'
::    blocchi: puoi sempre avviare tutto con doppio click.
::    (Due launcher = due bot = conflitti su MT5 e porta 5000.)
:: ==================================================================
echo [%date% %time%] Verifica di altri launcher attivi...
:: NOTA: $PID e' il processo PowerShell corrente; il suo parent (ParentProcessId)
:: e' il cmd.exe che esegue QUESTO launcher. Lo escludiamo dal kill, cosi'
:: vengono chiusi solo gli ALTRI avvia_tutto.bat (take-over).
powershell -NoProfile -Command "$self = (Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq $PID }).ParentProcessId; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'avvia_tutto\.bat' -and $_.ProcessId -ne $self } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('       Chiuso vecchio launcher: PID ' + $_.ProcessId) }" 2>nul
timeout /t 1 /nobreak >nul
echo.
:: ==================================================================
:: 2) ARRESTO VECCHIA ISTANZA - doppio meccanismo:
::    1) PID file (bot.pid) se esiste (scritto da start_bot_background.py)
::    2) Fallback: kill di OGNI python.exe che esegue run_master.py
::       (funziona anche se il PID file manca o e' stale)
:: ==================================================================
set PID_FILE=bot.pid
echo [%date% %time%] Arresto vecchia istanza...
if exist %PID_FILE% (
    set /p OLD_PID=<%PID_FILE%
    taskkill /f /pid !OLD_PID! >nul 2>&1
    if errorlevel 1 (echo       PID !OLD_PID! non attivo.) else (echo       Fatto. PID=!OLD_PID!)
    timeout /t 2 /nobreak >nul
    del %PID_FILE% 2>nul
) else (
    echo       Nessun PID file trovato.
)

echo       Ricerca e kill istanze run_master.py residue...
:: NOTA: si usa Where-Object (non -Filter WQL) perche' il wildcard "*"
:: funziona in PowerShell ma NON in WQL, dove serve "%".
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*python*' -and $_.CommandLine -match 'run_master\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('       Killato: PID ' + $_.ProcessId) }" 2>nul
timeout /t 2 /nobreak >nul
echo.

:: ==================================================================
:: 3) PRE-FLIGHT: verifica che python esista
:: ==================================================================
where python >nul 2>&1
if errorlevel 1 (
    echo   [ERRORE] Python non trovato nel PATH.
    echo            Installa Python 3.11+ e riprova.
    echo.
    pause
    exit /b 1
)
echo       Python trovato: [%date% %time%]
echo.

:: ==================================================================
:: 4) ROTAZIONE LOG (prima del primo avvio)
::    Se bot_smc.log supera 5 MB lo tronca alle ultime 3000 righe.
::    Un log da 17 MB rallentava /api/status e bloccava la landing.
:: ==================================================================
call :rotate_log
echo.

:: ==================================================================
:: 5) VERIFICA PORTA 5000 (solo avviso)
:: ==================================================================
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   [AVVISO] La porta 5000 risulta ancora occupata da un altro processo.
    echo            Se non e' il bot, chiudi quel programma prima di continuare.
    echo.
)

:: ==================================================================
:: 6) LOOP AUTO-RESTART
::    Il bot gira IN PRIMO PIANO in questa finestra.
::    Se termina con errore, riparte da solo dopo 10 secondi.
::    NOTA: premere Ctrl+C fa uscire Python con codice 1, quindi il
::    loop LO riavvia. Per fermare davvero il bot: chiudi questa
::    finestra (kill della finestra) oppure taskkill /F /IM python.exe
::    da un altro terminale, oppure usa avvia_tutto.bat chiudendolo.
:: ==================================================================
set /a RESTARTS=0
:loop
set /a RESTARTS+=1
echo.
echo [%date% %time%] === AVVIO BOT (tentativo #%RESTARTS%) ===
echo.

:: Ruota il log anche prima di ogni riavvio (il file cresce in continuazione)
if %RESTARTS% gtr 1 call :rotate_log

python run_master.py
if errorlevel 1 (
    echo.
    echo [%date% %time%] ERRORE: Bot terminato con errore. Riavvio tra 10 secondi...
    timeout /t 10 /nobreak >nul
    goto loop
)

:: Se il bot esce normalmente (es. CTRL+C), si ferma qui
echo [%date% %time%] Bot terminato normalmente.
pause
exit /b 0

:rotate_log
powershell -NoProfile -Command "$p = Join-Path (Get-Location) 'bot_smc.log'; if (Test-Path $p) { $len = (Get-Item $p).Length; if ($len -gt 5MB) { $lines = Get-Content $p -Tail 3000; [System.IO.File]::WriteAllLines((Resolve-Path $p).Path, $lines); Write-Host ('       Log troncato: era ' + [math]::Round($len/1MB,1) + ' MB') } }" 2>nul
exit /b 0
