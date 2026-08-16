# 🇩🇪 SMC Trading Bot — Guida OCI Germany Central (Francoforte)

Questa guida spiega **due cose**:

1. **Come creare l'istanza OCI** nella regione *Germany Central (Francoforte)*
   (`eu-frankfurt-1`) — sia a mano dalla Console, sia col workflow GitHub.
2. **Come modificare lo script GitHub già esistente** (`.github/workflows/oci-arm-provision.yml`
   e `.github/scripts/oci_provision.py`) per **forzare Francoforte** come regione di origine.

---

## 0. Nomi utili (memorizza questi)

| Cosa | Valore |
|---|---|
| Regione (label OCI) | **Germany Central (Frankfurt)** |
| Regione (identificatore CLI) | **`eu-frankfurt-1`** |
| Availability Domains | FRA-AD-1, FRA-AD-2, FRA-AD-3 |
| Shape Always Free | `VM.Standard.A1.Flex` (Ampere ARM, 4 OCPU / 24 GB) |
| Immagine | Canonical Ubuntu **22.04** (aarch64) |
| Utente SSH | `ubuntu` |
| Nome istanza usato dal workflow | `smc-bot-arm` |
| Porte da aprire | `22/tcp` (SSH) e `5000/tcp` (webhook TradingView) |

---

## PARTE A — Creare l'istanza a mano dalla Console OCI

### A1. Account e Home Region

1. Vai su https://signup.cloud.oracle.com e crea l'account (serve una carta
   solo per la verifica; il tier Always Free non viene addebitato).
2. Nel form di registrazione, imposta **Home Region = "Germany Central (Frankfurt)"**.
   → Se l'account **esiste già** con un'altra Home Region, non puoi cambiarla da solo:
     puoi comunque **sottoscrivere Francoforte** (vedi A2) e usarla, oppure aprire un
     nuovo account con Home Region Francoforte.

### A2. (Solo se l'account esiste già) Sottoscrivi la regione Francoforte

1. Console → menu ☰ → **Governance & Administration → Region Management**.
2. Cerca **Germany Central (Frankfurt)** e clicca **Subscribe**.
3. Attendi che lo stato diventi **Active/Ready**.
   → Nota: il workflow usa solo regioni già sottoscritte (`READY`), quindi questo passo
     è obbligatorio se Francoforte non è la Home Region.

### A3. Crea la rete (VCN + subnet pubblica + Internet Gateway)

1. Console → menu ☰ → **Networking → Virtual Cloud Networks**.
2. Seleziona il **compartment** giusto (in alto a sinistra) e clicca **Create VCN**.
3. Scegli **Create VCN with Internet Connectivity** (crea VCN + subnet pubblica +
   Internet Gateway + route table + security list in un colpo solo).
   - Nome: `vcn-francoforte`
   - CIDR: `10.0.0.0/16` (default)
4. Clicca **Create**.

### A4. Genera la chiave SSH (sul tuo PC)

```bash
ssh-keygen -t rsa -b 2048 -f ~/.ssh/oci_francoforte -N ""
```

- La chiave **pubblica** (`oci_francoforte.pub`) serve per creare l'istanza.
- La chiave **privata** (`oci_francoforte`) serve per collegarti via SSH.

### A5. Crea l'istanza

1. Console → menu ☰ → **Compute → Instances → Create instance**.
2. Compila così:

| Campo | Valore |
|---|---|
| Name | `smc-bot-arm` ⚠️ (deve essere esattamente questo per il workflow) |
| Placement | Availability domain `FRA-AD-1` (o lascia scegliere) |
| Image | **Canonical Ubuntu 22.04** (filtra per "Shape: Ampere" per vedere le aarch64) |
| Shape | **Ampere → `VM.Standard.A1.Flex`** → 4 OCPU, 24 GB RAM |
| VCN / Subnet | la VCN creata in A3 + **subnet pubblica** |
| Public IPv4 address | **Yes** (assegna IP pubblico) |
| SSH keys | incolla il contenuto di `oci_francoforte.pub` |
| Boot volume | 200 GB |

3. Clicca **Create** e aspetta che lo stato diventi **Running**.
4. Copia la **Public IP** dell'istanza.

### A6. Apri le porte nel security list (cloud firewall)

`deploy.sh` apre le porte con `ufw` **dentro** la VM, ma serve anche la regola
sul **security list** OCI, altrimenti TradingView non raggiunge il webhook.

