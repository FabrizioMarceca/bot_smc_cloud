"""
config.py
=========
Configurazione centralizzata del bot. Tutti i segreti sono letti dal file
``.env`` (mai hardcodati) tramite ``python-dotenv``. Il parsing e' difensivo:
una variabile mancante o malformata diventa ``None`` invece di far crashare
l'import del modulo, cosi' i moduli restano importabili anche in ambienti di
test/CI privi di credenziali reali.
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv

# Carica il .env che sta ACCANTO a questo file, indipendentemente dalla cartella
# da cui viene lanciato lo script (portabile tra PC diversi).
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_ENV_PATH, override=True)


# ---------------------------------------------------------------------------
# Helper di parsing difensivo
# ---------------------------------------------------------------------------

def _get_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = _get_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_str(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "si", "y"}


def _get_csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _get_str(name)
    if raw is None:
        return default
    items = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return items or default


# ---------------------------------------------------------------------------
# Credenziali MetaTrader 5
# ---------------------------------------------------------------------------

# Se il terminale MT5 e' gia' loggato manualmente, initialize() puo' agganciarsi
# senza credenziali. Se invece si vuole forzare il login da codice, questi
# valori (letti da .env) vengono passati a mt5.initialize(login=, password=, server=).
MT5_LOGIN: Optional[int] = _get_int("MT5_LOGIN")
MT5_PASSWORD: Optional[str] = _get_str("MT5_PASSWORD")
MT5_SERVER: Optional[str] = _get_str("MT5_SERVER")
# Path opzionale all'eseguibile terminale64.exe (utile con installazioni multiple).
MT5_TERMINAL_PATH: Optional[str] = _get_str("MT5_TERMINAL_PATH")
# Se True (default): ci si aggancia al terminale gia' aperto e loggato a mano,
# IGNORANDO le credenziali sopra (evita 'Authorization failed' da login via codice).
# Metti False solo se vuoi che sia il codice a fare il login con le credenziali.
MT5_ATTACH_ONLY: bool = _get_bool("MT5_ATTACH_ONLY", True)

# Backend di connessione MT5 (letto da mt5_adapter):
#   "local"     -> MetaTrader5 nativo (Windows, terminale aperto)  [default]
#   "mt5linux"  -> container Docker+Wine sulla VM Oracle ARM
# Sulla VM deploy.sh scrive MT5_BACKEND=mt5linux nel .env.
MT5_BACKEND: str = (_get_str("MT5_BACKEND", "local") or "local").lower()
# Host/porta del bridge mt5linux (solo backend mt5linux).
MT5_HOST: str = _get_str("MT5_HOST", "localhost") or "localhost"
MT5_PORT: int = _get_int("MT5_PORT", 18812) or 18812

# Controllo salute e riconnessione MT5. Tutti i valori sono in secondi.
# Il reconnect viene eseguito esclusivamente dal main thread del bot.
MT5_HEALTH_CHECK_INTERVAL_SECONDS: int = _get_int(
    "MT5_HEALTH_CHECK_INTERVAL_SECONDS", 10
) or 10
MT5_RECONNECT_INITIAL_DELAY_SECONDS: int = _get_int(
    "MT5_RECONNECT_INITIAL_DELAY_SECONDS", 2
) or 2
MT5_RECONNECT_MAX_DELAY_SECONDS: int = _get_int(
    "MT5_RECONNECT_MAX_DELAY_SECONDS", 300
) or 300


# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------

WEBHOOK_SECRET_TOKEN: Optional[str] = _get_str("WEBHOOK_SECRET_TOKEN")
WEBHOOK_HOST: str = _get_str("WEBHOOK_HOST", "0.0.0.0") or "0.0.0.0"
WEBHOOK_PORT: int = _get_int("WEBHOOK_PORT", 5000) or 5000

# Apre automaticamente la dashboard nel browser all'avvio del bot.
# Disattivabile con OPEN_DASHBOARD_ON_START=false nel .env.
OPEN_DASHBOARD_ON_START: bool = (
    (_get_str("OPEN_DASHBOARD_ON_START", "true") or "true").lower()
    in ("1", "true", "yes", "si", "s")
)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: Optional[str] = _get_str("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: Optional[str] = _get_str("TELEGRAM_CHAT_ID")
# Chat ID multipli (separati da virgola). Viene fuso con TELEGRAM_CHAT_ID
# cosi' l'utente non perde mai il proprio ID se imposta entrambi.
_chat_ids_set: set[str] = set()
_raw_multi = _get_str("TELEGRAM_CHAT_IDS")
if _raw_multi:
    _chat_ids_set.update(c.strip() for c in _raw_multi.split(",") if c.strip())
if TELEGRAM_CHAT_ID:
    _chat_ids_set.add(TELEGRAM_CHAT_ID)
TELEGRAM_CHAT_IDS: tuple[str, ...] = tuple(_chat_ids_set)

# ==========================================================================
# TELEGRAM NOTIFICHE (nuove tipologie)
# ==========================================================================
# Con la console silenziosa, Telegram e' il canale principale per monitorare
# errori e problemi operativi. Ogni tipologia e' attivabile singolarmente:
#
#   TELEGRAM_NOTIFY_ERRORS:  alert automatici su ERROR/CRITICAL del log
#                            (ordini rifiutati, margine insufficiente, ecc.)
#                            e notifica quando i log vengono svuotati.
#   TELEGRAM_NOTIFY_PENDING: avvisi quando il bot piazza un PENDING LIMIT
#                            (OB lontano dal mercato -> attesa al livello OB).
#   TELEGRAM_ALERT_THROTTLE_MIN: minuti minimi tra due alert dello stesso
#                            tipo (anti-spam, default 5).
TELEGRAM_NOTIFY_ERRORS: bool = _get_bool("TELEGRAM_NOTIFY_ERRORS", True)
TELEGRAM_NOTIFY_PENDING: bool = _get_bool("TELEGRAM_NOTIFY_PENDING", True)
TELEGRAM_ALERT_THROTTLE_MIN: int = _get_int("TELEGRAM_ALERT_THROTTLE_MIN", 5) or 5

# ==========================================================================
# NOTIFICHE TELEGRAM — SOLO ORDINI
# ==========================================================================
# Con True (default), il bot invia su Telegram SOLO le notifiche di ordini
# piazzati/aperti (market, pending limit, webhook trade, posizione aperta).
# Tutte le altre notifiche automatiche vengono silenziate:
#   - heartbeat (bot vivo ma nessun setup)
#   - report giornaliero delle 23:00
#   - aggiornamenti OB in formazione
#   - break-even attivato
#   - chiusure parziali ai TP
#   - posizioni chiuse
#   - alert errori (TelegramAlertHandler)
#   - log svuotati dalla dashboard
# Imposta False nel .env per riattivare tutte le notifiche.
TELEGRAM_NOTIFY_ONLY_ORDERS: bool = _get_bool("TELEGRAM_NOTIFY_ONLY_ORDERS", True)


# ---------------------------------------------------------------------------
# Parametri operativi
# ---------------------------------------------------------------------------

# Modalita' prop firm: applica i rischi ridotti (0.5% / 0.25%).
PROP_MODE: bool = _get_bool("PROP_MODE", False)

# Simboli monitorati dal loop di break-even (Market Watch deve contenerli).
SYMBOLS: tuple[str, ...] = _get_csv("SYMBOLS", ("XAUUSD",))

# Deviazione massima (in points) tollerata sugli ordini a mercato.
ORDER_DEVIATION: int = _get_int("ORDER_DEVIATION", 20) or 20

# Intervallo (secondi) del ciclo di gestione break-even sul runner.
BREAK_EVEN_INTERVAL_SECONDS: int = _get_int("BREAK_EVEN_INTERVAL_SECONDS", 10) or 10

# Intervallo (secondi) tra le scansioni SMC autonome.
# 2s per reattivita' quasi real-time: il mercato continua continuamente
# e ogni secondo puo' portare nuovi sweep / setup.
# I LOG dettagliati sono throttlati separatamente (LOG_OB_DEBUG_INTERVAL_SECONDS).
SMC_SCAN_INTERVAL_SECONDS: int = _get_int("SMC_SCAN_INTERVAL_SECONDS", 2) or 2

# Intervallo (secondi) tra i log dettagliati degli OB (OB-RAW, OB-MIT, OB-PD, OB-SWEEP).
# Le scansioni girano a SMC_SCAN_INTERVAL_SECONDS (2s), ma i log diagnostici
# vengono stampati solo ogni N secondi per evitare crash da log eccessivo.
# Default 30s: buon compromesso tra debug e leggibilità.
LOG_OB_DEBUG_INTERVAL_SECONDS: int = _get_int("LOG_OB_DEBUG_INTERVAL_SECONDS", 30) or 30

# Soglia (in pip) oltre la quale, se l'entry OB e' lontana dal prezzo di
# mercato, il bot NON entra a mercato ma piazza un PENDING LIMIT al livello OB
# (evita SL a pochi pip dal prezzo e stop-out quasi certo).
# Legacy: il limite effettivo per i pending autonomi è ora determinato da
# get_max_pending_distance_pips(mode), più restrittivo e specifico per modalità.
# Manteniamo questa variabile per compatibilità con configurazioni esistenti.
AUTO_PENDING_DISTANCE_PIPS: int = _get_int("AUTO_PENDING_DISTANCE_PIPS", 100) or 100

# Limiti di validità per la distanza dell'entry dal mercato. Un setup oltre
# questi limiti viene scartato: non deve essere trasformato in un pending GTC
# destinato a restare sul mercato per giorni.
#
# Valori espressi in pip, non in points. Questo è un filtro di attesa
# dell'ENTRY, NON un tetto al profitto: per lo swing il TP può superare
# liberamente 120 pip quando la struttura H4/Daily lo giustifica.
MAX_PENDING_DISTANCE_PIPS: dict[str, int] = {
    "daytrading": 80,
    "swing": 250,
}


def get_max_pending_distance_pips(mode: str | None = None) -> int:
    """Massima distanza dell'entry dal mercato per la modalità indicata."""
    return MAX_PENDING_DISTANCE_PIPS.get(mode or TRADING_MODE, 80)

