# 🚀 SMC Trading Bot — Guida al Deployment su Oracle Cloud

## Panoramica

Questa è la versione **cloud** del bot SMC. Gira su **Linux Ubuntu** con MT5 dentro Docker via Wine.  
Nessun PC Windows richiesto — tutto funziona 24/7 su Oracle Cloud **GRATIS**.

```
┌──────────────────────────────────────────────┐
│  ORACLE CLOUD (Linux ARM64 - GRATIS)         │
│                                              │
│  ┌────────────────────────────────────────┐  │
│  │  Docker: Wine + MetaTrader 5          │  │
│  │          ↑↓ socket locale              │  │
│  │  Bot Python (mt5linux)                │  │
│  │    → run_master.py 24/7               │  │
│  └────────────────────────────────────────┘  │
│              ↑↓                               │
│     Internet: Telegram + TradingView          │
└──────────────────────────────────────────────┘
```

---

## 📋 Prerequisiti

1. **Account Oracle Cloud** (gratis): https://www.oracle.com/cloud/free/
   - Richiede carta di credito per verifica (MAI addebitata sul tier gratuito)
   - Crea una VM **Ampere ARM**, Ubuntu 22.04, 4 CPU, 24 GB RAM

2. **Terminale SSH** per connetterti alla VM (Windows: usa `Putty` o `Windows Terminal`)

3. **Credenziali MT5** (login, password, server) del tuo conto demo/reale

---

## 🔧 Setup — Passo per Passo

### Passo 1: Crea la VM su Oracle Cloud

1. Vai su https://cloud.oracle.com → Compute → Instances → Create Instance
2. **Image:** Ubuntu 22.04 (o 24.04)
3. **Shape:** Ampere ARM → 4 OCPU, 24 GB RAM
4. **Boot volume:** 200 GB
5. **SSH key:** genera una chiave o carica la tua pubblica
6. Clicca **Create**

### Passo 2: Connettiti via SSH

```bash
ssh ubuntu@<IP-DELLA-VM> -i ~/.ssh/la-tua-chiave
```

### Passo 3: Carica i file del bot sulla VM

Il codice vive nel repo GitHub **`bot_smc_cloud`** (questo repo): sulla VM basta
clonarlo, cosi' il deploy e' sempre allineato all'ultima versione:
```bash
cd ~
git clone https://github.com/FabrizioMarceca/bot_smc_cloud.git bot_smc
cd bot_smc
```

> **Automatismo**: il workflow GitHub Actions `oci-arm-provision.yml` crea
> l'istanza OCI (se non esiste), la clona e lancia `deploy.sh` da solo.
> Il flusso manuale qui sotto serve solo se vuoi gestire la VM a mano.

### Passo 4: Modifica il file `.env` con le tue credenziali

Sulla VM:
```bash
cd ~/bot_smc
nano .env
```

Inserisci:
```
MT5_BACKEND=mt5linux
MT5_LOGIN=12345678
MT5_PASSWORD=LaTuaPassword
MT5_SERVER=MetaQuotes-Demo
TELEGRAM_BOT_TOKEN=123456:ABCdef...
TELEGRAM_CHAT_ID=123456789
WEBHOOK_SECRET_TOKEN=il-tuo-token-segreto
SYMBOLS=XAUUSD,USDJPY,GBPUSD,EURUSD
```

### Passo 5: Esegui il deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

### Passo 6: Verifica che tutto funzioni

```bash
# Vedi i log del bot
tail -f bot_output.log

# Vedi i log di MT5 Docker
docker-compose logs -f mt5
```

---

## 🛠️ Comandi utili

| Comando | Descrizione |
|---|---|
| `tail -f bot_output.log` | Segui i log del bot in tempo reale |
| `docker-compose logs -f mt5` | Log del container MT5 |
| `docker-compose restart mt5` | Riavvia MT5 (se si blocca) |
| `docker-compose down && docker-compose up -d` | Ricostruisci container |
| `pkill -f run_master.py && python3 run_master.py &` | Riavvia solo il bot |
| `git pull && bash deploy.sh` | Aggiorna il bot all'ultima versione e rideploy |

---

## 🔒 Sicurezza

- Solo TU puoi accedere al server via SSH (chiave privata)
- Il webhook (porta 5000) è protetto da `WEBHOOK_SECRET_TOKEN`
- Telegram notifica solo i tuoi `TELEGRAM_CHAT_ID`
- MT5 è dentro Docker, non esposto all'esterno

---

## ⚠️ Troubleshooting

| Problema | Soluzione |
|---|---|
| `mt5.initialize() fallita` | MT5 Docker non ancora pronto → attendi 60s e riprova |
| `No connection to MT5` | Verifica `docker-compose ps` che il container sia `Up` |
| `Simbolo non trovato` | Aggiungi il simbolo al Market Watch di MT5 via VNC |
| `ModuleNotFoundError: mt5linux` | `pip3 install mt5linux` dentro la VM |

---

## 📊 Differenze vs versione Windows

| Cosa | Windows (locale) | Oracle Cloud (questa) |
|---|---|---|
| Sistema operativo | Windows 10/11 | Ubuntu Linux (ARM64) |
| MT5 | Desktop nativo | Docker + Wine |
| Backend MT5 | `MT5_BACKEND=local` | `MT5_BACKEND=mt5linux` |
| Avvio | `python run_master.py` | `./deploy.sh` |
| Deploy | `avvia_tutto.bat` | GitHub Actions `oci-arm-provision.yml` |
| Spegnimento PC | Bot si ferma | Bot continua 24/7 |

---

## 💰 Costi

**€0 al mese.** Oracle Cloud Free Tier include per sempre:
- 4 CPU ARM, 24 GB RAM
- 200 GB disco
- 10 TB traffico mensile

Nessuna carta addebitata oltre la verifica iniziale.