1. Networking → Virtual Cloud Networks → la tua VCN → **Security Lists** →
   apri il security list della subnet pubblica → **Add Ingress Rules**.
2. Aggiungi:

| Source | IP protocol | Source port | Destination port |
|---|---|---|---|
| `0.0.0.0/0` | TCP | All | **5000** |
| `0.0.0.0/0` | TCP | All | 22 (di solito già presente) |

3. Salva.

### A7. Collegati via SSH e verifica

```bash
ssh ubuntu@<IP-DELLA-VM> -i ~/.ssh/oci_francoforte
```

Se l'istanza è stata creata a mano, puoi deployare direttamente da SSH:

```bash
cd ~
git clone https://github.com/Giovanni27032007/bot_smc_cloud.git bot_smc
cd bot_smc
nano .env            # inserisci MT5 + Telegram + webhook
chmod +x deploy.sh
./deploy.sh
```

---

## PARTE B — Modificare lo script GitHub esistente per forzare Francoforte

### B1. Come funziona oggi (per capire cosa toccare)

- `.github/workflows/oci-arm-provision.yml` fa la **scoperta** delle regioni
  sottoscritte e delle subnet pubbliche, poi chiama lo script di provisioning.
- `.github/scripts/oci_provision.py` lancia davvero l'istanza
  (`VM.Standard.A1.Flex`), riusando una VM esistente con nome `smc-bot-arm`.
- L'ordine attuale delle regioni è: **Home Region prima**, poi una lista fissa:

```python
preferred_order = [
    preferred_region,
    "eu-madrid-1", "eu-frankfurt-1", "eu-amsterdam-1",
    "uk-london-1", "eu-paris-1", "eu-marseille-1",
    "eu-milan-1",
]
rank = {name: index for index, name in enumerate(x for x in preferred_order if x)}
ready.sort(key=lambda item: (
    0 if item["home"] else 1,          # ← la Home Region vince sempre
    rank.get(item["name"], 999),
    item["name"],
))
```

👉 Il problema: **anche mettendo `eu-frankfurt-1` come `OCI_REGION`, la Home Region
viene comunque scelta prima**, se Francoforte non è la Home Region.

### B2. Modifica minima (consigliata se Francoforte È la Home Region)

Se hai creato l'account **con Home Region Francoforte**, non serve toccare il codice.
Basta impostare i **GitHub Secrets** (vedi B4) con:

- `OCI_REGION` = `eu-frankfurt-1`
- `OCI_SUBNET_ID` = l'OCID della subnet pubblica di Francoforte

### B3. Modifica del codice (forza Francoforte anche se non è Home Region)

Apri `.github/workflows/oci-arm-provision.yml` e sostituisci questo blocco:

**PRIMA:**
```python
          preferred_order = [
              preferred_region,
              "eu-madrid-1", "eu-frankfurt-1", "eu-amsterdam-1",
              "uk-london-1", "eu-paris-1", "eu-marseille-1",
              "eu-milan-1",
          ]
          rank = {name: index for index, name in enumerate(x for x in preferred_order if x)}
          ready.sort(key=lambda item: (
              0 if item["home"] else 1,
              rank.get(item["name"], 999),
              item["name"],
          ))
```

**DOPO:**
```python
          preferred_order = [
              "eu-frankfurt-1",          # ← Francoforte sempre per prima
              preferred_region,
              "eu-madrid-1", "eu-amsterdam-1",
              "uk-london-1", "eu-paris-1", "eu-marseille-1",
              "eu-milan-1",
          ]
          rank = {name: index for index, name in enumerate(x for x in preferred_order if x)}
          ready.sort(key=lambda item: (
              0 if item["name"] == "eu-frankfurt-1" else 1,   # ← forza Francoforte
              0 if item["home"] else 1,
              rank.get(item["name"], 999),
              item["name"],
          ))
```

Cosa cambia:
- La chiave di ordinamento mette **`eu-frankfurt-1` sempre a rank 0**, prima della
  Home Region.
- `eu-frankfurt-1` è anche il primo elemento della lista `preferred_order`,
  quindi il provisioning proverà Francoforte per primo.
- Il resto (fallback su Madrid/Amsterdam/ecc.) resta invariato come rete di sicurezza.

> Se preferisci NON hardcodare `eu-frankfurt-1` ma usare il secret, puoi scrivere
> `0 if item["name"] == preferred_region else 1` e impostare `OCI_REGION=eu-frankfurt-1`.
> Il risultato è identico; la versione hardcodata è più esplicita.

