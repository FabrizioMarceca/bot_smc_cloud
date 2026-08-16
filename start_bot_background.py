"""
start_bot_background.py
=======================
Avvia run_master.py come processo Windows indipendente 24/7.
Usa cmd.exe start /B per distaccare il processo.

Il bot resta vivo 24/7 anche dopo la chiusura del terminale.
Per fermarlo: taskkill /F /IM python.exe
"""
import subprocess
import sys
import os
import time

base = os.path.dirname(os.path.abspath(__file__))
script_path = os.path.join(base, "run_master.py")
pid_file = os.path.join(base, "bot.pid")
log_file = os.path.join(base, "bot_smc.log")

print("=" * 50)
print("  AVVIO BOT SMC 24/7")
print("=" * 50)

# 1) Killa vecchia istanza via PID file
print("\n[1/3] Pulizia vecchie istanze...")
if os.path.exists(pid_file):
    with open(pid_file) as f:
        old_pid = f.read().strip()
    if old_pid and old_pid.isdigit():
        subprocess.run(["taskkill", "/F", "/PID", old_pid],
                       capture_output=True, timeout=5)
        print(f"  Killato PID {old_pid}")
        os.remove(pid_file)
time.sleep(1)

# 2) Avvia run_master.py via start /B cmd.exe
# start /B avvia il processo NELLA STESSA finestra ma in background
# cmd.exe /C garantisce che si stacchi dal terminale corrente
print("\n[2/3] Avvio bot...")

# Metodo: start /B python run_master.py via cmd.exe
# Questo crea un processo indipendente
avvio_cmd = f'start /B /MIN python "{script_path}"'
subprocess.run(
    ["cmd.exe", "/C", avvio_cmd],
    capture_output=True, timeout=10
)
print("  Comando start /B eseguito.")

# 3) Trova il PID del bot python con run_master
print("\n[3/3] Rilevamento PID...")
time.sleep(4)

# Cerca il processo python che esegue run_master.py
# Usa wmic per avere la CommandLine (tasklist non la mostra)
bot_pid = None
try:
    wmi = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'",
         "get", "ProcessId,CommandLine", "/format:csv"],
        capture_output=True, text=True, timeout=5
    )
    for line in wmi.stdout.strip().split("\n")[1:]:
        if not line.strip():
            continue
        if "run_master" in line and "start_bot" not in line:
            parts = line.split(",")
            pid = parts[1].strip() if len(parts) >= 2 else None
            if pid and pid.isdigit() and int(pid) != os.getpid():
                bot_pid = pid
                break
except Exception:
    # Fallback: tasklist senza command line (meno preciso)
    tl = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=5
    )
    for line in tl.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2:
            pid = parts[1]
            if pid and pid.isdigit() and int(pid) != os.getpid():
                bot_pid = pid
                break

if bot_pid:
    with open(pid_file, "w") as f:
        f.write(bot_pid)
    print(f"\n[OK] BOT IN ESECUZIONE (PID {bot_pid})")
    print(f"  PID salvato in: {pid_file}")
    print(f"  Log: {log_file}")
    print(f"  Per fermarlo: taskkill /F /PID {bot_pid}")
else:
    print(f"\n[WARN] PID non rilevato. Verifica manuale:")
    print(f"  tasklist /FI \"IMAGENAME eq python.exe\"")
    print(f"  Log: {log_file}")

print("\nBot 24/7 avviato!")
print("(usa avvia_tutto.bat per esecuzione con auto-restart loop)")