# ==========================================================================
# LOGGING
# ==========================================================================
# Livello minimo dei log mostrati nel TERMINALE (console).
# Il debug completo viene SEMPRE scritto su bot_smc.log ed e' visibile nella
# dashboard (sezione "Log Recenti"), quindi la console puo' restare pulita.
# Valori: DEBUG, INFO, WARNING, ERROR, CRITICAL.
#   CRITICAL = console praticamente silenziosa (default, solo errori fatali)
#   ERROR    = solo errori
#   INFO     = riattiva tutti i log in console (come prima)
CONSOLE_LOG_LEVEL: str = (_get_str("CONSOLE_LOG_LEVEL", "CRITICAL") or "CRITICAL").upper()

# Modalita' esecuzione ordini: "market" (default) o "pending_limit".
EXECUTION_MODE: str = (_get_str("EXECUTION_MODE", "market") or "market").lower()

# Chiave legacy (retro-compatibilita' con i moduli di analisi grafica).
GEMINI_API_KEY: Optional[str] = _get_str("GEMINI_API_KEY")
# Rischio % su ogni trade (default 7 = 7%, profilo aggressivo)
RISK_PERCENT: float = float(_get_str("RISK_PERCENT", "7") or "7")

# Ora del riepilogo giornaliero Telegram (24h, default 23 = 23:00).
DAILY_REPORT_HOUR: int = _get_int("DAILY_REPORT_HOUR", 23) or 23

