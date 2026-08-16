"""
run_master.py
=============
Orchestratore UNIFICATO del bot SMC. Responsabilita':

    1. Inizializza MT5 UNA SOLA VOLTA per tutto il processo.
    2. Avvia webhook server Flask (thread WSGI) per segnali TradingView.
    3. Ogni ~5 secondi esegue analisi SMC autonoma.
    4. Ogni 10 secondi gestisce Break-Even (R:R 1:1) e chiusure parziali.
    5. Invia notifiche Telegram (da config / .env).
    6. NESSUN input manuale: completamente headless.

Modalita' di trading: config.ENABLED_MODES (daytrading/swing).
Determina i range SL, i timeframe di analisi e la gestione intraday.

Flussi operativi:
    A) TradingView -> webhook POST -> trade_validator -> MT5
    B) SMC autonomo -> structure_analyzer -> segnali -> MT5
    C) Loop BE -> secure_runners() -> modifica SL
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time
import webbrowser
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from types import FrameType
from typing import Optional

from mt5_adapter import mt5
import pandas as pd
from werkzeug.serving import make_server

import config
import structure_analyzer as sa
from position_monitor import manage_partial_closes, get_tracker
from smc_signals import (
    classify_reversal, classify_setup_type, generate_sweep_entry,
    find_opposite_liquidity_target, has_h4_liquidity_sweep,
    validate_swing_context, validate_swing_ltf_confirmation,
    validate_daytrading_ltf_confirmation, validate_daytrading_counter_trend,
    validate_liquidity_environment,
    validate_pre_entry_liquidity,
    validate_volatility_filter,
    detect_dxy_conflict,
)
from mt5_engine import (
    MT5ConnectionError,
    OrderExecutionError,
    build_engine_from_config,
)
from risk_manager import calculate_lot_size
from smc_engine import (
    get_current_session, is_near_news_hour, get_dxy_bias,
    get_market_open_status, get_asian_range,
)
from telegram_notifier import (
    TelegramAlertHandler, TelegramCommandListener, build_notifier_from_config,
)
from trade_manager import BreakEvenManager, TradeManagerError, MAGIC_MAIN
from webhook_server import create_app
import utils

# --- Logging: console quasi silenziosa, log completo su file ---
# Il debug completo e' SEMPRE scritto su bot_smc.log e visibile nella
# dashboard (sezione "Log Recenti"). In console compaiono solo i record
# >= CONSOLE_LOG_LEVEL (default CRITICAL = praticamente silenzio).
# Per riattivare i log in console: CONSOLE_LOG_LEVEL=INFO nel .env.

# Log completo su file (tutto, INFO+).
# Path ASSOLUTO rispetto al modulo: identico a quello usato dalla dashboard
# (/api/status, /api/logs/*) e dal report /errors, indipendentemente dalla
# cartella di lavoro da cui viene lanciato il processo.
_BOT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_smc.log")
_file_handler = logging.FileHandler(_BOT_LOG_PATH, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
))

# Console: livello configurabile (default CRITICAL)
try:
    _console_level = logging.getLevelName(config.CONSOLE_LOG_LEVEL.upper())
    if not isinstance(_console_level, int):
        raise ValueError(f"Livello log console non valido: {config.CONSOLE_LOG_LEVEL}")
except (ValueError, AttributeError):
    _console_level = logging.CRITICAL
_console_handler = logging.StreamHandler()
_console_handler.setLevel(_console_level)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
logger = logging.getLogger("run_master")

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------
SMC_SCAN_INTERVAL = config.SMC_SCAN_INTERVAL_SECONDS  # secondi tra scansioni SMC
BE_INTERVAL = config.BREAK_EVEN_INTERVAL_SECONDS  # secondi tra cicli break-even
# Costanti BE rimosse: ora il BE e' dinamico (R:R 1:1 = profitto >= distanza SL).
# Strategia SMC Video 32: "uso sempre la regola del rischio rendimento 1 a 1".
HEARTBEAT_INTERVAL = 1800   # 30 minuti senza segnali -> notifica Telegram
OB_UPDATE_INTERVAL = 900    # 15 minuti tra aggiornamenti OB su Telegram
DAILY_REPORT_HOUR = config.DAILY_REPORT_HOUR  # ora del report giornaliero (es. 23)
# Modalita' di trading attive (multi-mode parallelo)
ENABLED_MODES: tuple[str, ...] = config.ENABLED_MODES
TRADING_MODE: str = config.TRADING_MODE  # retro-compatibilita'
# --- Filtri strategia (dai documenti del corso) ---
# Sessioni tradabili: Londra + NY + Asia (utile per movimenti istituzionali)
# L'Asia ha volatilita' ridotta ma puo' generare setup validi (es. gap weekend)
TRADABLE_SESSIONS = frozenset({"london", "newyork", "asian"})
# News ad alto impatto USD (IPC/inflazione, PIL, NFP, FOMC/tassi, disoccupazione, discorsi Fed)
# Usiamo is_near_news_hour() di smc_engine che controlla le ore chiave UTC.
# Protezione weekend: non piazzare nuovi ordini dal venerdi' sera alla domenica sera
WEEKEND_SKIP_DAYS = frozenset({4, 5, 6})  # 4=ven, 5=sab, 6=dom (Python weekday())
# Sleep mode mercato chiuso: niente scan, BE ridotto, CPU al minimo.
# Il bot controlla ogni N secondi se il mercato ha riaperto e riprende
# automaticamente le scansioni normali.
MARKET_CLOSED_SLEEP_SECONDS = 60  # check riapertura ogni 60s


# ---------------------------------------------------------------------------
# Thread webhook server
# ---------------------------------------------------------------------------

class _WebhookServerThread(threading.Thread):
    """Esegue il server WSGI in un thread con shutdown pulito."""

    def __init__(self, app, host: str, port: int) -> None:
        super().__init__(name="webhook-server", daemon=True)
        self._server = make_server(host, port, app, threaded=True)
        self._ctx = app.app_context()

    def run(self) -> None:
        self._ctx.push()
        logger.info("Webhook server in ascolto su %s:%s", self._server.host, self._server.port)
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()


# ---------------------------------------------------------------------------
# MasterBot UNIFICATO
# ---------------------------------------------------------------------------

class MasterBot:
    """Ciclo di vita completo: webhook + SMC autonomo + break-even."""

    def __init__(self) -> None:
        self._engine = build_engine_from_config()
        self._notifier = build_notifier_from_config()
        # Alert Telegram automatici su ERROR/CRITICAL (throttled anti-spam)
        # Copre: ordini rifiutati, margine insufficiente, errori di scansione.
        self._alert_handler: Optional[logging.Handler] = None
        if (self._notifier and config.TELEGRAM_NOTIFY_ERRORS
                and not config.TELEGRAM_NOTIFY_ONLY_ORDERS):
            try:
                self._alert_handler = TelegramAlertHandler(
                    self._notifier,
                    throttle_min=config.TELEGRAM_ALERT_THROTTLE_MIN,
                )
                logging.getLogger().addHandler(self._alert_handler)
                logger.info("TelegramAlertHandler attivo (alert errori, throttle %d min).",
                            config.TELEGRAM_ALERT_THROTTLE_MIN)
            except Exception as e:
                logger.warning("TelegramAlertHandler non attivato: %s", e)
        self._break_even = BreakEvenManager(
            mt5_module=self._engine._lib(),
            extra_magics=config.ALL_MODE_MAGICS,
        )
        self._symbols: tuple[str, ...] = config.SYMBOLS
        self._server_thread: Optional[_WebhookServerThread] = None
        self._telegram_listener: Optional[TelegramCommandListener] = None
        self._stop = threading.Event()
        # Flag per /status e /positions: il listener li setta, il main loop genera i report
        # (MT5 non e' thread-safe, tutte le chiamate devono avvenire nel main thread)
        self._status_requested = threading.Event()
        self._positions_requested = threading.Event()
        self._last_smc_scan: dict[str, float] = {}
        self._was_market_closed = False  # per loggare transizioni sleep/attivo
        # Tracking posizioni chiuse in-memory (fallback per dashboard se MT5
        # history_deals_get non funziona, es. su conti demo senza storico)
        self._closed_positions_history: list[dict] = []
        self._last_be_time = 0.0
        self._last_signal_time: float = time.time()  # per heartbeat
        self._heartbeat_sent = False
        # Throttle notifiche OB su Telegram
        self._last_ob_update: float = 0.0
        self._last_ob_signature: str = ""  # per rilevare cambiamenti nello status degli OB
        # Throttle log DXY (evita spam ogni 2 secondi)
        self._last_dxy_status: str = ""
        # Throttle log OB dettagliato in console (evita spam, logga solo se
        # i dati OB cambiano o ogni 30 secondi)
        self._last_ob_log_time: float = 0.0
        self._last_ob_log_sig: str = ""
        # Contatori giornalieri per il report delle 23:00
        self._bot_start_dt: datetime = datetime.now()
        self._report_day: date = self._bot_start_dt.date()
        self._scans_today = 0
        self._signals_today = 0
        self._orders_today = 0
        # Se il bot parte dopo l'ora del report, segna il giorno come gia' inviato
        # per evitare un report vuoto immediato al primo loop.
        self._report_sent_for_day: Optional[date] = (
            self._report_day if self._bot_start_dt.hour >= DAILY_REPORT_HOUR else None
        )
        # Snapshot equity per calcolare il P/L del giorno
        self._day_start_equity: Optional[float] = None
        # Tracciamento posizioni per notifiche di apertura/chiusura
        # (inizializzato dopo MT5.connect() con le posizioni gia' attive)
        self._tracked_positions: dict[int, dict] = {}
        # Info ordini pending per re-registrare PartialCloseTracker al fill
        # chiave = order_ticket, valore = {tp1, tp2, tp3, initial_volume, direction, symbol}
        self._pending_order_info: dict[int, dict] = {}
        # Coda thread-safe per ordini provenienti dal webhook Flask.
        # MT5 non e' thread-safe: gli ordini vengono messi in coda dal thread
        # del webhook ed eseguiti nel main thread.
        self._webhook_queue: queue.Queue = queue.Queue(maxsize=50)
        # Throttle log dettagliati per scansione SMC (evita crash da log eccessivo)
        self._last_detail_log: dict[str, float] = {}
        # Debounce log skip ripetuti (es. retail_pullback su GBPUSD ogni scansione)
        self._last_skip_reason: dict[str, tuple[str, float]] = {}
        # Valuta del conto (rilevata dopo MT5.initialize)
        self._account_currency: str = "USD"
        # Stato della sessione verificato dal main thread. Quando è False,
        # ingressi, scansioni e gestione automatica vengono sospesi.
        self._mt5_ready = False
        self._last_mt5_health_check = 0.0
        self._reconnect_attempts = 0
        self._next_reconnect_at = 0.0
        self._mt5_disconnect_alerted = False

    @property
    def is_mt5_ready(self) -> bool:
        """True solo quando l'ultimo health check MT5 è passato."""
        return self._mt5_ready

    def _notify_connection(self, message: str) -> None:
        """Invia un alert di connessione senza interrompere il loop."""
        if not self._notifier:
            return
        try:
            self._notifier(message)
        except Exception as exc:
            logger.warning("Alert Telegram connessione MT5 fallito: %s", exc)

    def _discard_webhook_queue(self) -> int:
        """Scarta ordini ricevuti mentre MT5 non era pronto.

        Un segnale vecchio non deve essere eseguito al ritorno della
        connessione: il prezzo e la struttura potrebbero essere cambiati.
        """
        discarded = 0
        while True:
            try:
                self._webhook_queue.get_nowait()
            except queue.Empty:
                break
            else:
                discarded += 1
                self._webhook_queue.task_done()
        return discarded

    def _resync_mt5_state(self) -> None:
        """Rilegge posizioni e pending dopo una riconnessione riuscita."""
        self._tracked_positions.clear()
        self._init_tracked_positions()
        pending = mt5.orders_get() or []
        live_tickets = {int(getattr(order, "ticket")) for order in pending
                        if getattr(order, "ticket", None) is not None}
        self._pending_order_info = {
            ticket: info for ticket, info in self._pending_order_info.items()
            if ticket in live_tickets
        }
        logger.info(
            "MT5 stato riallineato | posizioni=%d | pending=%d | metadata_pending=%d",
            len(self._tracked_positions), len(pending), len(self._pending_order_info),
        )

    def _mark_mt5_disconnected(self, now: float) -> None:
        """Entra in modalità sicura dopo un health check fallito."""
        if self._mt5_ready:
            logger.error("Connessione MT5 persa: nuovi ingressi e gestione sospesi.")
        self._mt5_ready = False
        self._engine.shutdown()
        discarded = self._discard_webhook_queue()
        self._next_reconnect_at = now + max(
            1, config.MT5_RECONNECT_INITIAL_DELAY_SECONDS
        )
        self._reconnect_attempts = 0
        if not self._mt5_disconnect_alerted:
            suffix = f" Segnali in coda scartati: {discarded}." if discarded else ""
            self._notify_connection(
                "⚠️ Connessione MT5 persa. Nuovi ingressi e gestione automatica "
                f"sospesi; SL/TP già presenti restano al broker.{suffix}"
            )
            self._mt5_disconnect_alerted = True

    def _maintain_mt5_connection(self, now: float) -> bool:
        """Health check e reconnect con exponential backoff nel main thread."""
        interval = max(1, config.MT5_HEALTH_CHECK_INTERVAL_SECONDS)
        if self._mt5_ready and now - self._last_mt5_health_check < interval:
            return True

        if self._mt5_ready:
            self._last_mt5_health_check = now
            if self._engine.health_check(self._symbols):
                return True
            self._mark_mt5_disconnected(now)

        if now < self._next_reconnect_at:
            return False

        try:
            self._engine.shutdown()
            self._engine.initialize()
            self._last_mt5_health_check = now
            if not self._engine.health_check(self._symbols):
                raise MT5ConnectionError("health check fallito dopo initialize()")
            self._resync_mt5_state()
            self._mt5_ready = True
            self._reconnect_attempts = 0
            self._next_reconnect_at = 0.0
            if self._mt5_disconnect_alerted:
                self._notify_connection(
                    "✅ Connessione MT5 ripristinata e stato riallineato. "
                    "Il bot può riprendere le operazioni."
                )
            self._mt5_disconnect_alerted = False
            logger.info("Riconnessione MT5 riuscita.")
            return True
        except Exception as exc:
            self._engine.shutdown()
            delay = min(
                config.MT5_RECONNECT_MAX_DELAY_SECONDS,
                config.MT5_RECONNECT_INITIAL_DELAY_SECONDS
                * (2 ** min(self._reconnect_attempts, 16)),
            )
            self._reconnect_attempts += 1
            self._next_reconnect_at = now + max(1, delay)
            logger.warning(
                "Riconnessione MT5 fallita (tentativo %d): %s. "
                "Prossimo tentativo tra %ss.",
                self._reconnect_attempts, exc, delay,
            )
            return False

    def _can_log_detail(self, symbol: str, interval: Optional[float] = None) -> bool:
        """Throttle per i log non-critici di scansione SMC.

        Ritorna True solo se sono passati >= interval secondi dall'ultimo
        log dettagliato per questo simbolo. I log critici (trade, errori,
        segnali trovati) NON usano questo throttling.

        Default interval = LOG_OB_DEBUG_INTERVAL_SECONDS (60s).
        """
        if interval is None:
            interval = float(getattr(config, "LOG_OB_DEBUG_INTERVAL_SECONDS", 60))
        now = time.time()
        last = self._last_detail_log.get(symbol, 0)
        if now - last >= interval:
            self._last_detail_log[symbol] = now
            return True
        return False

    def _can_skip_log(self, symbol: str, reason: str, interval: float = 60.0) -> bool:
        """Debounce per i log di skip ripetuti (stesso simbolo+motivo).

        Ritorna True la prima volta, poi False per `interval` secondi
        se il motivo non cambia. Se il motivo cambia, logga subito.
        """
        now = time.time()
        prev = self._last_skip_reason.get(symbol)
        if prev is None or prev[0] != reason or (now - prev[1]) >= interval:
            self._last_skip_reason[symbol] = (reason, now)
            return True
        return False

    # -- Avvio --------------------------------------------------------------

    def start(self) -> None:
        logger.info("=" * 60)
        logger.info("AVVIO MASTER BOT UNIFICATO — MULTI-MODE")
        logger.info("Modalita' attive: %s | Simboli: %s | prop_mode=%s | risk=%.1f%%",
                    ", ".join(ENABLED_MODES), ", ".join(self._symbols),
                    config.PROP_MODE, config.RISK_PERCENT)
        for m in ENABLED_MODES:
            _tf = config.get_mode_timeframes(m)
            logger.info("  %-12s → %s+%s%s  [magic=%d]",
                        m, _tf[0], _tf[1],
                        f"+{_tf[2]}" if _tf[2] else "",
                        config.get_mode_magic(m))
        logger.info("BE: R:R 1:1 dinamico (Trailing Stop RIMOSSO)")
        logger.info("=" * 60)

        # Connessione iniziale resiliente: se MT5/bridge sono offline il
        # processo resta vivo e ritenta con lo stesso backoff del reconnect.
        # Webhook e dashboard vengono avviati solo dopo uno stato consistente.
        try:
            while not self._stop.is_set() and not self._maintain_mt5_connection(time.monotonic()):
                time.sleep(1)
        except KeyboardInterrupt:
            self._stop.set()
            self.shutdown()
            return
        if not self._mt5_ready:
            raise MT5ConnectionError(
                "MT5 non disponibile prima dell'arresto: conto, simboli o tick non raggiungibili."
            )

        # Legge la valuta del conto (EUR/USD/altre)
        self._detect_account_currency()

        # Inizializza tracked_positions con le posizioni gia' aperte
        # (evita falsi "POSIZIONE APERTA" al primo ciclo)
        self._init_tracked_positions()

        # Webhook server in thread separato
        app = create_app(engine=self._engine, notifier=self._notifier, webhook_queue=self._webhook_queue, master_bot=self)
        self._server_thread = _WebhookServerThread(
            app, config.WEBHOOK_HOST, config.WEBHOOK_PORT,
        )
        self._server_thread.start()

        # Apre la dashboard nel browser (default attivo, disattivabile via .env)
        self._open_dashboard_in_browser()

        # Telegram command listener (ascolta /status, /help)
        # Il callback setta solo un flag: il report vero viene generato nel main thread
        # perche' la libreria MetaTrader5 NON e' thread-safe.
        try:
            self._telegram_listener = TelegramCommandListener(
                on_status=self._on_status_request,
                on_positions=self._on_positions_request,
                on_errors=self._build_errors_report,
            )
            self._telegram_listener.start()
        except Exception as exc:
            logger.warning("Telegram listener non avviato (forse gia' attivo?): %s", exc)
            self._telegram_listener = None

        # Signal handlers
        self._install_signal_handlers()

        # Loop principale
        self._main_loop()

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Optional[FrameType]) -> None:
            logger.info("Segnale %s ricevuto: arresto...", signum)
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def _open_dashboard_in_browser(self) -> None:
        """Apre la dashboard nel browser predefinito, se abilitato.

        Usa un thread separato con un piccolo delay per dare tempo al server
        webhook di completare l'avvio prima dell'apertura.
        """
        if not config.OPEN_DASHBOARD_ON_START:
            logger.info("Apertura automatica dashboard disabilitata (OPEN_DASHBOARD_ON_START=false).")
            return

        host = "127.0.0.1" if config.WEBHOOK_HOST in ("0.0.0.0", "", None) else config.WEBHOOK_HOST
        url = f"http://{host}:{config.WEBHOOK_PORT}/dashboard"

        def _open() -> None:
            time.sleep(2.0)  # lascia partire il server webhook
            try:
                opened = webbrowser.open(url, new=2)
                if opened:
                    logger.info("Dashboard aperta nel browser: %s", url)
                else:
                    logger.warning("Apertura dashboard fallita silenziosamente: %s (apri manualmente)", url)
            except Exception as e:
                logger.warning("Impossibile aprire la dashboard nel browser: %s", e)

        threading.Thread(target=_open, name="dashboard-browser", daemon=True).start()

    # -- Webhook queue consumer ---------------------------------------------

    def _process_webhook_queue(self) -> None:
        """Esegue nel main thread gli ordini ricevuti dal webhook Flask.

        MetaTrader5 non e' thread-safe: le chiamate devono avvenire nel thread
        in cui e' stato inizializzato. Il webhook si limita ad accodare
        l'ordine validato; questa funzione lo esegue e gestisce notifica
        e PartialCloseTracker.
        """
        if not self._mt5_ready:
            self._discard_webhook_queue()
            return
        while not self._webhook_queue.empty():
            try:
                item = self._webhook_queue.get_nowait()
            except queue.Empty:
                break
            try:
                order = item["order"]
                if (
                    order.get("mode") == "daytrading"
                    and self._is_daytrading_cutoff_reached()
                ):
                    logger.info(
                        "[%s:daytrading] Webhook scartato: oltre l'orario EOD.",
                        order.get("symbol", "?"),
                    )
                    continue
                result = self._engine.place_order(order, plan_key="main")

                # Incrementa contatore ordini (dashboard)
                if result.ok:
                    self._orders_today += 1

                # Registra nel tracker per le chiusure parziali
                if result.ok and result.ticket:
                    tracker = get_tracker()
                    tracker.register(
                        ticket=result.ticket,
                        tp1=item["tp1"],
                        tp2=item["tp2"],
                        tp3=item["tp3"],
                        initial_volume=item["total_lot"],
                        direction=item["direction"],
                    )

                # Notifica Telegram
                if self._notifier:
                    try:
                        msg = (
                            f"🤖 WEBHOOK TRADE\n"
                            f"{order['symbol']}: {order['side'].upper()}\n"
                            f"Entry: {order['entry']} | SL: {order['sl']}\n"
                            f"TP: {order['tp']} | Lotto: {item['total_lot']}\n"
                            f"Ticket: {result.ticket} | Status: {'OK' if result.ok else 'FALLITO'}"
                        )
                        self._notifier(msg)
                    except Exception as e:
                        logger.warning("Errore notifica webhook: %s", e)
            except MT5ConnectionError as e:
                logger.error("Connessione MT5 persa durante l'ordine webhook: %s", e)
                self._mark_mt5_disconnected(time.monotonic())
            except OrderExecutionError as e:
                # Spread, volatilità o livelli possono rifiutare un ordine
                # senza che la connessione sia caduta. Prova un health check
                # immediato e avvia il reconnect solo se fallisce davvero.
                if not self._engine.health_check(self._symbols):
                    logger.error("Connessione MT5 persa durante l'ordine webhook: %s", e)
                    self._mark_mt5_disconnected(time.monotonic())
                else:
                    logger.warning("Ordine webhook rifiutato, MT5 ancora raggiungibile: %s", e)
            except Exception as e:
                logger.exception("Errore esecuzione ordine dalla coda webhook: %s", e)
            finally:
                self._webhook_queue.task_done()

    # -- Loop principale ----------------------------------------------------

    def _is_market_closed(self) -> bool:
        """Ritorna True se il mercato forex e' chiuso (weekend/no sessioni attive).

        Logica: venerdi' dopo le 20:00 UTC fino a lunedi' 00:00 UTC.
        Usa la stessa WEEKEND_SKIP_DAYS della weekend protection.
        """
        now_dt = utils.utc_now()
        wd = now_dt.weekday()
        if wd in WEEKEND_SKIP_DAYS:
            # Venerdi' prima delle 20:00 UTC: ancora tradabile
            if wd == 4 and now_dt.hour < 20:
                return False
            return True  # Sab/Dom o Ven dopo le 20
        return False  # Lun-Gio: mercato aperto

    @staticmethod
    def _is_daytrading_cutoff_reached() -> bool:
        now = utils.utc_now()
        return (now.hour, now.minute) >= (
            config.DAYTRADING_CLOSE_HOUR_UTC,
            config.DAYTRADING_CLOSE_MINUTE_UTC,
        )

    def _close_daytrading_at_eod(self) -> None:
        """Chiude posizioni e cancella pending daytrading oltre l'orario EOD.

        ``ORDER_TIME_DAY`` è una richiesta al broker, non una garanzia uniforme
        tra broker/bridge. Il controllo applicativo rende quindi esplicita la
        regola intraday: viene liquidato solo il magic della modalità
        daytrading; lo swing e gli ordini legacy non classificabili restano
        intatti.
        """
        now = utils.utc_now()
        if not self._is_daytrading_cutoff_reached():
            return
        day_magic = config.get_mode_magic("daytrading")
        # Non usare un marker giornaliero: un ordine arrivato dopo il primo
        # controllo EOD, o una chiusura fallita, deve essere ritentato al ciclo
        # successivo. Gli ordini già riusciti spariscono da MT5 naturalmente.
        day_magics = {day_magic}
        for symbol in self._symbols:
            positions = mt5.positions_get(symbol=symbol) or []
            for pos in positions:
                if int(getattr(pos, "magic", -1)) not in day_magics:
                    continue
                try:
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        raise RuntimeError("tick non disponibile")
                    is_buy = int(pos.type) == int(mt5.POSITION_TYPE_BUY)
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "position": int(pos.ticket),
                        "volume": float(pos.volume),
                        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                        "price": float(tick.bid if is_buy else tick.ask),
                        "deviation": config.ORDER_DEVIATION,
                        "magic": day_magic,
                        "comment": "SMC daytrading EOD close",
                        "type_filling": self._candidate_fillings(symbol)[0],
                    }
                    # --- Negozia il type_filling (evita retcode 10030) ---
                    invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
                    result = None
                    for filling in self._candidate_fillings(symbol):
                        request["type_filling"] = filling
                        result = mt5.order_send(request)
                        if result is not None and result.retcode in (
                            mt5.TRADE_RETCODE_DONE,
                            getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1),
                        ):
                            break
                        if result is not None and result.retcode != invalid_fill:
                            break  # errore diverso: niente retry
                    done_codes = {
                        mt5.TRADE_RETCODE_DONE,
                        getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1),
                    }
                    if result is not None and result.retcode in done_codes:
                        logger.info("[%s:daytrading] EOD: chiusura #%s inviata (retcode=%s).",
                                    symbol, pos.ticket, result.retcode)
                    else:
                        logger.error("[%s:daytrading] EOD: chiusura #%s fallita (retcode=%s).",
                                     symbol, pos.ticket, getattr(result, "retcode", None))
                except Exception as exc:
                    logger.exception("[%s:daytrading] EOD: errore chiusura #%s: %s",
                                     symbol, getattr(pos, "ticket", "?"), exc)

            pending = mt5.orders_get(symbol=symbol) or []
            for order in pending:
                if int(getattr(order, "magic", -1)) not in day_magics:
                    continue
                try:
                    request = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": int(order.ticket),
                        "symbol": symbol,
                        "magic": day_magic,
                        "comment": "SMC daytrading EOD cancel",
                    }
                    result = mt5.order_send(request)
                    if result is not None and result.retcode in (
                        mt5.TRADE_RETCODE_DONE,
                        getattr(mt5, "TRADE_RETCODE_CANCEL", -1),
                    ):
                        logger.info("[%s:daytrading] EOD: pending #%s cancellato.",
                                    symbol, order.ticket)
                    else:
                        logger.error("[%s:daytrading] EOD: cancellazione #%s fallita (retcode=%s).",
                                     symbol, order.ticket, getattr(result, "retcode", None))
                except Exception as exc:
                    logger.exception("[%s:daytrading] EOD: errore cancellazione #%s: %s",
                                     symbol, getattr(order, "ticket", "?"), exc)

    def _main_loop(self) -> None:
        """Loop infinito: SMC scan + BE + comandi Telegram.

        Quando il mercato e' chiuso (weekend):
        - Sospende le scansioni SMC (inutili, tutti i segnali sarebbero bloccati)
        - Riduce il BE a 1 check al minuto (protezione minima posizioni)
        - Dorme 60 secondi tra un ciclo e l'altro per risparmiare CPU
        - Appena il mercato riapre, riprende automaticamente le scansioni normali.
        """
        try:
            while not self._stop.is_set():
                # Health check e reconnect sono eseguiti qui, nel main thread;
                # gli ordini ricevuti dal webhook vengono comunque eseguiti qui.
                if not self._maintain_mt5_connection(time.monotonic()):
                    time.sleep(1)
                    continue

                # --- Esecuzione ordini webhook (solo main thread per MT5) ---
                self._process_webhook_queue()

                # --- Chiusura obbligatoria daytrading a fine giornata UTC ---
                self._close_daytrading_at_eod()

                # --- Rileva stato mercato (weekend = no sessioni attive) ---
                market_closed = self._is_market_closed()
                if market_closed != self._was_market_closed:
                    if market_closed:
                        logger.info(
                            "🛑 MERCATO CHIUSO — sleep mode: scan sospesi, "
                            "BE ridotto, check riapertura ogni %ds",
                            MARKET_CLOSED_SLEEP_SECONDS,
                        )
                    else:
                        logger.info("🟢 MERCATO RIAPERTO — scansioni ripristinate")
                        self._last_smc_scan.clear()  # forza scan immediata
                    self._was_market_closed = market_closed

                now = time.time()

                # --- SMC SCAN (solo mercato aperto) ---
                if not market_closed:
                    for symbol in self._symbols:
                        for mode in ENABLED_MODES:
                            scan_key = f"{symbol}:{mode}"
                            last = self._last_smc_scan.get(scan_key, 0)
                            if now - last >= SMC_SCAN_INTERVAL:
                                try:
                                    self._smc_scan(symbol, mode)
                                except Exception as e:
                                    logger.exception("[%s:%s] Errore SMC scan: %s", symbol, mode, e)
                                self._last_smc_scan[scan_key] = now

                # --- BREAK-EVEN (sempre attivo, ridotto a mercato chiuso) ---
                be_eff = BE_INTERVAL if not market_closed else BE_INTERVAL * 6
                if now - self._last_be_time >= be_eff:
                    for symbol in self._symbols:
                        try:
                            self._secure_symbol(symbol)
                        except Exception as e:
                            logger.warning("[%s] Errore BE: %s", symbol, e)
                    # Monitora anche le chiusure posizioni
                    try:
                        self._monitor_positions()
                    except Exception as e:
                        logger.warning("Errore monitoraggio posizioni: %s", e)
                    self._last_be_time = now

                # --- SLEEP: 1s normale, 60s a mercato chiuso ---
                time.sleep(MARKET_CLOSED_SLEEP_SECONDS if market_closed else 1)

                # --- HEARTBEAT: solo mercato aperto (a mercato chiuso e' spam) ---
                if not market_closed:
                    if (now - self._last_signal_time >= HEARTBEAT_INTERVAL
                            and not self._heartbeat_sent):
                        self._send_heartbeat()
                        self._heartbeat_sent = True

                # --- RIEPILOGO GIORNALIERO alle HH:00 (default 23:00) ---
                self._maybe_send_daily_report()

                # --- COMANDO /status: genera report nel main thread (MT5 non thread-safe) ---
                if self._status_requested.is_set():
                    self._status_requested.clear()
                    try:
                        report = self._generate_status_report()
                        if self._notifier:
                            self._notifier(report)
                    except Exception as e:
                        logger.exception("Errore generazione status report: %s", e)

                # --- COMANDO /positions: genera report posizioni ---
                if self._positions_requested.is_set():
                    self._positions_requested.clear()
                    try:
                        report = self._generate_positions_report()
                        if self._notifier:
                            self._notifier(report)
                    except Exception as e:
                        logger.exception("Errore generazione report posizioni: %s", e)

        except KeyboardInterrupt:
            logger.info("Interruzione da tastiera.")
        finally:
            self.shutdown()

    # -- Break-even ---------------------------------------------------------

    def _secure_symbol(self, symbol: str) -> None:
        """BE via BreakEvenManager + Partial Closes (NO Trailing Stop).

        Strategia SMC (Video 32): BE a R:R 1:1 dinamico.
        Il Trailing Stop e' stato RIMOSSO (non previsto dalla strategia).
        """
        try:
            moved = self._break_even.secure_runners(symbol)
            if moved:
                logger.info("[%s] Break-even ticket: %s", symbol, moved)
                self._notify_break_even(symbol, moved)
        except TradeManagerError as exc:
            logger.error("[%s] Errore Trade Manager: %s", symbol, exc)
        except Exception as exc:
            logger.warning("[%s] Errore BE temporaneo: %s", symbol, exc)

        # Partial closes (30%% TP1, 30%% TP2)
        try:
            pc_res = manage_partial_closes(symbol)
            total_closes = sum(len(v) for v in pc_res.values())
            if total_closes > 0:
                logger.info("[%s] Partial closes: %d totali", symbol, total_closes)
                self._notify_partial_closes(symbol, pc_res)
        except Exception as e:
            logger.warning("[%s] Errore partial close: %s", symbol, e)



    @staticmethod
    def _mode_label(magic: int) -> str:
        """Ritorna l'etichetta della modalita' dato il magic number."""
        _map = {1002: "📊DAYTRADE", 1003: "🏗️SWING"}
        return _map.get(magic, "SMC")

    def _notify_partial_closes(self, symbol: str, pc_res: dict[str, list[dict]]) -> None:
        """Invia notifica Telegram per le chiusure parziali."""
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente chiusure parziali
        if not self._notifier:
            return
        pip = utils.pip_size(symbol)

        for tp_label, closes in pc_res.items():
            for detail in closes:
                try:
                    ticket = detail["ticket"]
                    vol_closed = detail["volume_closed"]
                    close_price = detail["close_price"]
                    pct = detail["pct"]
                    direction = detail["direction"]

                    # Cerca entry price dalla posizione o dal tracker
                    entry = 0.0
                    pos = mt5.positions_get(ticket=ticket)
                    if pos:
                        entry = float(pos[0].price_open)
                    else:
                        # Posizione gia' chiusa completamente, cerca in tracked
                        tracked = self._tracked_positions.get(ticket)
                        if tracked:
                            entry = tracked.get("price_open", 0.0)

                    pips_val = 0.0
                    if entry > 0:
                        pips_val = (close_price - entry) / pip if direction == "BUY" else (entry - close_price) / pip

                    # Determina se e' chiusura totale o parziale
                    emoji = "🔒" if pct == 100 else "📊"
                    label = "CHIUSURA TOTALE" if pct == 100 else f"CHIUSURA PARZIALE {tp_label.upper()}"

                    msg = (
                        f"{emoji} {label}\n"
                        f"{symbol}: {direction}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"Ticket: #{ticket} | {tp_label.upper()}\n"
                        f"Entry: {entry:.2f}\n"
                        f"Exit: {close_price:.2f}\n"
                        f"Chiuso: {pct}% ({vol_closed:.2f} lotti)\n"
                        f"Pips: {pips_val:+.0f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )
                    logger.info(
                        "[%s] Notifica partial close: #%s %s %d%% @ %.5f",
                        symbol, ticket, tp_label, pct, close_price,
                    )
                    self._notifier(msg)
                except Exception as e:
                    logger.warning("Errore notifica partial close: %s", e)

    def _notify_break_even(self, symbol: str, tickets: list[int]) -> None:
        """Invia notifica Telegram quando lo SL viene spostato a break-even."""
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente break-even
        if not self._notifier:
            return
        pip = utils.pip_size(symbol)
        for ticket in tickets:
            try:
                # Cerca i dettagli della posizione nei dati tracciati o live
                info = self._tracked_positions.get(ticket)
                duration_min = 0
                if info is not None:
                    # Posizione tracciata: usa i dati salvati
                    direction = info.get("type", "BUY")
                    entry = float(info.get("price_open", 0))
                    volume = float(info.get("volume", 0))
                    profit = float(info.get("profit", 0))
                    magic_tag = self._mode_label(info.get("magic", 1000))
                    # Calcola pips da info
                    current_price = entry  # fallback
                    if info.get("open_time"):
                        duration_min = int((time.time() - info["open_time"]) / 60)
                    # Prova a prendere il prezzo corrente da MT5
                    try:
                        tick = mt5.symbol_info_tick(info.get("symbol", symbol))
                        if tick:
                            current_price = float(tick.bid if direction == "SELL" else tick.ask)
                    except Exception:
                        pass
                    pips = (current_price - entry) / pip if direction == "BUY" else (entry - current_price) / pip
                else:
                    # Fallback: cerca tra le posizioni aperte su MT5
                    pos = mt5.positions_get(ticket=ticket)
                    if not pos:
                        continue
                    pos = pos[0]
                    direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                    entry = float(pos.price_open)
                    current_price = float(pos.price_current)
                    pips = (current_price - entry) / pip if pos.type == mt5.POSITION_TYPE_BUY else (entry - current_price) / pip
                    volume = float(pos.volume)
                    profit = float(pos.profit)
                    magic_tag = self._mode_label(int(pos.magic))

                msg = (
                    f"🔒 BREAK-EVEN ATTIVATO\n"
                    f"{symbol}: {direction}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Ticket: #{ticket} | {magic_tag}\n"
                    f"Entry: {entry:.2f}\n"
                    f"SL spostato a: {entry:.2f}\n"
                    f"Volume: {volume}\n"
                    f"Pips in profitto: ~{pips:.0f}\n"
                    f"Profitto: ~{self._fmt_eur(profit)}\n"
                    f"Durata: ~{duration_min} min\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Ora il runner e' protetto!"
                )
                logger.info("[%s] 🔒 Break-even #%s: SL spostato a entry (%.2f)",
                            symbol, ticket, entry)
                self._notifier(msg)
            except Exception as e:
                logger.warning("Errore notifica BE #%s: %s", ticket, e)

    # -- Monitoraggio posizioni ---------------------------------------------

    def _init_tracked_positions(self) -> None:
        """Inizializza il tracker con le posizioni gia' aperte su MT5.
        Questo evita falsi "POSIZIONE APERTA" al primo ciclo dopo l'avvio.
        """
        for symbol in self._symbols:
            positions = mt5.positions_get(symbol=symbol)
            if not positions:
                continue
            for pos in positions:
                ticket = int(pos.ticket)
                # Prova a ottenere il timestamp reale di apertura dalla storia MT5
                open_ts = time.time()  # fallback: ora corrente
                try:
                    from_dt, to_dt = utils.mt5_history_window(days_back=30, minutes_ahead=1)
                    deals = mt5.history_deals_get(position=ticket, from_date=from_dt, to=to_dt)
                    if deals:
                        # Il primo deal nella history e' sempre l'apertura (DEAL_ENTRY_IN)
                        open_ts = float(deals[0].time)
                except Exception:
                    pass  # fallback a time.time()

                self._tracked_positions[ticket] = {
                    "symbol": symbol,
                    "type": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
                    "volume": float(pos.volume),
                    "price_open": float(pos.price_open),
                    "sl": float(pos.sl),
                    "tp": float(pos.tp),
                    "magic": int(pos.magic),
                    "profit": float(pos.profit),
                    "swap": float(pos.swap),
                    "open_time": open_ts,
                }
        n = len(self._tracked_positions)
        if n > 0:
            logger.info("Tracker posizioni inizializzato: %d posizioni gia' aperte.", n)

    def _monitor_positions(self) -> None:
        """Confronta le posizioni attuali con quelle tracciate.
        Notifica su Telegram quando una posizione viene aperta o chiusa.
        """
        # Raccogli tutte le posizioni attuali
        current_positions: dict[int, dict] = {}
        for symbol in self._symbols:
            positions = mt5.positions_get(symbol=symbol)
            if not positions:
                continue
            for pos in positions:
                ticket = int(pos.ticket)
                current_positions[ticket] = {
                    "symbol": symbol,
                    "type": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
                    "volume": float(pos.volume),
                    "price_open": float(pos.price_open),
                    "sl": float(pos.sl),
                    "tp": float(pos.tp),
                    "magic": int(pos.magic),
                    "profit": float(pos.profit),
                    "swap": float(pos.swap),
                }

        # Cerca posizioni NUOVE (aperte) — erano in pending ora sono attive
        for ticket, info in current_positions.items():
            if ticket not in self._tracked_positions:
                # Posizione appena aperta: salva timestamp per calcolare durata
                info["open_time"] = time.time()
                self._tracked_positions[ticket] = info
                self._notify_position_opened(ticket, info)
                # Se la posizione viene da un pending limit, re-registra
                # il PartialCloseTracker con i TP originali
                pending_info = self._resolve_pending_fill(ticket, info)
                if pending_info:
                    tracker = get_tracker()
                    tracker.register(
                        ticket=ticket,
                        tp1=pending_info["tp1"],
                        tp2=pending_info["tp2"],
                        tp3=pending_info["tp3"],
                        initial_volume=pending_info["initial_volume"],
                        direction=pending_info["direction"],
                    )
                    logger.info(
                        "[%s] Pending #%s fillato -> posizione #%s registrata nel tracker.",
                        info["symbol"], pending_info.get("order_ticket"), ticket,
                    )

        # Cerca posizioni CHIUSE — erano tracciate ma non ci sono piu'
        closed_tickets = set(self._tracked_positions.keys()) - set(current_positions.keys())
        for ticket in closed_tickets:
            info = self._tracked_positions[ticket]
            self._notify_position_closed(ticket, info)
            # Registra nella history in-memory (fallback per dashboard)
            self._record_closed_position(ticket, info)
            del self._tracked_positions[ticket]

        # Aggiorna profitti delle posizioni ancora aperte
        for ticket, info in current_positions.items():
            if ticket in self._tracked_positions:
                self._tracked_positions[ticket]["profit"] = info["profit"]

    def _resolve_pending_fill(self, position_ticket: int, position_info: dict) -> Optional[dict]:
        """Trova l'ordine pending che ha fillato questa posizione.

        Match per symbol + direction + magic + tempo (order creato < 24h fa).
        Dopo il match, rimuove l'entry da _pending_order_info e pulisce
        gli ordini scaduti (>24h).

        Returns: dict con tp1, tp2, tp3, initial_volume, direction, order_ticket
                 oppure None se nessun pending matcha.
        """
        sym = position_info["symbol"]
        pos_dir = position_info["type"]
        now = time.time()
        best_match: Optional[dict] = None
        best_order_ticket: Optional[int] = None

        for order_ticket, info in list(self._pending_order_info.items()):
            # Pulisci ordini vecchi (>24h)
            if now - info.get("created_at", 0) > 86400:
                del self._pending_order_info[order_ticket]
                continue

            if info["symbol"] == sym and info["direction"].upper() == pos_dir:
                best_match = dict(info)
                best_order_ticket = order_ticket
                break  # primo match: il piu' vecchio e' il piu' probabile

        if best_match and best_order_ticket is not None:
            best_match["order_ticket"] = best_order_ticket
            del self._pending_order_info[best_order_ticket]
            return best_match
        return None

    def _notify_position_opened(self, ticket: int, info: dict) -> None:
        """Invia notifica Telegram quando una posizione viene aperta."""
        try:
            pip = utils.pip_size(info["symbol"])
            magic_tag = self._mode_label(info.get("magic", 1000))
            entry = info["price_open"]
            sl = info["sl"]
            risk_pips = abs(entry - sl) / pip if sl > 0 else 0
            tp = info["tp"]
            reward_pips = abs(tp - entry) / pip if tp > 0 else 0
            rr = round(reward_pips / risk_pips, 1) if risk_pips > 0 else 0

            msg = (
                f"[POSIZIONE APERTA]\n"
                f"{info['symbol']}: {info['type']}\n"
                f"{'-'*30}\n"
                f"Ticket: #{ticket} | {magic_tag}\n"
                f"Entry: {entry:.2f}\n"
                f"Volume: {info['volume']}\n"
                f"SL: {sl:.2f} ({risk_pips:.0f} pip)\n"
                f"TP: {tp:.2f} ({reward_pips:.0f} pip)\n"
                f"R:R: {rr:.1f}\n"
                f"{'-'*30}"
            )
            logger.info(
                "[%s] [TRADE] %s trade eseguito: ticket=%s vol=%.2f entry=%.5f",
                info["symbol"], info["type"], ticket, info["volume"], entry,
            )
            if self._notifier:
                self._notifier(msg)
        except Exception as e:
            logger.warning("Errore notifica apertura #%s: %s", ticket, e)

    def _record_closed_position(self, ticket: int, info: dict) -> None:
        """Registra la chiusura in-memory per la dashboard (fallback se MT5 non ha storico).

        Usa i dati tracciati (last known profit/price). La query MT5 e' gia' fatta
        da _notify_position_closed() per la notifica Telegram.
        """
        import time as _time
        from datetime import datetime as _dt
        try:
            direction = str(info.get("type", "")).upper()
            if direction not in ("BUY", "SELL"):
                direction = "BUY" if info.get("direction") == "buy" else "SELL"
            entry = float(info.get("price_open", 0))
            symbol = str(info.get("symbol", ""))
            exit_price = float(info.get("price_current", entry))
            profit = float(info.get("profit", 0.0))
            pip = utils.pip_size(symbol)
            pips_val = 0.0
            if entry > 0 and pip > 0:
                pips_val = (exit_price - entry) / pip if direction == "BUY" else (entry - exit_price) / pip
            self._closed_positions_history.append({
                "ticket": ticket,
                "symbol": symbol,
                "direction": direction,
                "volume": round(float(info.get("volume", 0)), 2),
                "entry": round(entry, 5),
                "exit": round(exit_price, 5),
                "profit": round(profit, 2),
                "pips": round(pips_val, 2),
                "close_type": "TOTALE",
                "time": _dt.fromtimestamp(_time.time()).strftime("%d/%m %H:%M"),
                "timestamp": int(_time.time()),
            })
            # Mantieni max 100 record
            if len(self._closed_positions_history) > 100:
                self._closed_positions_history = self._closed_positions_history[-100:]
            logger.debug("[HISTORY] Posizione #%s registrata: %s profit=%.2f",
                         ticket, direction, profit)
        except Exception as e:
            logger.debug("[HISTORY] Errore registrazione chiusura #%s: %s", ticket, e)

    def _notify_position_closed(self, ticket: int, info: dict) -> None:
        """Notifica su Telegram la chiusura di una posizione con profitto."""
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente posizioni chiuse
        try:
            from datetime import timedelta
            from_dt, to_dt = utils.mt5_history_window(hours_back=24, minutes_ahead=1)

            # Cerca i deal di questa posizione nella storia
            deals = mt5.history_deals_get(position=ticket, from_date=from_dt, to=to_dt)
            profit = 0.0
            exit_price = info["price_open"]
            commission = 0.0
            swap = info.get("swap", 0.0)

            if deals:
                for deal in deals:
                    if deal.profit != 0.0:
                        profit = float(deal.profit)
                        exit_price = float(deal.price)
                        commission = float(getattr(deal, "commission", 0))
                        break

            # Fallback: usa profitto dall'ultimo aggiornamento
            if profit == 0.0:
                profit = info.get("profit", 0.0)

            pip = utils.pip_size(info["symbol"])
            entry = info["price_open"]
            pips_profit = (exit_price - entry) / pip if info["type"] == "BUY" else (entry - exit_price) / pip

            magic_tag = self._mode_label(info.get("magic", 1000))

            # Durata reale: dal momento in cui e' stata tracciata
            open_time = info.get("open_time", time.time())
            duration_min = int((time.time() - open_time) / 60)

            segno = "+" if profit >= 0 else "-"
            status = "PROFIT" if profit >= 0 else "LOSS"
            msg = (
                f"[{status}] POSIZIONE CHIUSA\n"
                f"{info['symbol']}: {info['type']}\n"
                f"{'-'*30}\n"
                f"Ticket: #{ticket} | {magic_tag}\n"
                f"Entry: {entry:.2f}\n"
                f"Exit: {exit_price:.2f}\n"
                f"Volume: {info['volume']}\n"
                f"Pips: {segno}{pips_profit:.0f}\n"
                f"Profitto: {self._fmt_eur(profit)}\n"
                f"Commissioni: {self._fmt_eur(commission)}\n"
                f"Swap: {self._fmt_eur(swap)}\n"
                f"Durata: ~{duration_min} min\n"
                f"{'-'*30}"
            )
            logger.info("[%s] [CLOSE] #%s: %.2f EUR (%+.0f pip)",
                        info["symbol"], ticket, profit, pips_profit)
            if self._notifier:
                self._notifier(msg)
        except Exception as e:
            logger.warning("Errore notifica chiusura #%s: %s", ticket, e)

    # -- Notifiche Telegram: nuove tipologie --------------------------------

    def _notify_pending_limit(self, symbol: str, direction: str, entry: float,
                              distance_pips: float) -> None:
        """Notifica quando il bot piazza un pending limit (OB lontano dal mercato)."""
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente pre-notifiche pending (c'e' gia' il [TRADE])
        if not self._notifier or not config.TELEGRAM_NOTIFY_PENDING:
            return
        msg = (
            f"📌 PENDING LIMIT\n"
            f"{symbol}: {direction.upper()} @ {entry:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"OB distante {distance_pips:.0f} pip dal mercato:\n"
            f"attendo il ritorno del prezzo al livello OB."
        )
        try:
            self._notifier(msg)
        except Exception as e:
            logger.warning("Notifica pending limit fallita: %s", e)

    def notify_logs_cleared(self) -> None:
        """Notifica Telegram quando la dashboard svuota il file dei log."""
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente log svuotati
        if not self._notifier or not config.TELEGRAM_NOTIFY_ERRORS:
            return
        try:
            self._notifier("🗑️ LOG SVUOTATI\nIl file dei log è stato azzerato dalla dashboard.")
        except Exception as e:
            logger.warning("Notifica 'log svuotati' fallita: %s", e)

    def _build_errors_report(self) -> str:
        """Report degli ultimi errori dal file di log (comando Telegram /errors)."""
        try:
            log_path = os.path.join(os.path.dirname(__file__), "bot_smc.log")
            if not os.path.exists(log_path):
                return "Nessun file di log trovato."
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = [line.rstrip() for line in f.readlines()]
            err = [l for l in lines if utils.is_error_log_line(l)]
            if not err:
                return "✅ Nessun errore nel file di log."
            last = err[-15:]
            # Tronca le righe (limite messaggi Telegram ~4096 caratteri)
            body = "\n".join(f"• {l[:220]}" for l in last)
            if len(body) > 3800:
                body = body[:3800] + "…"
            return (
                f"❌ ERRORI NEL LOG ({len(err)} totali)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{body}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Ultimi {len(last)} — dettaglio completo nella dashboard."
            )
        except Exception as e:
            return f"Errore lettura log: {e}"

    # -- SMC Scan autonomo --------------------------------------------------

    def _smc_scan(self, symbol: str, mode: str) -> None:
        """Pipeline SMC per una modalita' specifica.

        - Daytrading: D1 → H4 (direzione) → M15 (struttura) → M5 (POI) → M1 (conferma)
        - Swing:      D1 → H4 (direzione) → H1 (entry) → M15 (conferma)
        """
        if mode == "daytrading" and self._is_daytrading_cutoff_reached():
            logger.info("[%s:daytrading] [SKIP] Oltre l'orario EOD.", symbol)
            return

        # === TIMEFRAME DINAMICI (dalla modalita') ===
        htf, mtf, ltf = config.get_mode_timeframes_mt5(mode)
        htf_label, mtf_label, ltf_label = config.get_mode_timeframes(mode)
        mode_magic = config.get_mode_magic(mode)

        # Check posizioni ESISTENTI per questa modalita' (stesso magic)
        positions = mt5.positions_get(symbol=symbol)
        pending = mt5.orders_get(symbol=symbol)
        mode_positions = [p for p in (positions or []) if int(p.magic) == mode_magic]
        mode_pending = [p for p in (pending or []) if int(p.magic) == mode_magic]
        n_pos = len(mode_positions)
        n_pend = len(mode_pending)
        if n_pos > 0 or n_pend > 0:
            if self._can_log_detail(symbol):
                logger.info("[%s:%s] ⏸️ Già attivi: %d posizioni, %d ordini pending — salto scan.",
                            symbol, mode, n_pos, n_pend)
            return

        _detail = self._can_log_detail(symbol)  # throttle per log non-critici
        if _detail:
            logger.info("[%s:%s] 🔍 SMC scan avviato...", symbol, mode)
        self._scans_today += 1

        # === FILTRI STRATEGIA (condivisi tra modalita') ===
        session = get_current_session()
        can_place_orders = session in TRADABLE_SESSIONS
        if not can_place_orders and _detail:
            logger.info("[%s] [INFO] Sessione '%s': rischio ridotto per bassa volatilità.",
                        symbol, session)

        now_dt = utils.utc_now()
        today_weekday = now_dt.weekday()
        weekend_blocked = False
        if today_weekday in WEEKEND_SKIP_DAYS:
            if today_weekday == 4 and now_dt.hour < 20:
                pass
            else:
                weekend_blocked = True
                logger.info("[%s] 🚫 Weekend protection.", symbol)

        near_news = is_near_news_hour()
        market_open = get_market_open_status()
        if near_news and _detail:
            logger.info("[%s] 📰 News USD: riduco rischio.", symbol)
        if market_open["in_open_window"] and _detail:
            logger.info("[%s] 🔔 Apertura %s: finestra istituzionale (%d min rimanenti).",
                        symbol, market_open["market"], market_open["minutes_left"])

        dxy = get_dxy_bias()
        if dxy and dxy.get("bias"):
            if self._last_dxy_status != dxy["bias"]:
                logger.info("[%s] [DXY] trend=%s | bias=%s | prezzo=%.2f",
                            symbol, dxy["trend"], dxy["bias"], dxy.get("current_price", 0))
                self._last_dxy_status = dxy["bias"]
        else:
            if self._last_dxy_status != "unavailable":
                logger.info("[%s] [DXY] Non disponibile.", symbol)
                self._last_dxy_status = "unavailable"

        # ================================================================
        # FASE 1: HTF (direzione) — Trend + Liquidity Zones + PD Range
        # ================================================================
        # Shallow pullback per simboli momentum-driven (XAUUSD: ritraccia ~30%)
        shallow_pct = config.SHALLOW_PD_SYMBOLS.get(symbol, None)
        htf_bars = 200
        # Filtro macro obbligatorio: il daytrading segue D1 -> H4 -> M15 -> M5 -> M1.
        # Lo swing usa lo stesso contesto macro prima di H4 -> H1 -> M15.
        macro = sa.analyze_symbol(symbol, mt5.TIMEFRAME_D1, bars=250, pivot_window=4,
                                  mode=mode, shallow_pd_pct=shallow_pct)
        if not macro["success"] or macro.get("trend") not in ("bullish", "bearish"):
            logger.info("[%s:%s] [SKIP] Contesto D1 non direzionale o non disponibile.", symbol, mode)
            return
        h4 = sa.analyze_symbol(symbol, htf, bars=htf_bars, pivot_window=3,
                                mode=mode, shallow_pd_pct=shallow_pct)
        if not h4["success"]:
            logger.warning("[%s] %s fallita: %s", symbol, htf_label, h4.get("error"))
            return

        trend = h4["trend"]
        macro_trend = macro["trend"]
        if trend not in ("bullish", "bearish") or trend != macro_trend:
            logger.info("[%s:%s] [SKIP] Trend D1/H4 non allineato (%s/%s).",
                        symbol, mode, macro_trend, trend)
            return
        current_price = h4.get("current_price", 0)
        if _detail:
            logger.info("[%s] %s: trend=%s | swings=%s | prezzo=%.2f",
                        symbol, htf_label, trend, h4["swings_count"], current_price)

        if trend == "sideways":
            logger.info("[%s] [SKIP] Trend laterale %s: nessuna operazione.", symbol, htf_label)
            return

        # --- Analisi liquidita' HTF (BSL/SSL) + Fibonacci PD Range ---
        # Strategia SMC del corso: segnare TUTTE le liquidita' sopra i massimi e
        # sotto i minimi. Il prezzo va a cercarle come una calamita. Il Fib (0/0.5/1)
        # serve per il range Premium/Discount, non per i POI di entrata.
        h4_df = sa.get_market_data(symbol, htf, bars=htf_bars)
        h4_swings = pd.DataFrame()  # inizializzato per SL strutturale
        liq_zones_h4 = pd.DataFrame()  # per TP da liquidita'
        poi_info = ""
        eq = None  # equilibrio HTF per PD matrix MTF (strategia: HTF prevale su MTF)
        poi: dict = {}  # inizializzato per evitare NameError se h4_df e' None
        if h4_df is not None and len(h4_df) >= 20:
            try:
                h4_df = sa.identify_swings(h4_df, window=3)
                h4_swings = sa.filter_alternating_swings(h4_df)
                if not h4_swings.empty:
                    h4_swings = sa.label_structure(h4_swings)

                    # Calcola Fibonacci PD Range (HH-LL, livelli 0 / 0.5 / 1)
                    poi = sa.find_h4_poi(h4_swings, current_price)
                    if poi["hh"] > 0 and poi["ll"] > 0:
                        eq = (poi["hh"] + poi["ll"]) / 2
                        zone = "Premium" if current_price > eq else "Discount"
                        if _detail:
                            logger.info(
                                "[%s] [RANGE] %s: HH=%.2f LL=%.2f | Eq=%.2f | Zona=%s",
                                symbol, htf_label, poi["hh"], poi["ll"], eq, zone,
                            )
                        poi_info = f" | Range {htf_label}: {zone}"

                    # Trova zone di liquidita' HTF (BSL sopra massimi, SSL sotto minimi)
                    liq_zones_h4 = sa.find_liquidity_zones(h4_df, h4_swings)
                    if not liq_zones_h4.empty:
                        nearest_liq = None
                        nearest_dist = float("inf")
                        for _, lz in liq_zones_h4.iterrows():
                            dist = abs(current_price - float(lz["price_level"]))
                            if dist < nearest_dist:
                                nearest_dist = dist
                                nearest_liq = (lz["type"], float(lz["price_level"]))
                        if nearest_liq:
                            dist_pct = (nearest_dist / abs(poi["hh"] - poi["ll"]) * 100) if poi["hh"] > poi["ll"] else 100
                            if _detail:
                                logger.info(
                                    "[%s] [LIQ] %s piu' vicina: %s @ %.2f (dist=%.1f%% del range)",
                                    symbol, htf_label, nearest_liq[0], nearest_liq[1], dist_pct,
                                )
            except Exception as e:
                logger.debug("[%s] Analisi liquidita %s non disponibile: %s", symbol, htf_label, e)

        # ================================================================
        # CHECK SWEEP: gate obbligatorio per lo swing (video 17/22/26).
        # La sequenza non può proseguire senza liquidità presa e inversione
        # istituzionale: niente fallback HTF o ingresso diretto su sweep.
        # ================================================================
        sweep_check = {"swept": False, "type": None, "price": 0.0, "bars_ago": 0}
        reversal: dict = {}
        if not h4_swings.empty and h4_df is not None:
            sweep_check = has_h4_liquidity_sweep(h4_df, h4_swings, current_price)
            # Il gate di reclaim deve convertire la penetrazione in pip con la
            # convenzione corretta anche per JPY/metalli e simboli broker.
            sweep_check["symbol"] = symbol
            if sweep_check["swept"]:
                reversal = classify_reversal(
                    h4_df, h4_swings, sweep_check["price"],
                    "buy" if sweep_check["type"] == "SSL" else "sell",
                )
                if _detail:
                    logger.info("[%s] [SWEEP] %s: %s @ %.2f (%d barre fa) | inv: %s (%d%%)",
                                symbol, htf_label, sweep_check["type"], sweep_check["price"],
                                sweep_check["bars_ago"], reversal.get("type"), reversal.get("confidence", 0))
            else:
                if _detail:
                    logger.info("[%s] [SKIP] Nessun sweep %s recente.", symbol, htf_label)

        if mode == "swing":
            swing_context_ok, swing_context_reason = validate_swing_context(
                htf_ready=bool(h4_df is not None and not h4_swings.empty),
                htf_trend=trend,
                sweep_check=sweep_check,
                reversal=reversal,
            )
            if not swing_context_ok:
                if self._can_skip_log(symbol, f"swing_context:{swing_context_reason}"):
                    logger.info("[%s:swing] [SKIP] %s", symbol, swing_context_reason)
                return

        # ================================================================
        # FASE 2: MTF (entry) — BOS/CHOCH + Order Block
        # ================================================================
        # Strategia SMC: 'Il range HTF prevale sempre sul MTF.'
        # Passiamo l'equilibrio HTF alla matrice PD di MTF cosi' gli OB vengono
        # filtrati contro il range HTF, non contro il range MTF (piu' stretto).
        ob_lookback = 7
        m15 = sa.analyze_symbol(
            symbol, mtf, bars=200, pivot_window=4,
            h4_equilibrium=eq, ob_lookback=ob_lookback,
            shallow_pd_pct=shallow_pct,
            pd_range_high=poi.get("hh") if poi.get("hh", 0) > 0 else None,
            pd_range_low=poi.get("ll") if poi.get("ll", 0) > 0 else None,
            mode=mode,
        )
        if not m15["success"]:
            logger.warning("[%s] %s fallita: %s", symbol, mtf_label, m15.get("error"))
            return

        if _detail:
            logger.info("[%s] %s: trend=%s | swings=%s | OB_validi=%s | segnali=%s",
                        symbol, mtf_label, m15["trend"], m15["swings_count"], m15["obs_count"],
                    len(m15.get("signals", [])))

        # M15 è la struttura intraday; il daytrading richiede inoltre
        # un POI/entry valido sul M5. Nessun fallback da solo H4/M15.
        entry_analysis = m15
        entry_label = mtf_label
        m5 = None
        if mode == "daytrading":
            m5 = sa.analyze_symbol(
                symbol, mt5.TIMEFRAME_M5, bars=200, pivot_window=4,
                h4_equilibrium=eq, ob_lookback=7,
                shallow_pd_pct=shallow_pct,
                pd_range_high=poi.get("hh") if poi.get("hh", 0) > 0 else None,
                pd_range_low=poi.get("ll") if poi.get("ll", 0) > 0 else None,
                mode=mode,
            )
            if not m5["success"] or m5.get("trend") != trend:
                logger.info("[%s:daytrading] [SKIP] M5 non disponibile o non allineato a M15.", symbol)
                self._maybe_send_ob_update(symbol, h4, m15, dxy)
                return
            # Il POI M5 deve restare vicino al POI strutturale M15:
            # altrimenti il segnale sta inseguendo un movimento già esteso.
            m15_signals = m15.get("signals", [])
            m15_entries = {
                sig.get("direction"): float(sig.get("entry", 0.0))
                for sig in m15_signals
                if sig.get("direction") and sig.get("entry")
            }
            poi_max_distance = 25.0
            m5_near_poi = []
            for candidate in m5.get("signals", []):
                reference_entry = m15_entries.get(candidate.get("direction"))
                if reference_entry is None:
                    continue
                distance = abs(float(candidate["entry"]) - reference_entry) / utils.pip_size(symbol)
                if distance <= poi_max_distance:
                    m5_near_poi.append(candidate)
                else:
                    logger.info(
                        "[%s:daytrading] [REJECTED-SETUP] M5 entry distante %.1f pip dal POI M15 (massimo %.1f).",
                        symbol, distance, poi_max_distance,
                    )
            m5["signals"] = m5_near_poi
            entry_analysis = m5
            entry_label = "M5"

        if not entry_analysis.get("signals"):
            obs = entry_analysis.get("ob_potentials", [])
            if _detail:
                logger.info("[%s] [NO SIGNAL] Nessun segnale/POI valido su %s (OB=%d).",
                            symbol, entry_label, len(obs))
            self._maybe_send_ob_update(symbol, h4, m15, dxy)
            return

        self._last_signal_time = time.time()
        self._heartbeat_sent = False
        signals = entry_analysis["signals"]
        from_sweep = False

        # ================================================================
        # FASE 2.5: LTF — Conferma finale allineamento entrata
        # La conferma M1 è obbligatoria per il daytrading e M15 per lo swing.
        # Lo sweep HTF non sostituisce la conferma del timeframe operativo.
        # ================================================================
        pip = utils.pip_size(symbol)

        # ================================================================
        # FILTRO SPREAD E VOLATILITA' (punto 6): calcolati UNA volta per
        # scansione, applicati a ogni segnale prima dell'ordine.
        # Spread: ask-bid live. Volatilità: ampiezza media M15 (high-low).
        # ================================================================
        _spread_pips = 0.0
        _avg_range_pips = 0.0       # timeframe lento: M15/H1
        _fast_avg_range_pips = 0.0  # timeframe veloce: M5/M15
        _current_range_pips = 0.0   # ultima candela del timeframe veloce
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is not None and float(tick.ask) > 0 and float(tick.bid) > 0:
                _spread_pips = (float(tick.ask) - float(tick.bid)) / pip
        except Exception:
            _spread_pips = 0.0
        try:
            _vol_bars = int(config.VOLATILITY_BARS or 20)
            # Daytrading: M15 come contesto + M5 per la stabilità dell'entry.
            # Swing: H1 come contesto + M15 per la stabilità dell'entry.
            _slow_tf, _fast_tf = {
                "daytrading": (mt5.TIMEFRAME_M15, mt5.TIMEFRAME_M5),
                "swing": (mt5.TIMEFRAME_H1, mt5.TIMEFRAME_M15),
            }.get(mode, (mt5.TIMEFRAME_M15, mt5.TIMEFRAME_M5))
            _slow_df = sa.get_market_data(symbol, _slow_tf, bars=_vol_bars + 1)
            _fast_df = sa.get_market_data(symbol, _fast_tf, bars=_vol_bars + 1)
            if _slow_df is not None and len(_slow_df) >= 4:
                # copy_rates_from_pos include spesso la candela in formazione:
                # escluderla da media e ultima candela di riferimento.
                _slow_closed = (_slow_df["high"] - _slow_df["low"]).iloc[:-1]
                _avg_range_pips = float(_slow_closed.tail(_vol_bars).mean()) / pip
            if _fast_df is not None and len(_fast_df) >= 4:
                _fast_closed = (_fast_df["high"] - _fast_df["low"]).iloc[:-1]
                # L'ultima candela chiusa viene controllata separatamente:
                # non deve attenuare la propria anomalia entrando nella media
                # di riferimento (nessun look-ahead e baseline indipendente).
                _fast_baseline = _fast_closed.iloc[:-1].tail(_vol_bars)
                if len(_fast_baseline) >= 2:
                    _fast_avg_range_pips = float(_fast_baseline.mean()) / pip
                    _current_range_pips = float(_fast_closed.iloc[-1]) / pip
        except Exception:
            _avg_range_pips = 0.0
            _fast_avg_range_pips = 0.0
            _current_range_pips = 0.0

        if ltf is not None:
            m1 = sa.analyze_symbol(
                symbol, ltf, bars=100, pivot_window=5,
                shallow_pd_pct=shallow_pct,
                mode=mode,
            )
            m1_confirmed: list[dict] = []
            m1_skipped = 0

            if m1["success"] and m1["obs_count"] > 0:
                m1_potentials = m1.get("ob_potentials", [])
                if _detail:
                    logger.info(
                        "[%s] %s: trend=%s | OB_validi=%s | segnali=%s",
                        symbol, ltf_label, m1["trend"], m1["obs_count"],
                        len(m1.get("signals", [])),
                    )

                for sig in signals:
                    sig_dir = sig["direction"]
                    sig_entry = sig["entry"]
                    confirmed = False
                    best_m1_entry = 0.0
                    best_m1_dist = float("inf")

                    for ob in m1_potentials:
                        if ob["direction"] != sig_dir:
                            continue
                        if ob["status"] != "ready":
                            continue
                        dist = abs(ob["entry"] - sig_entry) / pip
                        if dist < best_m1_dist:
                            best_m1_dist = dist
                            best_m1_entry = ob["entry"]
                            max_confirmation_distance = (
                                config.SWING_CONFIRMATION_MAX_DISTANCE_PIPS
                                if mode == "swing" else 25
                            )
                            if dist <= max_confirmation_distance:
                                confirmed = True

                    if confirmed:
                        m1_confirmed.append(sig)
                        if _detail:
                            logger.info(
                                "[%s]   [%s ✅] %s confermato: entry %s=%.2f (dist=%.0f pip da %s=%.2f)",
                                symbol, ltf_label, sig_dir.upper(), ltf_label,
                                best_m1_entry, best_m1_dist, mtf_label, sig_entry,
                            )
                    else:
                        m1_skipped += 1
                        logger.info(
                            "[%s]   [%s ❌] %s SCARTATO: nessun OB %s valido vicino a entry=%.2f (miglior dist=%.0f pip)",
                            symbol, ltf_label, sig_dir.upper(), ltf_label, sig_entry, best_m1_dist,
                        )
            else:
                if mode == "swing":
                    if _detail:
                        logger.info(
                            "[%s:swing] [SKIP] %s non disponibile o senza POI/OB.",
                            symbol, ltf_label,
                        )
                    m1_confirmed = []
                else:
                    if _detail:
                        logger.info(
                            "[%s:%s] [SKIP] %s non disponibile o senza POI/OB.",
                            symbol, mode, ltf_label,
                        )
                    m1_confirmed = []

            if mode == "swing":
                # Un POI LTF deve anche produrre TC/MSS nella direzione del
                # setup; la sola vicinanza geometrica non basta.
                required_events = {
                    "buy": ({"TC_bullish"}, {"MSS_bullish", "SB_bullish"}),
                    "sell": ({"TC_bearish"}, {"MSS_bearish", "SB_bearish"}),
                }
                ltf_events = set(m1.get("structure_events", []))
                structural_confirmations = sum(
                    1 for ltf_sig in m1.get("signals", [])
                    if ltf_sig.get("direction") in {sig.get("direction") for sig in signals}
                    and (
                        required_events.get(ltf_sig.get("direction"), (set(), set()))[0] & ltf_events
                        or required_events.get(ltf_sig.get("direction"), (set(), set()))[1].issubset(ltf_events)
                    )
                )
                ltf_ok, ltf_reason = validate_swing_ltf_confirmation(
                    ltf_ready=bool(m1.get("success")),
                    ltf_obs_count=int(m1.get("obs_count", 0)),
                    confirmed_count=len(m1_confirmed),
                    structure_confirmed_count=structural_confirmations,
                )
                if not ltf_ok:
                    if _detail:
                        logger.info("[%s:swing] [SKIP] %s", symbol, ltf_reason)
                    self._maybe_send_ob_update(symbol, h4, m15, dxy)
                    return

            if mode == "daytrading":
                # La conferma daytrade richiede rottura strutturale reale su
                # entrambi i timeframe operativi, non la sola concordanza trend.
                structural_confirmed: list[dict] = []
                for candidate in m1_confirmed:
                    ok, reason = validate_daytrading_ltf_confirmation(
                        direction=candidate.get("direction", ""),
                        m5_ready=bool(m5 and m5.get("success")),
                        m5_events=m5.get("structure_events", []) if m5 else [],
                        m1_ready=bool(m1.get("success")),
                        m1_events=m1.get("structure_events", []) if m1 else [],
                    )
                    if ok:
                        structural_confirmed.append(candidate)
                    elif _detail:
                        logger.info("[%s:daytrading] [REJECTED-SETUP] %s", symbol, reason)
                m1_confirmed = structural_confirmed
                if not m1_confirmed:
                    self._maybe_send_ob_update(symbol, h4, m15, dxy)
                    return

            signals = m1_confirmed
            if m1_skipped > 0:
                logger.info(
                    "[%s] [%s FILTER] %d segnali confermati, %d scartati da %s",
                    symbol, ltf_label, len(signals), m1_skipped, ltf_label,
                )

            if not signals:
                if _detail:
                    logger.info("[%s] [NO SIGNAL] Tutti i segnali %s scartati da %s.",
                                symbol, mtf_label, ltf_label)
                self._maybe_send_ob_update(symbol, h4, m15, dxy)
                return

        # ================================================================
        # FASE 3: Esecuzione segnali
        # ================================================================
        if weekend_blocked:
            logger.info("[%s] 🚫 Weekend: segnali trovati ma ordini bloccati.", symbol)
            self._signals_today += len(signals)
            for i, sig in enumerate(signals, 1):
                msg = (
                    f"[SETUP] ATTESA\n"
                    f"{symbol}: {sig['direction'].upper()}\n"
                    f"Entry: {sig['entry']:.2f} | SL: {sig['sl']:.2f}\n"
                    f"TP1: {sig['tp1']:.2f} | RR: {sig['rr']:.1f}\n"
                    f"Trend {htf_label}: {trend} | Sessione: {session}\n"
                    f"[WARN] Ordine non piazzato (weekend protection)"
                )
                if self._notifier and not config.TELEGRAM_NOTIFY_ONLY_ORDERS:
                    try:
                        self._notifier(msg)
                    except Exception:
                        pass
            return

        if not can_place_orders:
            logger.info("[%s] ⏸️ Sessione '%s': piazzo pending limit.", symbol, session)

        logger.info("[%s:%s] [TRADE] TROVATI %s SEGNALI %s:", symbol, mode, len(signals), mtf_label)
        self._signals_today += len(signals)

        for i, sig in enumerate(signals, 1):
            direction = sig["direction"]
            entry = sig["entry"]
            sl_m15 = sig["sl"]  # M15 micro-SL (riferimento — verra' sostituito)
            tp1 = sig["tp1"]
            tp2 = sig["tp2"]

            # ============================================================
            # SL DINAMICO: basato sulla modalità daytrading o swing.
            # Usa le tabelle SL_DAYTRADING / SL_SWING da config.py
            # ============================================================
            pip = utils.pip_size(symbol)
            min_sl_pips = config.get_sl_min_pips(symbol, mode)
            max_sl = config.get_sl_max_pips(symbol, mode)
            min_sl_m1 = min_sl_pips  # floor M1 uguale al minimo della modalita'

            # Se non c'e' sessione in corso, riduci rischio
            session = get_current_session()
            if session not in TRADABLE_SESSIONS:
                logger.info("[%s] [INFO] Sessione '%s': rischio ridotto.", symbol, session)

            if not h4_swings.empty:
                sl = sa.find_h4_structural_sl(h4_swings, entry, direction, min_sl_pips, max_sl, pip)
                sl_pips = abs(entry - sl) / pip
                sl_m15_pips = abs(entry - sl_m15) / pip
                logger.info("[%s]   [%d] SL %s: %.2f (%.0f pip) | %s era: %.2f (%.1f pip %s)",
                            symbol, i, htf_label, sl, sl_pips, mtf_label, sl_m15, sl_m15_pips, mtf_label)
            else:
                sl = entry - min_sl_pips * pip if direction == "buy" else entry + min_sl_pips * pip
                logger.info("[%s]   [%d] SL fallback (no %s swings): %.2f (%.0f pip)",
                            symbol, i, htf_label, sl, min_sl_pips)

            # Ricalcola TP: prima prova con liquidita' H4, fallback 3x/5x
            risk_price = abs(entry - sl)
            h4_tp1, h4_tp2, h4_tp3 = find_opposite_liquidity_target(liq_zones_h4, entry, direction)
            if mode == "swing":
                # Scarta le liquidità sotto 4R: nello swing anche TP1
                # deve essere una zona opposta reale, non un fallback matematico.
                swing_min_rr = config.get_min_rr("swing", sig.get("setup_type"))
                swing_targets = [target for target in (h4_tp1, h4_tp2, h4_tp3)
                                 if target and ((target - entry) / risk_price >= swing_min_rr
                                                if direction == "buy" else
                                                (entry - target) / risk_price >= swing_min_rr)]
                swing_targets = sorted(swing_targets, reverse=direction == "sell")
                if not swing_targets:
                    logger.info("[%s:swing] [REJECTED-SETUP] nessuna liquidità opposta reale raggiungibile a 4R.", symbol)
                    continue
                h4_tp1 = swing_targets[0]
                h4_tp2 = swing_targets[1] if len(swing_targets) > 1 else 0.0
                h4_tp3 = swing_targets[2] if len(swing_targets) > 2 else None

            if h4_tp1 > 0:
                tp1 = h4_tp1
                # TP2 deve sempre restare oltre TP1. Se la seconda zona H4
                # manca o è troppo vicina, estendila almeno di un rischio
                # oltre TP1 (senza imporre alcun tetto al TP).
                tp2_distance = max(risk_price * 5.0, abs(tp1 - entry) + risk_price)
                fallback_tp2 = entry + tp2_distance if direction == "buy" else entry - tp2_distance
                tp2 = h4_tp2 if h4_tp2 > 0 else round(fallback_tp2, 2)
                if direction == "buy" and tp2 <= tp1:
                    tp2 = round(fallback_tp2, 2)
                elif direction == "sell" and tp2 >= tp1:
                    tp2 = round(fallback_tp2, 2)
                tp3 = h4_tp3  # None se non c'e' terza liquidita' H4
                logger.info("[%s]   [%d] TP da liquidita H4: TP1=%.2f TP2=%.2f TP3=%s",
                            symbol, i, tp1, tp2, f"{tp3:.2f}" if tp3 else "N/A")
            else:
                # Daytrading conserva il fallback storico 3R; solo lo
                # swing applica la nuova soglia minima 4R.
                fallback_tp1_rr = (
                    config.get_min_rr("swing", sig.get("setup_type"))
                    if mode == "swing" else 3.0
                )
                fallback_tp2_rr = max(5.0, fallback_tp1_rr + 1.0) if mode == "swing" else 5.0
                tp1 = round(entry + risk_price * fallback_tp1_rr, 2) if direction == "buy" else round(entry - risk_price * fallback_tp1_rr, 2)
                tp2 = round(entry + risk_price * fallback_tp2_rr, 2) if direction == "buy" else round(entry - risk_price * fallback_tp2_rr, 2)
                tp3 = None  # fallback: no TP3
                logger.info("[%s]   [%d] TP fallback %.0fx/%.0fx: TP1=%.2f TP2=%.2f (no TP3)",
                            symbol, i, fallback_tp1_rr, fallback_tp2_rr, tp1, tp2)
            # ============================================================
            # FILTRO LIQUIDITA' (regole ④⑩ del corso SMC): nessuna zona
            # davanti all'entry e liquidità opposta abbastanza lontana per
            # il R:R richiesto, prima di spendere il rischio.
            # ============================================================
            if not liq_zones_h4.empty:
                liq_ok, liq_reason = validate_liquidity_environment(
                    liq_zones_h4, entry, sl, direction,
                    min_rr=config.get_min_rr(mode, sig.get("setup_type")),
                    mode=mode,
                )
                if not liq_ok:
                    logger.info("[%s]   [%d] ⚠️ Liquidità davanti: %s — salto trade.",
                                symbol, i, liq_reason)
                    continue

            rr = round(abs(entry - tp1) / risk_price, 1) if risk_price > 0 else 0

            logger.info("[%s]   [%d] %s | entry=%.2f sl=%.2f (%.0f pip) tp1=%.2f tp2=%.2f | "
                        "rr=%.1f | %s | sweep=%s | prob=%s",
                        symbol, i, direction.upper(),
                        entry, sl, abs(entry - sl) / pip, tp1, tp2,
                        rr, sig["setup_type"],
                        sig.get("sweep_type", "?"), sig["probability"])

            # Rischio dinamico
            risk = config.RISK_PERCENT
            if sig["setup_type"] == "counter_trend":
                risk = risk * 0.5
            if not can_place_orders:
                risk = risk * 0.5  # sessione chiusa: pending limit con rischio dimezzato
            if near_news:
                risk = risk * 0.5
            # Il conflitto DXY è un blocco, non un dimezzamento del rischio:
            # un setup contro il Dollaro viene scartato sotto (vedi dxy_conflict).
            dxy_note = ""

            # M1 raffinamento: stringi SL entro il ceiling H4
            sl = self._refine_entry_m1(symbol, direction, entry, sl, min_sl_m1)

            # R:R con SL raffinato (TP resta basato su rischio H4)
            rr_m1 = round(abs(entry - tp1) / abs(entry - sl), 1) if abs(entry - sl) > 0 else rr

            # ============================================================
            # FILTRO SPREAD E VOLATILITÀ (punto 6), sui livelli finali:
            # spread massimo per simbolo, spread/SL, range M5/M15,
            # TP realistico e candela veloce non anomala.
            # ============================================================
            _farthest_tp = tp3 if tp3 else tp2 if tp2 else tp1
            vol_ok, vol_reason = validate_volatility_filter(
                symbol, direction, entry, sl,
                spread_pips=_spread_pips,
                avg_range_pips=_avg_range_pips,
                mode=mode,
                tp_price=_farthest_tp,
                fast_avg_range_pips=_fast_avg_range_pips,
                current_range_pips=_current_range_pips,
                require_fast_range=True,
                require_slow_range=True,
            )
            if not vol_ok:
                logger.info("[%s]   [%d] ⚠️ Spread/volatilità: %s — salto trade.",
                            symbol, i, vol_reason)
                continue

            # ============================================================
            # GATE LIQUIDITÀ PRE-ENTRY (regole ④⑩):
            # sweep realmente pulito, nessun ostacolo davanti, target opposto
            # sufficiente per il R:R, corridoio entry/SL privo di retail e
            # nessun ingresso dopo un movimento già esteso.
            # Lo sweep HTF resta obbligatorio per entrambe le modalità: la
            # conferma MTF/LTF non può sostituire la liquidità realmente presa.
            # ============================================================
            liq_ok, liq_reason = validate_pre_entry_liquidity(
                df=h4_df,
                swings=h4_swings,
                liq_zones=liq_zones_h4,
                sweep_check=sweep_check,
                reversal=reversal,
                entry=entry,
                sl=sl,
                direction=direction,
                min_rr=config.get_min_rr(mode, sig.get("setup_type")),
                mode=mode,
                # Il daytrading usa target M5/M15: la liquidità H4 opposta
                # non deve imporre un R:R che appartiene allo swing.
                target_levels=(h4_tp1, h4_tp2, h4_tp3) if mode == "swing" else None,
                avg_range_price=(_avg_range_pips * pip),
                require_sweep=True,
                symbol=symbol,
            )
            if not liq_ok:
                logger.info("[%s]   [%d] ⚠️ Liquidità pre-entry: %s — salto trade.",
                            symbol, i, liq_reason)
                continue
            logger.debug("[%s]   [%d] ✅ Liquidità pre-entry pulita: %s",
                         symbol, i, liq_reason)

            # ============================================================
            # BLOCCO DXY (punto 5): nessun trade contro il trend del Dollaro.
            # EUR/GBP/XAU inverse, USDJPY diretta, GBPJPY cross neutro.
            # ============================================================
            dxy_conflict = False
            dxy_reason = ""
            if dxy and dxy.get("trend"):
                dxy_conflict, dxy_reason = detect_dxy_conflict(
                    symbol, direction, dxy["trend"],
                )
            # Per lo swing la classificazione deve usare lo sweep H4 che ha
            # superato il gate contestuale, non un eventuale sweep dell'OB LTF.
            sweep_for_class = (
                sweep_check.get("type", "none")
                if mode == "swing" else sig.get("sweep_type", "none")
            )
            setup_type, setup_detail = classify_setup_type(
                sweep_for_class, reversal if sweep_check.get("swept") else {},
                near_news, trend, direction, dxy_conflict,
            )
            logger.info("[%s]   [%d] [SETUP] %s - %s",
                        symbol, i, setup_type, setup_detail)

            # I setup generic o con conflitto DXY sono diagnostici soltanto:
            # vengono registrati ma non trasformati in ordini.
            if setup_type == "generic" or dxy_conflict:
                logger.info("[%s:%s] [REJECTED-SETUP] %s: %s%s",
                            symbol, mode, setup_type, setup_detail,
                            f" | {dxy_reason}" if dxy_reason else "")
                continue
            # Per lo swing la priorità alta è obbligatoria: sweep + inversione
            # istituzionale + displacement. L'exhaustion resta osservazionale.
            if mode == "swing" and setup_type != "manipulation":
                logger.info("[%s:swing] [REJECTED-SETUP] priorità non alta: %s", symbol, setup_type)
                continue

            # I counter-trend daytrade richiedono sweep coerente e MSS/TC.
            if mode == "daytrading" and sig.get("setup_type") == "counter_trend":
                counter_ok, counter_reason = validate_daytrading_counter_trend(
                    direction=direction,
                    trend=trend,
                    sweep_check=sweep_check,
                    structure_events=(m5 or {}).get("structure_events", []),
                )
                if not counter_ok:
                    logger.info("[%s:daytrading] [REJECTED-SETUP] %s", symbol, counter_reason)
                    continue

            # Lo swing deve aspettare il ritorno al bordo iniziale dell'OB:
            # niente ingresso market dopo che il movimento è già partito.
            tickets = self._send_single_order(symbol, direction, entry, sl, tp1, tp2, risk, tp3,
                                              pending_mode=(mode == "swing" or not can_place_orders),
                                              magic=mode_magic, mode=mode)
            if tickets:
                self._orders_today += len(tickets)

            if tickets:
                ticket_str = " | ".join(f"{t}" for t in tickets)
                tp3_str = f" | TP3: {tp3:.2f}" if tp3 else ""
                is_pending = not can_place_orders
                trade_label = "PENDING LIMIT PIAZZATO ⏳" if is_pending else "POSIZIONE APERTA"
                risk_note = f" | Rischio: {risk:.1f}%" if is_pending else ""
                session_note = f" | Sessione: {session}" if is_pending else ""
                msg = (
                    f"[TRADE] {trade_label}\n"
                    f"{symbol}: {direction.upper()}\n"
                    f"{'-'*30}\n"
                    f"Entry: {entry:.2f}\n"
                    f"SL: {sl:.2f}\n"
                    f"TP1: {tp1:.2f} | TP2: {tp2:.2f}{tp3_str}\n"
                    f"Ticket: {ticket_str}\n"
                    f"{'-'*30}\n"
                    f"RR: {rr_m1:.1f} | {sig['setup_type']} | {sig['probability']}"
                    f"{risk_note}{session_note}\n"
                    f"Trend {htf_label}: {trend} | Zone: {sig.get('pd_zone', '?')}{poi_info}"
                    f"{dxy_note}"
                )
                if self._notifier:
                    try:
                        self._notifier(msg)
                    except Exception as e:
                        logger.warning("Notifica Telegram fallita: %s", e)
    def _send_single_order(
        self, symbol: str, direction: str,
        entry: float, sl: float, tp1: float, tp2: float, risk_pct: float,
        tp3: Optional[float] = None,
        pending_mode: bool = False,
        magic: int = MAGIC_MAIN,
        mode: str = "daytrading",
    ) -> list[int]:
        """Apre 1 posizione (MARKET o PENDING LIMIT) col 100% del lotto.

        Strategia SMC con chiusure parziali:
        - 1 sola posizione, 100% del lotto calcolato sul rischio.
        - TP sull'ordine = target piu' lontano nella direzione del trade.
        - Le chiusure parziali (30% TP1, 30% TP2) sono gestite dal
          PartialCloseTracker nel loop BE.
        - MT5 chiude automaticamente il remainder al TP dell'ordine.

        Modalita' pending (pending_mode=True):
        - Piazzo ordine BUY_LIMIT / SELL_LIMIT al livello OB entry.
        - Il lotto e' calcolato sull'OB entry (fill atteso al livello limite).
        - Usato in sessione 'closed' per entrare quando il mercato riapre.
        - Rischio ridotto del 50% dal chiamante prima di passare risk_pct.
        """
        # --- Prezzo per calcolo lotti ---
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("[%s] Tick non disponibile per calcolo lotti.", symbol)
            return []
        market_entry = float(tick.ask if direction == "buy" else tick.bid)

        # Per pending limit: lot size calcolato sul prezzo OB (fill atteso)
        # Per market: lot size calcolato sul prezzo corrente
        calc_entry = entry if pending_mode else market_entry

        # Avvisa se il prezzo di mercato e' lontano dall'OB entry
        pip = utils.pip_size(symbol)
        market_distance_pips = abs(market_entry - entry) / pip
        max_pending_pips = config.get_max_pending_distance_pips(mode)
        if market_distance_pips > 50:
            logger.warning(
                "[%s] OB entry (%.2f) distante %.1f pip dal mercato (%.2f).",
                symbol, entry, market_distance_pips, market_entry,
            )

        # Un setup oltre il limite e' stantio: NON va trasformato in un
        # pending limit. In precedenza questo ramo convertiva, per esempio,
        # 157.710 -> 163.538 in un SELL LIMIT GTC, creando uno swing
        # involontario. Il pending resta ammesso solo entro il limite della
        # modalità e con una scadenza giornaliera (vedi _place_pending_limit).
        if market_distance_pips > max_pending_pips:
            logger.warning(
                "[%s:%s] Setup scartato: entry %.5f distante %.1f pip dal mercato "
                "(massimo %d pip). Nessun pending verra' piazzato.",
                symbol, mode, entry, market_distance_pips, max_pending_pips,
            )
            return []

        # ============================================================
        # SAFETY: SL dalla parte corretta rispetto al prezzo di mercato
        # ============================================================
        # Un BUY con SL >= prezzo (o SELL con SL <= prezzo) non e'
        # eseguibile a mercato: il broker lo rifiuta (10016 Invalid stops)
        # e il lotto calcolato su una distanza minima esplode
        # (10019 No money). Segnali con entry OB sopra/sotto il mercato
        # sono setup stantii: li saltiamo senza tentare l'ordine.
        _sl_invalid = False
        if not pending_mode:
            # Guardia sl > 0: con SL assente (0) l'ordine market e' comunque
            # invalido per la strategia, ma non lo trattiamo come 'lato sbagliato'
            if sl > 0 and direction == "buy" and sl >= market_entry:
                _sl_invalid = True
            elif sl > 0 and direction == "sell" and sl <= market_entry:
                _sl_invalid = True
        if _sl_invalid:
            if self._can_skip_log(symbol, f"sl-wrong-side-{direction}", interval=300):
                logger.warning(
                    "[%s] %s scartato: SL (%.2f) dalla parte sbagliata rispetto "
                    "al mercato (%.2f) — setup non eseguibile, nessun ordine.",
                    symbol, direction.upper(), sl, market_entry,
                )
            return []

        # ============================================================
        # SAFETY: PENDING LIMIT con entry dalla parte sbagliata
        # ============================================================
        # Un BUY LIMIT richiede entry SOTTO il mercato; un SELL LIMIT
        # richiede entry SOPRA il mercato. Se l'entry OB sta dall'altra
        # parte (es. OB BUY sopra il mercato), il "limit" sarebbe in
        # realta' uno STOP (operativita' non prevista dalla strategia)
        # e MT5 lo rifiuterebbe. Saltiamo il setup.
        if pending_mode:
            _limit_invalid = False
            if direction == "buy" and entry >= market_entry:
                _limit_invalid = True
            elif direction == "sell" and entry <= market_entry:
                _limit_invalid = True
            if _limit_invalid:
                if self._can_skip_log(symbol, f"limit-wrong-side-{direction}", interval=300):
                    logger.warning(
                        "[%s] %s LIMIT con entry (%.2f) dalla parte sbagliata rispetto "
                        "al mercato (%.2f) — sarebbe uno STOP, non un limite: scartato.",
                        symbol, direction.upper(), entry, market_entry,
                    )
                return []

        try:
            total_lot = calculate_lot_size(symbol, calc_entry, sl, risk_pct)
        except Exception as e:
            logger.error("[%s] Calcolo lotti fallito: %s", symbol, e)
            return []

        if total_lot <= 0:
            logger.warning("[%s] Lotto totale <= 0", symbol)
            return []

        # --- Safety: margine disponibile (evita retcode 10019 "No money") ---
        # Il lotto da rischio puo' richiedere piu' margine del conto (es. gold
        # con SL stretto e risk alto): riduci al massimo sostenibile.
        total_lot = self._cap_lot_to_margin(symbol, direction, calc_entry, total_lot)
        if total_lot <= 0:
            logger.warning("[%s] Margine insufficiente: ordine saltato.", symbol)
            return []

        # Determina il TP piu' lontano per l'ordine MT5 (direction-aware)
        tp_candidates = [tp1]
        if abs(tp2 - tp1) > 0.0001:
            tp_candidates.append(tp2)
        if tp3 is not None and abs(tp3 - tp2) > 0.0001:
            tp_candidates.append(tp3)
        farthest_tp = max(tp_candidates) if direction == "buy" else min(tp_candidates)

        # Ultima barriera prima del calcolo/invio: controlla geometria, ordine
        # TP1/TP2/TP3 e il range SL usando i pip reali del simbolo. Il TP non
        # ha un tetto: può seguire la liquidità individuata dall'analisi.
        levels_ok, levels_reason = utils.validate_intraday_levels(
            symbol, direction, entry, sl, farthest_tp, market_entry, mode,
            tp_levels=tp_candidates,
        )
        if not levels_ok:
            logger.warning(
                "[%s:%s] Setup scartato prima dell'ordine: %s",
                symbol, mode, levels_reason,
            )
            return []

        # tp_label: quale target e' stato effettivamente selezionato?
        if tp3 is not None and abs(farthest_tp - tp3) < 0.0001:
            tp_label = "TP3"
        elif abs(farthest_tp - tp2) < 0.0001:
            tp_label = "TP2"
        else:
            tp_label = "TP1"
        comment = f"SMC {tp_label} {'pending' if pending_mode else 'runner'}"

        # --- Esecuzione: market vs pending limit ---
        if pending_mode:
            ticket = self._place_pending_limit(
                symbol, direction, total_lot, entry, sl, farthest_tp,
                magic, comment, mode=mode,
            )
        else:
            ticket = self._place_market(
                symbol, direction, total_lot, sl, farthest_tp,
                magic, comment,
            )

        if ticket:
            # Registra nel PartialCloseTracker per gestire chiusure parziali
            # (Solo per ordini market: i pending vengono registrati dal
            #  monitor posizioni quando l'ordine viene fillato)
            if not pending_mode:
                has_tp2 = abs(tp2 - tp1) > 0.0001
                has_tp3 = tp3 is not None and abs(tp3 - tp2) > 0.0001
                tracker = get_tracker()
                tracker.register(
                    ticket=ticket,
                    tp1=tp1,
                    tp2=tp2 if has_tp2 else tp1,
                    tp3=tp3 if has_tp3 else None,
                    initial_volume=total_lot,
                    direction=direction,
                )
            else:
                # Salva info per re-registrare il tracker quando l'ordine fillera'
                self._pending_order_info[ticket] = {
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": tp3,
                    "initial_volume": total_lot,
                    "direction": direction,
                    "symbol": symbol,
                    "created_at": time.time(),
                }
            mode_label = "PENDING" if pending_mode else "MARKET"
            logger.info(
                "[%s] [OK] %s %s ticket=%s vol=%.2f SL=%.2f TP=%s=%.2f",
                symbol, mode_label,
                direction.upper(), ticket, total_lot, sl, tp_label, farthest_tp,
            )
            return [ticket]
        return []

    @staticmethod
    def _candidate_fillings(symbol: str) -> list[int]:
        """Ordine di preferenza dei ``type_filling`` da provare sul simbolo.

        Parte dalle modalita' dichiarate supportate dal ``filling_mode`` del
        simbolo (IOC, poi FOK), poi aggiunge le restanti come fallback: cosi'
        l'ordine market non viene rifiutato con 10030 (Unsupported filling mode).
        """
        info = mt5.symbol_info(symbol)
        modes = getattr(info, "filling_mode", 0) if info is not None else 0

        preferred: list[int] = []
        if modes & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            preferred.append(mt5.ORDER_FILLING_IOC)
        if modes & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            preferred.append(mt5.ORDER_FILLING_FOK)

        result = list(preferred)
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK,
                        mt5.ORDER_FILLING_RETURN):
            if filling not in result:
                result.append(filling)
        return result

    def _cap_lot_to_margin(
        self, symbol: str, direction: str,
        entry: float, lot: float,
    ) -> float:
        """Riduce il lotto se il margine richiesto eccede il margine libero.

        Usa ``mt5.order_calc_margin`` per calcolare il margine del lotto
        proposto; se supera il margine libero disponibile, scala il lotto alla
        massima dimensione sostenibile (rispettando volume_min/volume_step).
        Se nemmeno il lotto minimo e' sostenibile, ritorna 0.0 (ordine da saltare).
        """
        try:
            info = mt5.symbol_info(symbol)
            account = mt5.account_info()
            if info is None or account is None or lot <= 0:
                return lot
            order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
            margin = mt5.order_calc_margin(order_type, symbol, lot, entry)
            free_margin = float(account.margin_free)
            if free_margin <= 0:
                # Margine libero nullo/negativo: l'ordine verrebbe rifiutato
                # con retcode 10019 "No money" a ogni scan. Meglio saltarlo.
                logger.warning(
                    "[%s] Margine libero %.2f <= 0: ordine saltato (evito retcode 10019).",
                    symbol, free_margin,
                )
                return 0.0
            if margin is None or margin <= 0:
                return lot  # non calcolabile: non bloccare

            # Tetto al 90% del margine libero (margine di sicurezza)
            budget = free_margin * 0.9
            if float(margin) <= budget:
                return lot

            step_dec = Decimal(str(getattr(info, "volume_step", 0.01)))
            min_lot = float(getattr(info, "volume_min", 0.01))
            max_lot = float(getattr(info, "volume_max", lot))
            if step_dec > 0:
                # Quantizza al volume_step esatto (Decimal, come calculate_lot_size)
                # e arrotonda verso il BASSO per non superare mai il budget.
                scaled_raw = (Decimal(str(lot)) * Decimal(str(budget))
                              / Decimal(str(margin)))
                scaled = float((scaled_raw / step_dec).quantize(
                    Decimal('1'), rounding=ROUND_DOWN) * step_dec)
            else:
                scaled = lot * budget / float(margin)
            capped = min(max_lot, max(min_lot, scaled))

            logger.warning(
                "[%s] Margine insufficiente: lotto %.2f richiede margine %.2f > libero %.2f — ridotto a %.2f.",
                symbol, lot, float(margin), free_margin, capped,
            )
            # Verifica finale sul lotto ridotto
            margin2 = mt5.order_calc_margin(order_type, symbol, capped, entry)
            if margin2 is not None and float(margin2) > budget:
                logger.error(
                    "[%s] Margine insufficiente anche al lotto minimo (%.2f): ordine saltato.",
                    symbol, min_lot,
                )
                return 0.0
            return float(capped)
        except Exception as e:
            logger.warning("[%s] Errore controllo margine: %s", symbol, e)
            return lot

    def _place_pending_limit(
        self, symbol: str, direction: str, volume: float,
        entry: float, sl: float, tp: float, magic: int, comment: str,
        mode: str = "daytrading",
    ) -> Optional[int]:
        """Piazza un ordine PENDING LIMIT al livello OB entry.

        Usato in sessione 'closed': l'ordine resta in attesa e viene
        fillato automaticamente quando il mercato riapre e il prezzo
        tocca il livello limite.

        SL e TP vengono piazzati contestualmente all'ordine pending.
        """
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("[%s] Simbolo non trovato.", symbol)
            return None

        digits = int(info.digits)
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "buy" else mt5.ORDER_TYPE_SELL_LIMIT

        # --- Safety: verifica che SL e entry siano dalla parte corretta ---
        # Calcola stops_level/point UNA SOLA VOLTA (usati sia per entry-vs-mercato
        # che per SL/TP-vs-entry piu' avanti)
        stops_level_p = float(getattr(info, "trade_stops_level", 0) or 0)
        point_p = float(getattr(info, "point", 0) or 0)

        tick = mt5.symbol_info_tick(symbol)
        if tick is not None:
            current_price = float(tick.ask if direction == "buy" else tick.bid)
            pip = utils.pip_size(symbol)
            if direction == "buy":
                if entry >= current_price:
                    if self._can_skip_log(symbol, "pending-limit-wrong-side", interval=300):
                        logger.warning(
                            "[%s] BUY LIMIT entry (%.2f) >= mercato (%.2f): "
                            "annullato (fillerebbe subito).",
                            symbol, entry, current_price,
                        )
                    return None
            else:
                if entry <= current_price:
                    if self._can_skip_log(symbol, "pending-limit-wrong-side", interval=300):
                        logger.warning(
                            "[%s] SELL LIMIT entry (%.2f) <= mercato (%.2f): "
                            "annullato (fillerebbe subito).",
                            symbol, entry, current_price,
                        )
                    return None
            # Distanza minima entry dal mercato (stops_level, evita 10015)
            if stops_level_p > 0 and point_p > 0:
                min_dist = stops_level_p * point_p
                if direction == "buy":
                    dist = current_price - entry
                else:
                    dist = entry - current_price
                if dist < min_dist - (point_p * 0.5):  # tolleranza 0.5 pip
                    if self._can_skip_log(symbol, "pending-entry-too-close", interval=300):
                        logger.warning(
                            "[%s] %s LIMIT: entry (%.2f) troppo vicina al mercato "
                            "(%.2f, minimo %.2f, distanza %.04f) — annullato.",
                            symbol, direction.upper(), entry, current_price, min_dist, dist,
                        )
                    return None
            pip = utils.pip_size(symbol)
            dist_pips = (current_price - entry) / pip if direction == "buy" else (entry - current_price) / pip
            logger.info(
                "[%s] PENDING LIMIT distanza dal mercato: %.0f pip",
                symbol, dist_pips,
            )
        if direction == "buy":
            if sl >= entry:
                logger.error("[%s] BUY LIMIT con SL (%.2f) >= entry (%.2f) — annullato.", symbol, sl, entry)
                return None
            if tp <= entry:
                logger.error("[%s] BUY LIMIT con TP (%.2f) <= entry (%.2f) — annullato.", symbol, tp, entry)
                return None
        else:  # sell
            if sl <= entry:
                logger.error("[%s] SELL LIMIT con SL (%.2f) <= entry (%.2f) — annullato.", symbol, sl, entry)
                return None
            if tp >= entry:
                logger.error("[%s] SELL LIMIT con TP (%.2f) >= entry (%.2f) — annullato.", symbol, tp, entry)
                return None

        # --- Safety: distanza minima stop dal livello limite (evita 10016) ---
        # (stops_level_p e point_p sono gia' stati calcolati sopra)
        if stops_level_p > 0 and point_p > 0:
            min_stop_p = stops_level_p * point_p
            if sl > 0 and abs(entry - sl) < min_stop_p:
                logger.warning(
                    "[%s] %s LIMIT: SL (%.2f) troppo vicino all'entry (%.2f, "
                    "minimo broker %.2f) — ordine saltato.",
                    symbol, direction.upper(), sl, entry, min_stop_p,
                )
                return None
            if tp > 0 and abs(tp - entry) < min_stop_p:
                logger.warning(
                    "[%s] %s LIMIT: TP (%.2f) troppo vicino all'entry (%.2f, "
                    "minimo broker %.2f) — ordine saltato.",
                    symbol, direction.upper(), tp, entry, min_stop_p,
                )
                return None

        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": round(float(entry), digits),
            "sl": round(float(sl), digits),
            "tp": round(float(tp), digits),
            "deviation": config.ORDER_DEVIATION,
            "magic": magic,
            "comment": comment,
            # Swing: il pending può attendere il ritorno al POI per più
            # sessioni. Il daytrading resta valido solo per la giornata.
            "type_time": (
                getattr(mt5, "ORDER_TIME_GTC", 0)
                if mode == "swing"
                else getattr(mt5, "ORDER_TIME_DAY", mt5.ORDER_TIME_GTC)
            ),
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }

        # --- Negozia il type_filling: prova IOC, poi FOK, poi RETURN ---
        # (il broker puo' supportare modalita' diverse per simbolo: evita 10030)
        invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        result = None
        for filling in self._candidate_fillings(symbol):
            req["type_filling"] = filling
            result = mt5.order_send(req)
            if result is None:
                logger.error("[%s] PENDING order_send ritornato None: %s", symbol, mt5.last_error())
                return None
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.warning(
                    "[%s] PENDING rifiutato con filling %s: retcode=%s comment=%s",
                    symbol, filling, result.retcode, getattr(result, "comment", ""),
                )
                if result.retcode == invalid_fill:
                    continue  # riprova con il prossimo type_filling
                break  # errore diverso (margine, prezzo...): niente retry

        if result is None:
            return None
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("[%s] PENDING rifiutato: retcode=%s comment=%s",
                        symbol, result.retcode, getattr(result, "comment", ""))
            return None

        ticket = int(getattr(result, "order", 0))
        pip = utils.pip_size(symbol)
        risk_pips = abs(entry - sl) / pip if sl else 0
        logger.info("[%s] [OK] PENDING %s ticket=%s vol=%s @ %.2f SL=%.2f (%.0f pip) TP=%.2f | %s",
                    symbol, direction.upper(), ticket, volume, entry, sl, risk_pips, tp, comment)
        return ticket

    def _place_market(
        self, symbol: str, direction: str, volume: float,
        sl: float, tp: float, magic: int, comment: str,
    ) -> Optional[int]:
        """Apre una posizione a MERCATO (esecuzione immediata).
        SL e TP vengono piazzati contestualmente all'ordine.
        Il BE/trailing vengono gestiti dal loop break-even.
        """
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("[%s] Tick non disponibile per market order.", symbol)
            return None

        prezzo = float(tick.ask if direction == "buy" else tick.bid)
        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

        # --- Safety: verifica che SL e TP siano dalla parte corretta ---
        if direction == "buy":
            if sl >= prezzo:
                logger.error("[%s] BUY con SL (%.2f) >= prezzo mercato (%.2f) — annullato.", symbol, sl, prezzo)
                return None
            if tp <= prezzo:
                logger.error("[%s] BUY con TP (%.2f) <= prezzo mercato (%.2f) — annullato.", symbol, tp, prezzo)
                return None
        else:  # sell
            if sl <= prezzo:
                logger.error("[%s] SELL con SL (%.2f) <= prezzo mercato (%.2f) — annullato.", symbol, sl, prezzo)
                return None
            if tp >= prezzo:
                logger.error("[%s] SELL con TP (%.2f) >= prezzo mercato (%.2f) — annullato.", symbol, tp, prezzo)
                return None

        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("[%s] Simbolo non trovato.", symbol)
            return None

        digits = int(info.digits)

        # --- Safety: distanza minima stop (evita retcode 10016 Invalid stops) ---
        # Il broker impone una distanza minima (trade_stops_level, in points)
        # tra il prezzo e SL/TP. Se SL o TP sono piu' vicini, MT5 rifiuta
        # l'ordine con 10016: meglio saltarlo con un log chiaro.
        stops_level = float(getattr(info, "trade_stops_level", 0) or 0)
        point_size = float(getattr(info, "point", 0) or 0)
        if stops_level > 0 and point_size > 0:
            min_stop_dist = stops_level * point_size
            if (abs(prezzo - sl) < min_stop_dist or abs(tp - prezzo) < min_stop_dist):
                logger.warning(
                    "[%s] %s stop troppo vicini al prezzo (SL %.2f / TP %.2f, "
                    "minimo broker %.2f) — ordine saltato.",
                    symbol, direction.upper(), sl, tp, min_stop_dist,
                )
                return None

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": prezzo,
            "sl": round(float(sl), digits),
            "tp": round(float(tp), digits),
            "deviation": config.ORDER_DEVIATION,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # --- Negozia il type_filling: prova IOC, poi FOK, poi RETURN ---
        # (il broker puo' supportare modalita' diverse per simbolo: evita 10030)
        invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        result = None
        for filling in self._candidate_fillings(symbol):
            req["type_filling"] = filling
            result = mt5.order_send(req)
            if result is None:
                logger.error("[%s] order_send ritornato None: %s", symbol, mt5.last_error())
                return None
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.warning(
                    "[%s] Ordine rifiutato con filling %s: retcode=%s comment=%s",
                    symbol, filling, result.retcode, getattr(result, "comment", ""),
                )
                if result.retcode == invalid_fill:
                    continue  # riprova con il prossimo type_filling
                break  # errore diverso (margine, prezzo...): niente retry

        if result is None:
            return None
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("[%s] Ordine rifiutato: retcode=%s comment=%s",
                        symbol, result.retcode, getattr(result, "comment", ""))
            return None

        ticket = int(getattr(result, "order", 0))
        # Calcola pips per log
        pip = utils.pip_size(symbol)
        risk_pips = abs(prezzo - sl) / pip if sl else 0
        logger.info("[%s] [OK] MARKET %s ticket=%s vol=%s @ %.2f SL=%.2f (%.0f pip) TP=%.2f | %s",
                    symbol, direction.upper(), ticket, volume, prezzo, sl, risk_pips, tp, comment)
        return ticket

    # -- Notifica OB in formazione ------------------------------------------

    def _maybe_send_ob_update(self, symbol: str, h4: dict, m15: dict, dxy: Optional[dict] = None) -> None:
        """Invia i dettagli degli OB in formazione su Telegram (con throttle).

        Invia se:
        - sono passati >= OB_UPDATE_INTERVAL secondi dall'ultimo invio, OPPURE
        - lo status degli OB e' cambiato rispetto all'ultimo invio
        """
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente aggiornamenti OB
        now = time.time()
        obs = m15.get("ob_potentials", [])
        if not obs:
            return

        # Signature: concatena gli status per rilevare cambiamenti
        signature = "|".join(
            f"{o['direction']}@{o['entry']:.2f}:{o['status']}" for o in obs
        )
        status_changed = signature != self._last_ob_signature
        time_elapsed = now - self._last_ob_update >= OB_UPDATE_INTERVAL

        if not (status_changed or time_elapsed):
            return

        self._last_ob_update = now
        self._last_ob_signature = signature

        prezzo = m15.get("current_price", 0)
        session = get_current_session()

        lines = [
            f"📋 SETUP IN FORMAZIONE — {symbol}",
            f"Trend H4: {h4['trend']} | Prezzo: {prezzo:.2f} | Sessione: {session}",
        ]
        if dxy and dxy.get("bias"):
            lines.append(f"💵 DXY: {dxy['trend']} | {dxy['bias']}")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")

        ready_count = 0
        for i, ob in enumerate(obs, 1):
            arrow = "🟢" if ob["status"] == "ready" else "🟡"
            if ob["status"] == "ready":
                ready_count += 1
            lines.append(
                f"{arrow} [{i}] {ob['direction'].upper()} "
                f"E={ob['entry']:.2f} SL={ob['sl']:.2f} | "
                f"TP1={ob['tp1']:.2f} TP2={ob['tp2']:.2f} | "
                f"RR={ob['rr']:.2f} | {ob['pd_zone']}"
            )
            if ob["status"] != "ready":
                lines.append(f"      ⚠️ {ob['status']}")

        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        if ready_count > 0:
            lines.append(f"✅ {ready_count} setup pronto/i — ordini verranno piazzati")
        else:
            lines.append(f"⏳ Setup in attesa — condizioni non ancora soddisfatte")

        msg = "\n".join(lines)
        logger.info("[%s] 📋 OB update inviato: %d OB (%d ready)",
                    symbol, len(obs), ready_count)
        if self._notifier:
            try:
                self._notifier(msg)
            except Exception as e:
                logger.warning("Notifica OB update fallita: %s", e)

    # -- M1 3-CANDLE PATTERN (Video 25: doji + forte + forte) -------------

    def _check_m1_3candle_pattern(self, df: pd.DataFrame, direction: str) -> bool:
        """Verifica il pattern di 3 candele M1 per entrata sniper.

        Pattern (dal corso SMC, Video 25):
        - Candela 1: DOJI (indecisione) — corpo < 20% del range
        - Candela 2: FORTE (istituzionale) — corpo > 60%, direzione del trade
        - Candela 3: FORTE (conferma) — corpo > 60%, stessa direzione

        Args:
            df: DataFrame M1 con almeno 3 candele
            direction: 'buy' o 'sell'

        Returns:
            True se il pattern e' presente nelle ultime 3 candele.
        """
        if df is None or len(df) < 5:
            return False

        # Prende le ultime 3 candele complete (esclude quella in formazione)
        for start_idx in range(max(0, len(df) - 8), len(df) - 2):
            c1 = df.iloc[start_idx]
            c2 = df.iloc[start_idx + 1]
            c3 = df.iloc[start_idx + 2]

            for c in [c1, c2, c3]:
                c_range = float(c["high"]) - float(c["low"])
                if c_range <= 0:
                    return False

            # Candela 1: DOJI (corpo < 20% del range)
            c1_range = float(c1["high"]) - float(c1["low"])
            c1_body = abs(float(c1["close"]) - float(c1["open"]))
            is_doji = (c1_body / c1_range) < 0.20 if c1_range > 0 else False

            if not is_doji:
                continue

            # Candela 2: FORTE nella direzione del trade
            c2_range = float(c2["high"]) - float(c2["low"])
            c2_body = abs(float(c2["close"]) - float(c2["open"]))
            c2_bullish = float(c2["close"]) > float(c2["open"])
            c2_bearish = float(c2["close"]) < float(c2["open"])
            c2_strong = (c2_body / c2_range) >= 0.60 if c2_range > 0 else False

            if direction == "buy":
                is_c2_ok = c2_bullish and c2_strong
            else:
                is_c2_ok = c2_bearish and c2_strong

            if not is_c2_ok:
                continue

            # Candela 3: FORTE (conferma) nella stessa direzione
            c3_range = float(c3["high"]) - float(c3["low"])
            c3_body = abs(float(c3["close"]) - float(c3["open"]))
            c3_bullish = float(c3["close"]) > float(c3["open"])
            c3_bearish = float(c3["close"]) < float(c3["open"])
            c3_strong = (c3_body / c3_range) >= 0.60 if c3_range > 0 else False

            if direction == "buy":
                is_c3_ok = c3_bullish and c3_strong
            else:
                is_c3_ok = c3_bearish and c3_strong

            if is_c3_ok:
                logger.debug("[M1] Pattern 3 candele TROVATO a idx %d: doji + forte + forte", start_idx)
                return True

        return False

    # -- M1 Refinement (entrata perfetta) -----------------------------------

    def _refine_entry_m1(
        self, symbol: str, direction: str,
        entry: float, h4_sl: float, min_sl_pips_m1: float,
    ) -> float:
        """Raffina SL con M1: entrata SNIPER tra floor M1 e ceiling H4.

        Strategia a 3 livelli:
        - H4 SL = CEILING assoluto (mai oltrepassare)
        - M1 swing = SNIPER (stringe SL per entrata millimetrica)
        - MIN M1 = FLOOR (mai sotto X pip, sicurezza minima)

        Restituisce lo SL raffinato: h4_sl >= SL >= (entry ± min_sl_m1).
        """
        pip = utils.pip_size(symbol)
        floor_dist = min_sl_pips_m1 * pip
        sl = h4_sl  # partenza: ceiling H4

        try:
            df = sa.get_market_data(symbol, mt5.TIMEFRAME_M1, bars=50)
            if df is None or len(df) < 20:
                return h4_sl

            current_price = float(df["close"].iloc[-1])
            distance_pips = abs(current_price - entry) / pip

            if distance_pips > 100:
                logger.debug("[%s] M1: prezzo distante %.0f pip. Tengo H4 SL.", symbol, distance_pips)
                return h4_sl

            # === M1 3-CANDLE PATTERN CHECK (Video 25: doji + forte + forte) ===
            has_3candle = self._check_m1_3candle_pattern(df, direction)
            if not has_3candle:
                logger.debug("[%s] M1: pattern 3 candele non trovato. Tengo H4 SL.", symbol)
                return h4_sl

            df = sa.identify_swings(df, window=3)
            swings = sa.filter_alternating_swings(df)
            if swings.empty:
                return h4_sl

            swings = sa.label_structure(swings)
            floor = entry - floor_dist if direction == "buy" else entry + floor_dist

            if direction == "buy":
                lows = swings[swings["type"] == "low"].sort_values("price_level", ascending=False)
                for _, row in lows.iterrows():
                    m1_candidate = float(row["price_level"]) - 1.0 * pip  # buffer 1 pip
                    # Deve essere: sotto entry, sopra H4 SL (più stretto), sopra floor
                    if m1_candidate < entry and m1_candidate > h4_sl and m1_candidate <= floor:
                        sl = m1_candidate
                        logger.info("[%s] 🎯 M1 SNIPER: SL H4=%.2f → M1=%.2f (%.0f pip | floor=%.0f pip)",
                                    symbol, h4_sl, sl, abs(entry - sl) / pip, min_sl_pips_m1)
                        break
                    elif m1_candidate < entry and m1_candidate > h4_sl:
                        # Trovato swing valido ma sotto floor -> usa floor
                        sl = floor
                        logger.info("[%s] 🎯 M1: SL a floor MIN=%.2f (%.0f pip | swing M1=%.2f troppo stretto)",
                                    symbol, sl, min_sl_pips_m1, m1_candidate + 1.0 * pip)
                        break
            else:
                highs = swings[swings["type"] == "high"].sort_values("price_level", ascending=True)
                for _, row in highs.iterrows():
                    m1_candidate = float(row["price_level"]) + 1.0 * pip
                    if m1_candidate > entry and m1_candidate < h4_sl and m1_candidate >= floor:
                        sl = m1_candidate
                        logger.info("[%s] 🎯 M1 SNIPER: SL H4=%.2f → M1=%.2f (%.0f pip | floor=%.0f pip)",
                                    symbol, h4_sl, sl, abs(entry - sl) / pip, min_sl_pips_m1)
                        break
                    elif m1_candidate > entry and m1_candidate < h4_sl:
                        sl = floor
                        logger.info("[%s] 🎯 M1: SL a floor MIN=%.2f (%.0f pip | swing M1=%.2f troppo stretto)",
                                    symbol, sl, min_sl_pips_m1, m1_candidate - 1.0 * pip)
                        break

            logger.debug("[%s] M1: entry=%.2f sl=%.2f (%.0f pip) | H4_ceiling=%.2f | floor=%.0f pip",
                         symbol, entry, sl, abs(entry - sl) / pip, h4_sl, min_sl_pips_m1)
        except Exception as e:
            logger.debug("[%s] M1 refinement non disponibile: %s", symbol, e)

        return sl

    # -- Heartbeat -----------------------------------------------------------

    def _send_heartbeat(self) -> None:
        """Notifica Telegram: bot vivo ma nessun setup da 30+ minuti.
        Include anche un riepilogo degli OB in formazione se disponibili."""
        if config.TELEGRAM_NOTIFY_ONLY_ORDERS:
            return  # solo-ordini: niente heartbeat
        elapsed = int((time.time() - self._last_signal_time) / 60)
        symbols = ", ".join(self._symbols)
        # Recupera gli ultimi OB potenziali da M15 per mostrarli nell'heartbeat
        ob_lines = []
        for symbol in self._symbols:
            try:
                m15_hb = sa.analyze_symbol(symbol, mt5.TIMEFRAME_M15, bars=200, pivot_window=4)
                if m15_hb["success"]:
                    obs = m15_hb.get("ob_potentials", [])
                    if obs:
                        ob_lines.append(f"  {symbol} ({m15_hb['trend']}):")
                        for ob in obs[:3]:  # max 3 OB per simbolo
                            arrow = "🟢" if ob["status"] == "ready" else "🟡"
                            ob_lines.append(
                                f"  {arrow} {ob['direction'].upper()} "
                                f"E={ob['entry']:.2f} RR={ob['rr']:.2f}"
                            )
                            if ob["status"] != "ready":
                                ob_lines.append(f"      {ob['status']}")
            except Exception:
                pass  # heartbeat non deve crashare per un errore M15

        ob_section = "\n".join(ob_lines) if ob_lines else "  Nessun OB in formazione"
        msg = (
            f"💓 HEARTBEAT\n"
            f"Bot attivo da {elapsed} min senza segnali.\n"
            f"Simboli: {symbols}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"OB in formazione:\n"
            f"{ob_section}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Il bot continua a monitorare."
        )
        logger.info("💓 Heartbeat: nessun segnale da %s min.", elapsed)
        if self._notifier:
            try:
                self._notifier(msg)
            except Exception as e:
                logger.warning("Notifica heartbeat fallita: %s", e)

    # -- Riepilogo giornaliero ----------------------------------------------

    def _maybe_send_daily_report(self) -> None:
        """Invia il report giornaliero quando e' l'ora configurata (default 23:00)."""
        # NOTA: il guard solo-ordini NON e' all'inizio: il metodo gestisce anche
        # il rollover giornaliero (reset contatori, snapshot equity) che deve
        # continuare a girare. Blocchiamo solo l'invio del messaggio.
        now = utils.utc_now()
        today = now.date()

        # Rollolday: nuovo giorno -> reset contatori e snapshot equity
        if today != self._report_day:
            self._report_day = today
            self._scans_today = 0
            self._signals_today = 0
            self._orders_today = 0
            self._report_sent_for_day = None
            self._day_start_equity = self._current_equity()
            logger.info("Nuova giornata: reset contatori giornalieri.")
            return

        # Inizializza lo snapshot equity del giorno alla prima iterazione
        if self._day_start_equity is None:
            self._day_start_equity = self._current_equity()

        # Gia' inviato per oggi?
        if self._report_sent_for_day == today:
            return

        # E' l'ora del report? (finestra: DAILY_REPORT_HOUR <= ora < DAILY_REPORT_HOUR+1)
        if now.hour >= DAILY_REPORT_HOUR:
            if not config.TELEGRAM_NOTIFY_ONLY_ORDERS:
                self._send_daily_report()
            self._report_sent_for_day = today

    # -- Convertitore nella valuta del conto (per notifiche) ----------------

    _eur_rate_cache: float = 0.0
    _eur_rate_time: float = 0.0

    def _detect_account_currency(self) -> None:
        """Legge la valuta del conto MT5 (EUR, USD, etc.) e la salva."""
        try:
            info = mt5.account_info()
            if info:
                self._account_currency = str(info.currency).strip().upper()
                logger.info("Valuta conto rilevata: %s", self._account_currency)
            else:
                self._account_currency = "USD"
        except Exception:
            self._account_currency = "USD"

    def _get_eur_rate(self) -> float:
        """Restituisce il tasso EURUSD (1 EUR = X USD).
        Cache di 60 secondi per non appesantire MT5."""
        now = time.time()
        if self._eur_rate_cache > 0 and now - self._eur_rate_time < 60:
            return self._eur_rate_cache
        try:
            tick = mt5.symbol_info_tick("EURUSD")
            if tick is not None:
                rate = (tick.bid + tick.ask) / 2.0
                if rate > 0:
                    self._eur_rate_cache = rate
                    self._eur_rate_time = now
                    return rate
        except Exception:
            pass
        return 1.0  # fallback: se non trova EURUSD, mostra uguale (1:1)

    def _fmt_eur(self, amount: float, *, always_sign: bool = True) -> str:
        """Mostra un importo in EUR nelle notifiche Telegram.

        - Se il conto e' in EUR: formatta direttamente (nessuna conversione).
        - Se il conto e' in USD: converte USD -> EUR usando il tasso EURUSD live.
        """
        if self._account_currency == "USD":
            eur_value = amount / self._get_eur_rate()
        else:
            eur_value = amount
        return utils.format_money(eur_value, "EUR", always_sign=always_sign)

    def _current_equity(self) -> Optional[float]:
        """Legge l'equity corrente da MT5 (in modo sicuro)."""
        try:
            info = mt5.account_info()
            return float(info.equity) if info else None
        except Exception:
            return None

    def _calculate_daily_pl(self) -> tuple[Optional[float], Optional[float]]:
        """Calcola il P/L del giorno confrontando equity di inizio giorno con quella attuale.

        Returns:
            (dollari, percentuale) — entrambi None se non calcolabile.
        """
        current = self._current_equity()
        start = self._day_start_equity
        if current is None or start is None or start == 0:
            return None, None
        diff = current - start
        pct = (diff / start) * 100.0
        return diff, pct

    def _send_daily_report(self) -> None:
        """Compone e invia il riepilogo giornaliero su Telegram."""
        today_str = self._report_day.strftime("%d/%m/%Y")
        pl_usd, pl_pct = self._calculate_daily_pl()
        if pl_usd is not None:
            pl_emoji = "🟢" if pl_usd >= 0 else "🔴"
            pl_line = f"{pl_emoji} P/L: {self._fmt_eur(pl_usd)} ({pl_pct:+.2f}%)"
        else:
            pl_line = "P/L: N/D"

        equity = self._current_equity()
        equity_str = self._fmt_eur(equity, always_sign=False) if equity is not None else "N/D"

        msg = (
            f"📊 RIEPILOGO GIORNALIERO\n"
            f"📅 {today_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 Scansioni: {self._scans_today}\n"
            f"🎯 Segnali trovati: {self._signals_today}\n"
            f"📝 Ordini piazzati: {self._orders_today}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{pl_line}\n"
            f"💰 Equity attuale: {equity_str}\n"
            f"🏦 Simboli: {', '.join(self._symbols)}"
        )
        logger.info("📊 Riepilogo giornaliero: scans=%d signals=%d orders=%d pl=%s",
                    self._scans_today, self._signals_today, self._orders_today,
                    f"{pl_usd:+.2f}" if pl_usd is not None else "N/D")
        if self._notifier:
            try:
                self._notifier(msg)
            except Exception as e:
                logger.warning("Notifica report giornaliero fallita: %s", e)

    # -- Comando /positions ------------------------------------------------

    def _generate_positions_report(self) -> str:
        """Genera report solo delle posizioni aperte (per comando /positions).
        DEVE essere chiamato dal main thread (MT5 non e' thread-safe)."""
        lines = ["📍 POSIZIONI APERTE IN TEMPO REALE"]
        lines.append("━" * 22)
        any_positions = False
        for symbol in self._symbols:
            positions = mt5.positions_get(symbol=symbol)
            if positions and len(positions) > 0:
                any_positions = True
                lines.append(f"📍 {symbol}:")
                for pos in positions:
                    direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                    profit = float(pos.profit)
                    p_emoji = "🟢" if profit >= 0 else "🔴"
                    magic_tag = self._mode_label(int(pos.magic))
                    pip = utils.pip_size(symbol)
                    entry = float(pos.price_open)
                    current = float(pos.price_current)
                    pips = (current - entry) / pip if pos.type == mt5.POSITION_TYPE_BUY else (entry - current) / pip
                    lines.append(
                        f"  {direction} #{pos.ticket} | {magic_tag}\n"
                        f"  Entry: {entry:.2f} → {current:.2f}\n"
                        f"  Vol: {pos.volume} | Pip: {pips:+.0f}\n"
                        f"  {p_emoji} {self._fmt_eur(profit)}"
                    )
        if not any_positions:
            lines.append("Nessuna posizione aperta.")
        lines.append("━" * 22)
        return "\n".join(lines)

    def _on_status_request(self) -> str:
        """Callback per il comando /status: setta un flag e restituisce un placeholder.

        Il report vero viene generato nel main thread perche' MetaTrader5
        non e' thread-safe. Il main loop controlla il flag e invia il report."""
        self._status_requested.set()
        return "⏳ Status in generazione... il report arrivera tra pochi secondi."

    def _on_positions_request(self) -> str:
        """Callback per il comando /positions: setta un flag e restituisce un placeholder."""
        self._positions_requested.set()
        return "⏳ Posizioni in generazione... il report arrivera tra pochi secondi."

    def _generate_status_report(self) -> str:
        """Genera il report di stato live per il comando Telegram /status.

        DEVE essere chiamato dal main thread (MT5 non e' thread-safe).
        Include: sessione corrente, equity, posizioni aperte, ordini pending,
        OB attuali (M15), DXY, contatori giornalieri.
        """
        lines = ["🤖 STATUS LIVE"]

        # Sessione
        session = get_current_session()
        session_emoji = "🟢" if session in TRADABLE_SESSIONS else "🔴"
        lines.append(f"{session_emoji} Sessione: {session}")
        lines.append("━" * 22)

        # Account / equity
        equity = self._current_equity()
        if equity is not None:
            lines.append(f"💰 Equity: {self._fmt_eur(equity, always_sign=False)}")
            pl_usd, pl_pct = self._calculate_daily_pl()
            if pl_usd is not None:
                pl_emoji = "🟢" if pl_usd >= 0 else "🔴"
                lines.append(f"📊 P/L oggi: {pl_emoji} {self._fmt_eur(pl_usd)} ({pl_pct:+.2f}%)")
        else:
            lines.append("💰 Equity: N/D")

        # Contatori giornalieri
        lines.append(f"🔍 Scansioni oggi: {self._scans_today}")
        lines.append(f"🎯 Segnali oggi: {self._signals_today}")
        lines.append(f"📝 Ordini oggi: {self._orders_today}")
        lines.append("━" * 22)

        # Posizioni aperte
        any_positions = False
        for symbol in self._symbols:
            positions = mt5.positions_get(symbol=symbol)
            if positions and len(positions) > 0:
                any_positions = True
                lines.append(f"📍 POSIZIONI {symbol}:")
                for pos in positions:
                    direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
                    profit = float(pos.profit)
                    p_emoji = "🟢" if profit >= 0 else "🔴"
                    magic_tag = self._mode_label(int(pos.magic))
                    lines.append(
                        f"  {direction} #{pos.ticket} | vol={pos.volume} | "
                        f"entry={pos.price_open:.2f} | sl={pos.sl:.2f} | tp={pos.tp:.2f} | "
                        f"{p_emoji} {profit:+.2f} | {magic_tag}"
                    )
        if not any_positions:
            lines.append("📍 Nessuna posizione aperta")

        # Ordini pending
        any_pending = False
        for symbol in self._symbols:
            pending = mt5.orders_get(symbol=symbol)
            if pending and len(pending) > 0:
                any_pending = True
                lines.append(f"⏳ PENDING {symbol}:")
                for ord in pending:
                    direction = "BUY LIMIT" if ord.type == mt5.ORDER_TYPE_BUY_LIMIT else "SELL LIMIT"
                    magic_tag = self._mode_label(int(ord.magic))
                    lines.append(
                        f"  {direction} #{ord.ticket} | vol={ord.volume_initial} | "
                        f"entry={ord.price_open:.2f} | sl={ord.sl:.2f} | tp={ord.tp:.2f} | {magic_tag}"
                    )
        if not any_pending:
            lines.append("⏳ Nessun ordine pending")

        lines.append("━" * 22)

        # DXY
        dxy = get_dxy_bias()
        if dxy and dxy.get("bias"):
            lines.append(f"💵 DXY: {dxy['trend']} | {dxy['bias']} | prezzo={dxy.get('current_price', 0):.2f}")
        else:
            lines.append("💵 DXY: non disponibile")

        # OB attuali (M15) - analisi rapida
        for symbol in self._symbols:
            try:
                m15_sr = sa.analyze_symbol(symbol, mt5.TIMEFRAME_M15, bars=200, pivot_window=4)
                if m15_sr["success"]:
                    obs = m15_sr.get("ob_potentials", [])
                    if obs:
                        lines.append(f"📋 OB M15 {symbol} ({m15_sr['trend']}):")
                        for ob in obs[:3]:
                            arrow = "🟢" if ob["status"] == "ready" else "🟡"
                            lines.append(
                                f"  {arrow} {ob['direction'].upper()} "
                                f"E={ob['entry']:.2f} RR={ob['rr']:.2f}"
                            )
                            if ob["status"] != "ready":
                                lines.append(f"      {ob['status']}")
                    else:
                        lines.append(f"📋 OB M15 {symbol}: nessuno")
            except Exception:
                lines.append(f"📋 OB M15 {symbol}: errore analisi")

        return "\n".join(lines)

    # -- Shutdown -----------------------------------------------------------

    def shutdown(self) -> None:
        logger.info("Shutdown master bot...")
        self._mt5_ready = False
        if self._telegram_listener is not None:
            try:
                self._telegram_listener.stop()
            except Exception:
                logger.exception("Errore shutdown telegram listener.")
        if self._server_thread is not None:
            try:
                self._server_thread.shutdown()
            except Exception:
                logger.exception("Errore shutdown webhook server.")
        try:
            self._engine.shutdown()
        except Exception:
            logger.exception("Errore shutdown MT5.")
        logger.info("Master bot arrestato.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    bot = MasterBot()
    try:
        bot.start()
    except MT5ConnectionError as exc:
        logger.critical("Connessione MT5 fallita: %s", exc)
        raise SystemExit(1)
    except Exception:
        logger.exception("Errore fatale.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
