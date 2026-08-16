#!/bin/bash
# ==========================================================================
# deploy.sh - Deploy SMC Trading Bot su Oracle Cloud (Linux Ubuntu)
# ==========================================================================
# Eseguito dal workflow GitHub dopo la creazione dell'istanza, oppure a mano
# via SSH. Se le variabili d'ambiente MT5_LOGIN / MT5_PASSWORD / ... sono
# presenti (esportate dal workflow), il .env viene creato con i valori REALI.
# Senza variabili restano i segnaposto da modificare con: nano .env
#
# ARCHITETTURA (rilevata automaticamente):
#   - aarch64/arm64 (OCI Ampere A1.Flex): MT5 via HANGOVER
#       (Wine fork per ARM64, https://github.com/AndreRH/hangover).
#       Il bot gira DENTRO Wine con Python Windows 3.11 (MT5_BACKEND=local).
#       NB: l'immagine Docker gmag11/metatrader5-docker e' SOLO amd64 e
#       NON funziona su ARM64: ecco perche' su ARM non usiamo Docker.
#   - x86_64: MT5 via Docker (gmag11/metatrader5-docker) + mt5linux bridge.
#
# Requisiti: Oracle Cloud Free Tier VM (2 ARM CPU+, 12GB+ RAM, Ubuntu 22.04+)
# ==========================================================================

set -e

echo "============================================"
echo " SMC Trading Bot - Deploy su Oracle Cloud"
echo "============================================"
echo ""

ARCH=$(uname -m)
echo "Architettura rilevata: $ARCH"
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    IS_ARM=1
else
    IS_ARM=0
fi

# ==========================================================================
# 1. Aggiorna sistema e installa dipendenze di base
# ==========================================================================
echo "[1/7] Aggiornamento sistema e installazione dipendenze..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip git curl wget ca-certificates

if [ "$IS_ARM" = "1" ]; then
    # Hangover richiede un display virtuale per installare MT5
    sudo apt-get install -y xvfb winbind cabextract unzip
else
    sudo apt-get install -y docker.io docker-compose ufw
    sudo systemctl enable docker
    sudo systemctl start docker
    sudo usermod -aG docker "$USER"
fi
echo "Dipendenze installate."

# ==========================================================================
# 2. Configura firewall (apri solo webhook e SSH)
# ==========================================================================
echo "[2/7] Configurazione firewall..."
sudo ufw allow 22/tcp
sudo ufw allow 5000/tcp
sudo ufw --force enable || echo "  (ufw non disponibile o gia' attivo)"
echo "Firewall configurato."

# ==========================================================================
# 3. Installa MetaTrader 5 (Hangover su ARM / Docker su x86)
# ==========================================================================
echo "[3/7] Installazione MetaTrader 5..."