# ==========================================================================
# MODALITA' DI TRADING ATTIVE (multi-mode parallelo)
# ==========================================================================
# Sono operative esclusivamente le modalità documentate per questo bot:
# daytrading e swing. Lo scalping non è più una modalità valida.
#
# ENABLED_MODES: lista separata da virgola (default: daytrading,swing).
# TRADING_MODE (legacy): se impostato, sostituisce ENABLED_MODES.
_ENABLED_RAW = _get_str("ENABLED_MODES", "daytrading,swing")
_TRADING_MODE_LEGACY = (_get_str("TRADING_MODE") or "").lower()
if _TRADING_MODE_LEGACY and _TRADING_MODE_LEGACY in ("daytrading", "swing"):
    ENABLED_MODES: tuple[str, ...] = (_TRADING_MODE_LEGACY,)
else:
    _modes = [m.strip().lower() for m in (_ENABLED_RAW or "daytrading,swing").split(",") if m.strip()]
    _modes = [m for m in _modes if m in ("daytrading", "swing")]
    ENABLED_MODES = tuple(_modes) if _modes else ("daytrading", "swing")
TRADING_MODE: str = ENABLED_MODES[0]  # retro-compatibilita' (prima modalita' attiva)

# ==========================================================================
# Magic numbers per modalita' (ogni modalita' ha il suo)
# ==========================================================================
#   daytrading: 1002 → posizioni intraday M1-M15
#   swing:      1003 → posizioni H1-H4+, SL strutturale
#   main:       1000 → legacy/webhook
_MODE_MAGIC: dict[str, int] = {
    "daytrading": 1002,
    "swing": 1003,
}
ALL_MODE_MAGICS: frozenset[int] = frozenset(_MODE_MAGIC.values())