### B4. Configura i GitHub Secrets

Repo GitHub → **Settings → Secrets and variables → Actions → New repository secret**.
Crea questi secret:

| Secret | Valore |
|---|---|
| `OCI_USER_ID` | OCID utente (My Profile) |
| `OCI_TENANCY_ID` | OCID tenancy |
| `OCI_KEY_FINGERPRINT` | fingerprint della API key |
| `OCI_PRIVATE_KEY` | contenuto della chiave privata API (`oci_api_key.pem`) |
| `OCI_REGION` | `eu-frankfurt-1` |
| `OCI_COMPARTMENT_ID` | OCID del compartment |
| `OCI_SUBNET_ID` | OCID della subnet pubblica di Francoforte |
| `OCI_SSH_PUBLIC_KEY` | contenuto di `oci_francoforte.pub` |
| `OCI_SSH_PRIVATE_KEY` | contenuto di `oci_francoforte` (chiave privata) |
| `BOT_REPO_URL` | `https://github.com/Giovanni27032007/bot_smc_cloud.git` |
| `MT5_LOGIN` | login conto MT5 |
| `MT5_PASSWORD` | password MT5 |
| `MT5_SERVER` | server MT5 (es. `MetaQuotes-Demo`) |
| `TELEGRAM_BOT_TOKEN` | token del bot Telegram |
| `TELEGRAM_CHAT_ID` | chat id Telegram |
| `WEBHOOK_SECRET_TOKEN` | token segreto del webhook |
| `VNC_PASSWORD` | (opzionale) password VNC, default `changeme` |
| `OCI_INSTANCE_IP` | (opzionale) IP per il job `keepalive` |

#### Come ottenere la API key OCI (per i secret sopra)

1. Console → icona profilo (in alto a destra) → **My Profile**.
2. Copia **User OCID** e **Tenancy OCID**.
3. Tab **API Keys → Add API key → Generate API key pair**.
4. Scarica la chiave **privata** (`oci_api_key.pem`) e annota la **fingerprint**.
5. Copia il blocco "Configuration file preview": contiene `user`, `fingerprint`,
   `tenancy`, `region`.

#### Come ottenere l'OCID della subnet

1. Networking → Virtual Cloud Networks → la tua VCN → **Subnets**.
2. Clicca la subnet pubblica e copia l'**OCID**.

### B5. Eseguire il workflow

- **A mano:** repo → **Actions → OCI ARM Host Capacity → Run workflow**.
- **Automatico:** il workflow gira già ogni 3 ore (`cron: '0 */3 * * *'`).
- Se esiste già una VM `smc-bot-arm` (anche creata a mano in A5), il workflow la
  **riusa** invece di crearne una seconda. Se è STOPPED la riavvia.

---

## PARTE C — Verifica finale

1. In Actions controlla che il job **provision** finisca con
   `SUCCESSO: regione=eu-frankfurt-1 ...`.
2. Il job **deploy** clona il repo sulla VM, scrive il `.env` dai secret e lancia
   `deploy.sh` (su ARM installa Hangover + MetaTrader 5 + Python Windows).
3. Dalla VM controlla il bot:

```bash
tail -f ~/bot_smc/bot_output.log
```

4. Testa il webhook TradingView verso `http://<IP-DELLA-VM>:5000/webhook`
   con l'header/token configurato in `WEBHOOK_SECRET_TOKEN`.

---

## ⚠️ Troubleshooting

| Problema | Causa / Soluzione |
|---|---|
| `nessuna regione READY` nel log del workflow | Francoforte non è sottoscritta → fai **A2** e rilancia |
| Il workflow sceglie la Home Region invece di Francoforte | Non hai applicato la modifica **B3** |
| `out of host capacity` su Francoforte | Capacità ARM esaurita in quel momento: il workflow prova Madrid/Amsterdam. Rilancia più tardi o usa un altro AD |
| Webhook non raggiungibile dall'esterno | Manca la regola ingress `5000/tcp` nel security list (**A6**) |
| `mt5.initialize()` fallita al primo avvio | MT5 dentro Wine è ancora in login: `deploy.sh` già ritenta 5 volte |
| Il workflow crea una seconda VM | Il nome deve essere `smc-bot-arm` (**A5**), altrimenti non viene riusata |