if [ "$IS_ARM" = "1" ]; then
    # ------------------------------------------------------------------
    # Percorso ARM64: HANGOVER (Wine x86-64 su ARM64)
    # ------------------------------------------------------------------
    echo "  -> Hangover (Wine per ARM64) + MetaTrader 5 + Python Windows"

    # 3a. Scarica l'ultimo pacchetto Hangover per Ubuntu 22.04 (jammy) ARM64
    HANGOVER_DEB_URL=""
    for i in 1 2 3; do
        HANGOVER_DEB_URL=$(curl -sL --max-time 30 \
            "https://api.github.com/repos/AndreRH/hangover/releases/latest" \
            | python3 -c "
import sys, json
try:
    rel = json.load(sys.stdin)
except Exception:
    sys.exit(0)
assets = rel.get('assets', [])
# Preferisci jammy (Ubuntu 22.04) arm64; fallback: qualsiasi arm64 .deb
cands = [a['browser_download_url'] for a in assets if a['name'].endswith('.deb')]
jammy = [u for u in cands if 'jammy' in u and 'arm64' in u]
anyarm = [u for u in cands if 'arm64' in u]
print((jammy or anyarm or [''])[0])
")
        [ -n "$HANGOVER_DEB_URL" ] && break
        echo "  Retry download Hangover ($i/3)..."
        sleep 10
    done

    if [ -z "$HANGOVER_DEB_URL" ]; then
        echo "  ERRORE: impossibile trovare il pacchetto Hangover per ARM64."
        echo "  Controlla manualmente: https://github.com/AndreRH/hangover/releases"
        exit 1
    fi

    echo "  Scarico Hangover: $HANGOVER_DEB_URL"
    wget -q -O /tmp/hangover.deb "$HANGOVER_DEB_URL"
    sudo dpkg -i /tmp/hangover.deb || sudo apt-get install -f -y
    rm -f /tmp/hangover.deb

    # 3b. Trova il binario wine64 (posizione varia tra versioni Hangover)
    WINE64=""
    for cand in "$(command -v wine64 || true)" /opt/hangover/bin/wine64 /usr/bin/wine64; do
        if [ -n "$cand" ] && [ -x "$cand" ]; then
            WINE64="$cand"
            break
        fi
    done
    if [ -z "$WINE64" ]; then
        echo "  ERRORE: binario wine64 Hangover non trovato."
        exit 1
    fi
    echo "  Binario wine64: $WINE64"

    export WINEPREFIX=/root/.wine-mt5
    export WINEARCH=win64

    # 3c. Inizializza il prefix Wine (primo avvio: ~1-2 min)
    if [ ! -d "$WINEPREFIX/drive_c" ]; then
        echo "  Inizializzazione prefix Wine (primo avvio, ~2 min)..."
        xvfb-run -a "$WINE64" wineboot --init || true
    fi

    # 3d. Installa MetaTrader 5 (silenzioso)
    if [ ! -f "$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe" ]; then
        echo "  Scarico e installo MetaTrader 5 (mt5setup.exe /auto)..."
        wget -q -O /tmp/mt5setup.exe \
            "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
        xvfb-run -a "$WINE64" /tmp/mt5setup.exe /auto || true
        rm -f /tmp/mt5setup.exe
        # L'installazione puo' richiedere tempo (download + estrazione)
        for i in $(seq 1 30); do
            if [ -f "$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe" ]; then
                break
            fi
            sleep 10
        done
        if [ ! -f "$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe" ]; then
            echo "  ERRORE: MetaTrader 5 non installato dopo 5 min. Controlla i log."
            exit 1
        fi
    fi
    echo "  MetaTrader 5 installato in: $WINEPREFIX/drive_c/Program Files/MetaTrader 5"

    # 3e. Installa Python Windows 3.11 dentro Wine (per il bot)
    if [ ! -f "$WINEPREFIX/drive_c/Python311/python.exe" ]; then
        echo "  Scarico e installo Python Windows 3.11..."
        wget -q -O /tmp/python311.exe \
            "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
        xvfb-run -a "$WINE64" /tmp/python311.exe \
            /quiet InstallAllUsers=1 TargetDir='C:\Python311' PrependPath=1 || true
        rm -f /tmp/python311.exe
        if [ ! -f "$WINEPREFIX/drive_c/Python311/python.exe" ]; then
            echo "  ERRORE: Python Windows non installato."
            exit 1
        fi
    fi

    # 3f. Dipendenze Python del bot dentro Wine
    echo "  Installo dipendenze Python dentro Wine (Windows pip)..."
    xvfb-run -a "$WINE64" 'C:\Python311\python.exe' -m pip install \
        --no-warn-script-location \
        MetaTrader5 pandas numpy flask requests python-dotenv werkzeug

    # Variabili per l'avvio del bot (niente Docker)
    BOT_CMD=(
        xvfb-run -a "$WINE64" 'C:\Python311\python.exe' run_master.py
    )
    MT5_BACKEND_VALUE="local"
    echo "  MT5 (Hangover) pronto."
else
    # ------------------------------------------------------------------
    # Percorso x86_64: DOCKER (gmag11/metatrader5-docker) + mt5linux
    # ------------------------------------------------------------------
    echo "  -> Docker + Wine + MetaTrader 5 (solo architettura x86_64)"

    # .env delle variabili per docker-compose
    MT5_LOGIN=$(grep '^MT5_LOGIN=' .env 2>/dev/null | cut -d= -f2)
    MT5_PASSWORD=$(grep '^MT5_PASSWORD=' .env 2>/dev/null | cut -d= -f2)
    MT5_SERVER=$(grep '^MT5_SERVER=' .env 2>/dev/null | cut -d= -f2)
    export MT5_LOGIN MT5_PASSWORD MT5_SERVER
    sudo docker-compose up -d
    echo "  MT5 in avvio... attendi 60 secondi per l'inizializzazione."
    sleep 60
    sudo docker-compose logs --tail=10 || true

    BOT_CMD=(python3 run_master.py)
    MT5_BACKEND_VALUE="mt5linux"
fi

# ==========================================================================
# 4. Crea o aggiorna .env
# I valori passati dal workflow aggiornano solo le chiavi gestite dal deploy;
# le altre impostazioni già presenti vengono conservate.
# ==========================================================================
echo "[4/7] Creazione/aggiornamento file .env..."
export MT5_BACKEND_VALUE
python3 - << 'PYEOF'
import os
from pathlib import Path

path = Path(".env")
defaults = {
    "MT5_BACKEND": os.environ.get("MT5_BACKEND_VALUE", "local"),
    "MT5_HOST": "localhost",
    "MT5_PORT": "18812",
    "MT5_LOGIN": "IL_TUO_LOGIN",
    "MT5_PASSWORD": "LA_TUA_PASSWORD",
    "MT5_SERVER": "MetaQuotes-Demo",
    "MT5_ATTACH_ONLY": "false",
    "SYMBOLS": "XAUUSD,USDJPY,GBPUSD,EURUSD",
    "ENABLED_MODES": "daytrading,swing",
    "EXECUTION_MODE": "pending_limit",
    "RISK_PERCENT": "7",
    "MIN_RR": "3.0",
    "AUTO_PENDING_DISTANCE_PIPS": "100",
    "MT5_HEALTH_CHECK_INTERVAL_SECONDS": "10",
    "MT5_RECONNECT_INITIAL_DELAY_SECONDS": "2",
    "MT5_RECONNECT_MAX_DELAY_SECONDS": "300",
    "TELEGRAM_BOT_TOKEN": "IL_TUO_TOKEN",
    "TELEGRAM_CHAT_ID": "IL_TUO_CHAT_ID",
    "TELEGRAM_NOTIFY_ONLY_ORDERS": "true",
    "WEBHOOK_SECRET_TOKEN": "IL_TUO_TOKEN_SEGRETO",
    "WEBHOOK_HOST": "0.0.0.0",
    "WEBHOOK_PORT": "5000",
    "CONSOLE_LOG_LEVEL": "CRITICAL",
    "SMC_SCAN_INTERVAL_SECONDS": "2",
    "OPEN_DASHBOARD_ON_START": "false",
    "VNC_PASSWORD": "changeme",
}

updates = {}
input_path = Path(".deploy-input.env")
if input_path.exists():
    for raw_line in input_path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        if key.strip() in defaults and value.strip():
            updates[key.strip()] = value.strip()
for key in defaults:
    value = os.environ.get(key, "").strip()
    if value:
        updates[key] = value

existing = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
seen = set()
output = []
for line in existing:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key = stripped.split("=", 1)[0].strip()
        if key in defaults:
            output.append(f"{key}={updates.get(key, stripped.split('=', 1)[1])}\n")
            seen.add(key)
            continue
    output.append(line if line.endswith("\n") else line + "\n")
for key, default in defaults.items():
    if key not in seen:
        output.append(f"{key}={updates.get(key, default)}\n")
path.write_text("".join(output), encoding="utf-8")
try:
    path.chmod(0o600)
except OSError:
    pass
PYEOF
echo "  File .env pronto (impostazioni esistenti conservate, valori forniti aggiornati)."

# ==========================================================================
# 5. Installa dipendenze Python sul SISTEMA (solo per script ausiliari;
#    il bot su ARM gira con il Python Windows in Wine)
# ==========================================================================
echo "[5/7] Dipendenze Python di supporto..."
pip3 install --user --quiet python-dotenv pandas numpy flask requests 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
echo "Dipendenze installate."

# ==========================================================================
# 6. Avvia il bot (con retry: al primo boot MT5 puo' essere ancora in login)
# ==========================================================================
echo "[6/7] Avvio del bot SMC..."
# Stop di eventuali istanze precedenti
pkill -f run_master.py 2>/dev/null || true
sleep 2

BOT_STARTED=0
for attempt in $(seq 1 5); do
    echo "  Tentativo avvio bot ($attempt/5)..."
    nohup "${BOT_CMD[@]}" > bot_output.log 2>&1 &
    sleep 25
    if pgrep -f run_master.py >/dev/null; then
        echo "  Bot in esecuzione!"
        BOT_STARTED=1
        break
    fi
    echo "  Il bot non e' partito, riprovo tra 30s (MT5 potrebbe essere in login)..."
    tail -5 bot_output.log || true
    sleep 30
done

if [ "$BOT_STARTED" != "1" ]; then
    echo "  [WARN] Il bot non e' partito dopo 5 tentativi. Log:"
    tail -20 bot_output.log 2>/dev/null || true
fi

echo ""
echo "============================================"
echo " DEPLOY COMPLETATO"
echo "============================================"
echo ""
echo "Comandi utili:"
echo "  tail -f bot_output.log           -> vedi i log del bot"
if [ "$IS_ARM" = "1" ]; then
echo "  xvfb-run -a wine64 'C:\\Python311\\python.exe' run_master.py"
echo "                                      -> avvia il bot manualmente (Hangover)"
else
echo "  docker-compose logs -f mt5        -> vedi i log di MT5"
echo "  docker-compose restart mt5        -> riavvia MT5"
echo "  kill \$(pgrep -f run_master.py)   -> ferma il bot"
fi
echo "  bash deploy.sh                    -> riesegui il deploy"
echo ""
echo "IMPORTANTE: se il .env contiene segnaposto (IL_TUO_...),"
echo "modificalo prima di riavviare:"
echo "  nano .env   poi: bash deploy.sh"