def get_mode_magic(mode: str) -> int:
    """Ritorna il magic number per la modalita'."""
    return _MODE_MAGIC.get(mode, 1000)

# ==========================================================================
# Timeframe di analisi per ogni modalita'
# ==========================================================================
# htf = Higher Time Frame (direzione trend)
# mtf = Medium Time Frame (entry + Order Block)
# ltf = Lower Time Frame (conferma sniper, None = non serve)
#
# Timeframe operativi mantenuti nel formato legacy (HTF, MTF, LTF).
# La pipeline completa, inclusi i filtri macro e il livello intermedio, è
# esposta da get_mode_timeframe_pipeline().
_MODE_TIMEFRAMES: dict[str, tuple[str, str, str | None]] = {
    "daytrading": ("H4", "M15", "M1"),
    "swing":      ("H4", "H1", "M15"),
}

# Pipeline usata dall'analisi autonoma:
# daytrading: D1 → H4 → M15 → M5 → M1
# swing:      D1 → H4 → H1 → M15
_MODE_TIMEFRAME_PIPELINES: dict[str, tuple[str, ...]] = {
    "daytrading": ("D1", "H4", "M15", "M5", "M1"),
    "swing": ("D1", "H4", "H1", "M15"),
}

def get_mode_timeframe_pipeline(mode: str | None = None) -> tuple[str, ...]:
    """Ritorna la gerarchia completa dei timeframe per la modalità."""
    m = mode if mode else TRADING_MODE
    return _MODE_TIMEFRAME_PIPELINES.get(m, _MODE_TIMEFRAME_PIPELINES["daytrading"])


def get_mode_timeframes(mode: str | None = None) -> tuple[str, str, str | None]:
    """Ritorna (htf_label, mtf_label, ltf_label) per la modalita'.

    Se mode=None, usa TRADING_MODE (prima modalita' abilitata).
    """
    m = mode if mode else TRADING_MODE
    labels = _MODE_TIMEFRAMES.get(m, _MODE_TIMEFRAMES["swing"])
    return labels[0], labels[1], labels[2]

def get_mode_timeframes_mt5(mode: str | None = None) -> tuple[int, int, int | None]:
    """Come get_mode_timeframes() ma ritorna le costanti MT5."""
    from mt5_adapter import mt5 as _mt5
    _MAP = {
        "M1": _mt5.TIMEFRAME_M1,
        "M5": _mt5.TIMEFRAME_M5,
        "M15": _mt5.TIMEFRAME_M15,
        "H1": _mt5.TIMEFRAME_H1,
        "D1": getattr(_mt5, "TIMEFRAME_D1", _mt5.TIMEFRAME_H4),
        "H4": _mt5.TIMEFRAME_H4,
    }
    htf_label, mtf_label, ltf_label = get_mode_timeframes(mode)
    ltf = _MAP[ltf_label] if ltf_label else None
    return _MAP[htf_label], _MAP[mtf_label], ltf

# ==========================================================================
# Range SL per modalita' di trading (estratti dai Video 22, 25, 31, 32, 33)
# ==========================================================================
# Formato per ogni modalita':
#   {simbolo: (min_sl_pips, max_sl_pips)}
#   - min_sl_pips: SL minimo consigliato (sotto = troppo stretto, rumore)
#   - max_sl_pips: SL massimo consigliato (sopra = lotti microscopici)
#   - Il simbolo "_default" viene usato se il pair non e' nella lista.
#
# DAYTRADING (M1-M15):
# Range operativi elastici: il bot cerca prima la fascia indicata, ma la
# struttura del mercato resta prioritaria. Il valore massimo è una barriera
# di proporzione, non un invito a stringere lo SL dentro una distanza fissa.
#   EURUSD/GBPUSD: 15-25 pip
#   GBPJPY/USDJPY: 25-40 pip
#   XAUUSD: 50-100 pip (eventualmente più largo solo con volatilità/struttura
#           eccezionale, mantenendo il rischio monetario invariato)
SL_DAYTRADING: dict[str, tuple[int, int]] = {
    "XAUUSD": (50, 100),
    "EURUSD": (15, 25),
    "GBPUSD": (15, 25),
    "GBPJPY": (25, 40),
    "USDJPY": (25, 40),
    "_default": (15, 40),
}
# SWING (H4-Daily): lo SL resta strutturale e proporzionato al simbolo.
# Questi sono i range normali; nello swing il massimo può estendersi del 25%
# quando la struttura H4 o la volatilità richiedono più spazio. XAUUSD resta
# invece rigidamente entro il massimo nominale di 200 pip.
# L'estensione non modifica il rischio: il lotto viene ricalcolato sulla
# distanza effettiva dello SL.
SL_SWING: dict[str, tuple[int, int]] = {
    "XAUUSD": (100, 200),  # nessuna estensione swing oltre 200 pip
    "EURUSD": (15, 30),
    "GBPUSD": (15, 30),
    "GBPJPY": (30, 40),
    "USDJPY": (30, 40),
    "_default": (15, 50),
}
SWING_SL_TOLERANCE_PCT: float = float(
    _get_str("SWING_SL_TOLERANCE_PCT", "0.25") or "0.25"
)

# Funzione helper: ritorna (min_sl, max_sl) normale in pip per un simbolo e modalita'
def _get_sl_range(symbol: str, mode: str | None = None) -> tuple[int, int]:
    """Ritorna (min_sl, max_sl) in pip per il simbolo e modalita'."""
    m = mode if mode else TRADING_MODE
    table = {"daytrading": SL_DAYTRADING, "swing": SL_SWING}
    sl_table = table.get(m, SL_SWING)
    sym_upper = symbol.upper()
    return sl_table.get(sym_upper, sl_table["_default"])

def get_sl_min_pips(symbol: str, mode: str | None = None) -> int:
    """SL minimo consigliato in pip per il simbolo e modalita'."""
    return _get_sl_range(symbol, mode)[0]

def get_sl_nominal_max_pips(symbol: str, mode: str | None = None) -> int:
    """Massimo nominale della fascia SL, prima della tolleranza swing."""
    return _get_sl_range(symbol, mode)[1]


def get_sl_max_pips(symbol: str, mode: str | None = None) -> float:
    """Massimo operativo SL in pip, con tolleranza swing escluso XAUUSD."""
    nominal_max = get_sl_nominal_max_pips(symbol, mode)
    m = mode if mode else TRADING_MODE
    if m == "swing" and symbol.upper() != "XAUUSD":
        return nominal_max * (1.0 + max(0.0, SWING_SL_TOLERANCE_PCT))
    return float(nominal_max)

# R:R minimo: daytrading 1:2 per pro-trend e counter-trend.
# Lo swing ha una regola più severa: minimo 1:4.
MIN_RR: float = float(_get_str("MIN_RR", "3.0") or "3.0")
DAYTRADING_MIN_RR: float = float(_get_str("DAYTRADING_MIN_RR", "2.0") or "2.0")
SWING_MIN_RR: float = float(_get_str("SWING_MIN_RR", "4.0") or "4.0")
SWING_ENTRY_OFFSET_PIPS: float = float(
    _get_str("SWING_ENTRY_OFFSET_PIPS", "2.0") or "2.0"
)
# Distanza massima tra POI MTF e POI LTF per la conferma dello swing.
# La conferma deve avvenire nella stessa zona, non solo nella stessa direzione.
SWING_CONFIRMATION_MAX_DISTANCE_PIPS: float = float(
    _get_str("SWING_CONFIRMATION_MAX_DISTANCE_PIPS", "10.0") or "10.0"
)

# ===========================================================================
# FILTRO LIQUIDITÀ PRE-ENTRY
# ===========================================================================
# Il gate finale distingue la liquidità realmente presa da un semplice
# attraversamento del livello e impedisce di inseguire un movimento già esteso.
LIQUIDITY_FRONT_BUFFER_R: float = float(
    _get_str("LIQUIDITY_FRONT_BUFFER_R", "1.0") or "1.0"
)
LIQUIDITY_MAX_ENTRY_EXTENSION_R: float = float(
    _get_str("LIQUIDITY_MAX_ENTRY_EXTENSION_R", "2.5") or "2.5"
)
LIQUIDITY_MAX_ENTRY_EXTENSION_ATR: float = float(
    _get_str("LIQUIDITY_MAX_ENTRY_EXTENSION_ATR", "3.0") or "3.0"
)
LIQUIDITY_MIN_SWEEP_RECLAIM_RATIO: float = float(
    _get_str("LIQUIDITY_MIN_SWEEP_RECLAIM_RATIO", "0.10") or "0.10"
)
# Penetrazione minima reale oltre il livello di liquidità. Evita di trattare
# un attraversamento di pochi decimali/tick come sweep istituzionale.
LIQUIDITY_MIN_SWEEP_PENETRATION_PIPS: float = float(
    _get_str("LIQUIDITY_MIN_SWEEP_PENETRATION_PIPS", "0.1") or "0.1"
)
LIQUIDITY_CLEARED_LEVEL_TOLERANCE_R: float = float(
    _get_str("LIQUIDITY_CLEARED_LEVEL_TOLERANCE_R", "0.15") or "0.15"
)


def get_min_rr(mode: str | None = None, setup_type: str | None = None) -> float:
    """R:R minimo del target operativo finale per una modalità."""
    if mode == "swing":
        return max(4.0, SWING_MIN_RR)
    if mode == "daytrading":
        return max(2.0, DAYTRADING_MIN_RR)
    if setup_type == "counter_trend":
        return 2.0
    return MIN_RR


def get_min_target_rr(mode: str | None = None, setup_type: str | None = None) -> float:
    """R:R minimo anche per TP1; lo swing non accetta un primo target a 1:2."""
    return get_min_rr(mode, setup_type) if mode == "swing" else 0.0

# Fine giornata operativa daytrading in UTC. Le posizioni e i pending
# daytrading vengono chiusi/cancellati oltre questo orario; lo swing resta attivo.
DAYTRADING_CLOSE_HOUR_UTC: int = _get_int("DAYTRADING_CLOSE_HOUR_UTC", 21) or 21
DAYTRADING_CLOSE_MINUTE_UTC: int = _get_int("DAYTRADING_CLOSE_MINUTE_UTC", 45) or 45

# Intervallo (secondi) per il polling dei comandi Telegram (default 5).
TELEGRAM_COMMAND_POLLING_SECONDS: int = _get_int("TELEGRAM_COMMAND_POLLING_SECONDS", 5) or 5

# Simbolo DXY (Dollar Index) sul tuo broker. Vuoto = auto-detect.
# Se il tuo broker non lo ha, lascia vuoto (il bot funziona senza DXY).
DXY_SYMBOL: str = (_get_str("DXY_SYMBOL", "") or "").strip().upper()

# ==========================================================================
# Parametri SL/TP legacy (deprecati: ora si usano le tabelle per modalita')
# ==========================================================================
# Mantenuti per retro-compatibilita' con test e moduli legacy.
# Il nuovo codice usa get_sl_min_pips() / get_sl_max_pips().
MAX_SL_PIPS: int = _get_int("MAX_SL_PIPS", 300) or 300

# ==========================================================================
# FILTRO SPREAD E VOLATILITA' (punto 6: spread eccessivo / volatilità anomala)
# ==========================================================================
# Un setup tecnicamente valido può perdere per spread alto o volatilità
# anomala. Regole applicate da validate_volatility_filter() in smc_signals.py:
#
#   1. Spread massimo in pip per simbolo (sopra -> setup scartato).
#   2. Spread rapportato alla distanza SL: se lo spread è una parte
#      significativa dello SL (default 15%), il trade viene rifiutato.
#   3. Volatilità: SL confrontato con l'ampiezza media del timeframe lento
#      (M15 daytrading, H1 swing) e del timeframe veloce (M5/M15).
#      SL troppo piccolo rispetto alla candela media -> stop da rumore.
#      SL troppo grande rispetto alla candela media -> volatilità insufficiente
#      o movimento già esteso.
#   4. TP: il target più lontano deve essere raggiungibile entro un numero
#      configurabile di range medi della modalità.
#   5. Candela corrente: uno spike anomalo blocca l'entry instabile.
#
# Valori in pip (stesso significato di SL_DAYTRADING/SL_SWING).
MAX_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 2.0,
    "GBPUSD": 2.0,
    "GBPJPY": 3.0,
    "USDJPY": 3.0,
    "XAUUSD": 30.0,
    "_default": 3.0,
}

# Quota massima dello SL assorbibile dallo spread (default 0.15 = 15%).
# Es. SL daytrading EURUSD da 15 pip con spread 2 pip = 13% -> ok.
# Spread 3 pip su SL 15 pip = 20% -> rifiutato.
MAX_SPREAD_TO_SL_RATIO: float = float(
    _get_str("MAX_SPREAD_TO_SL_RATIO", "0.15") or "0.15"
)

# Limiti di volatilita' espressi come rapporto SL / candela media del
# timeframe lento (M15 daytrading, H1 swing).
#   SL_TOO_SMALL_RATIO: SL minore di questo multiplo della candela media
#                       -> stop facilmente colpito dal rumore.
#   SL_TOO_LARGE_RATIO: SL maggiore di questo multiplo della candela media
#                       -> volatilita' troppo bassa per coprire lo SL
#                         oppure movimento gia' esteso.
MIN_SL_TO_AVG_RANGE_RATIO: float = float(
    _get_str("MIN_SL_TO_AVG_RANGE_RATIO", "0.5") or "0.5"
)
MAX_SL_TO_AVG_RANGE_RATIO: float = float(
    _get_str("MAX_SL_TO_AVG_RANGE_RATIO", "5.0") or "5.0"
)

# Numero di candele CHIUSE usate per l'ampiezza media (high-low) del timeframe
# lento: M15 per daytrading, H1 per swing. Il timeframe veloce è M5/M15.
VOLATILITY_BARS: int = _get_int("VOLATILITY_BARS", 20) or 20

# Il TP più lontano non deve essere irrealistico rispetto alla volatilità
# osservata. I limiti sono volutamente diversi: lo swing può attraversare
# più candele, mentre il daytrading deve raggiungere il target nella sessione.
MAX_TP_TO_AVG_RANGE_RATIO_DAYTRADING: float = float(
    _get_str("MAX_TP_TO_AVG_RANGE_RATIO_DAYTRADING", "6.0") or "6.0"
)
MAX_TP_TO_AVG_RANGE_RATIO_SWING: float = float(
    _get_str("MAX_TP_TO_AVG_RANGE_RATIO_SWING", "12.0") or "12.0"
)
# Una candela corrente molto più ampia della propria media segnala entry
# instabile/news spike: non si entra mentre il prezzo sta ancora espandendo.
MAX_CURRENT_RANGE_TO_AVG_RATIO: float = float(
    _get_str("MAX_CURRENT_RANGE_TO_AVG_RATIO", "3.0") or "3.0"
)


def get_max_tp_to_avg_range_ratio(mode: str | None = None) -> float:
    """Massimo rapporto TP/ampiezza media per la modalità operativa."""
    return (
        MAX_TP_TO_AVG_RANGE_RATIO_SWING
        if mode == "swing"
        else MAX_TP_TO_AVG_RANGE_RATIO_DAYTRADING
    )


def get_max_spread_pips(symbol: str) -> float:
    """Spread massimo ammesso in pip per il simbolo."""
    sym_upper = symbol.upper()
    return MAX_SPREAD_PIPS.get(sym_upper, MAX_SPREAD_PIPS["_default"])


# ==========================================================================
# Shallow Pullback — Momentum-Driven Symbols (pag. 30 manuale SMC)
# ==========================================================================
# 'Alcuni strumenti NON ritracciano fino all'Equilibrio. Sono momentum-driven:
#  ritracciano circa il 30%.'
# Per questi simboli, gli OB devono trovarsi nella zona 0-30% (Demand) o
# 70-100% (Supply) del range HH-LL, non nel classico 0-50% / 50-100%.
# Formato: {"SIMBOLO": percentuale} — es. 0.30 = pullback massimo 30%.
# Default: XAUUSD (Gold) è momentum-driven, ritraccia ~30%.
SHALLOW_PD_SYMBOLS: dict[str, float] = {"XAUUSD": 0.30}