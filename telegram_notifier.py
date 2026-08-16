"""
telegram_notifier.py
====================
Notifiche Telegram per il bot di trading.

Fornisce:
    - TelegramNotifier: client iniettabile e compatibile con il webhook
    - build_notifier_from_config(): factory che crea un callable per inviare messaggi
    - send_telegram_message(): funzione standalone per invio diretto
    - TelegramCommandListener: thread che ascolta comandi in arrivo (es. /status)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import requests

import config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Client Telegram iniettabile, usabile anche come callable notifier."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        session: Optional[object] = None,
        timeout: float = 10,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.session = session or requests
        self.timeout = timeout

    @property
    def url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/sendMessage"

    def __call__(self, text: str) -> bool:
        return self.send(text)

    def send(self, text: str) -> bool:
        """Invia un messaggio; la sessione può essere un mock nei test."""
        try:
            response = self.session.post(
                self.url,
                json={"chat_id": self.chat_id, "text": str(text)},
                timeout=self.timeout,
            )
            status_code = getattr(response, "status_code", 0)
            ok = bool(getattr(response, "ok", 200 <= status_code < 300))
            if not ok:
                logger.warning(
                    "Telegram API error [%s]: %s %s",
                    self.chat_id,
                    status_code,
                    str(getattr(response, "text", ""))[:200],
                )
            return ok
        except Exception as exc:
            logger.warning("Errore invio Telegram a %s: %s", self.chat_id, exc)
            return False

    def notify_execution(self, text: str, *args: object, **kwargs: object) -> bool:
        """Alias compatibile con i vecchi test/chiamanti del notifier."""
        return self.send(text)


def build_notifier_from_config() -> Optional[Callable[[str], None]]:
    """Costruisce un notificatore Telegram usando i parametri di config.

    Invia a TUTTI i chat ID configurati (TELEGRAM_CHAT_IDS).

    Returns:
        Callable che accetta una stringa e invia un messaggio Telegram,
        oppure None se token o chat_id non sono configurati.
    """
    token = config.TELEGRAM_BOT_TOKEN
    chat_ids = config.TELEGRAM_CHAT_IDS

    if not token or not chat_ids:
        logger.warning(
            "Telegram non configurato: TELEGRAM_BOT_TOKEN=%s, chat_ids=%s",
            bool(token), len(chat_ids),
        )
        return None

    def _send(text: str) -> None:
        """Invia un messaggio a tutti i chat ID configurati."""
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for cid in chat_ids:
            try:
                resp = requests.post(
                    url,
                    json={"chat_id": cid, "text": text},
                    timeout=10,
                )
                if not resp.ok:
                    logger.warning("Telegram API error [%s]: %s %s", cid, resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("Errore invio Telegram a %s: %s", cid, e)

    return _send


def send_telegram_message(text: str) -> bool:
    """Invia un messaggio Telegram standalone. Richiede config.

    Returns:
        True se inviato con successo, False altrimenti.
    """
    notifier = build_notifier_from_config()
    if notifier is None:
        return False
    try:
        notifier(text)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Alert automatici su ERROR/CRITICAL (nuova tipologia)
# ---------------------------------------------------------------------------

class TelegramAlertHandler(logging.Handler):
    """Handler di logging che inoltra i record ERROR/CRITICAL come alert Telegram.

    Serve per notificare subito problemi operativi (ordini rifiutati, margine
    insufficiente, errori di scansione) anche con la console silenziosa.

    Anti-spam: una notifica per combinazione (logger, messaggio) ogni
    ``throttle_min`` minuti; i record ripetuti ravvicinati vengono ignorati.
    Un errore di notifica non blocca mai il logging.
    """

    def __init__(self, notifier: Callable[[str], None], throttle_min: int = 5) -> None:
        super().__init__(level=logging.ERROR)
        self._notifier = notifier
        self._throttle = float(max(int(throttle_min), 1) * 60)
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            key = f"{record.name}:{msg[:80]}"
            now = time.time()
            with self._lock:
                if now - self._last_sent.get(key, 0.0) < self._throttle:
                    return
                self._last_sent[key] = now
                # Potatura: evita crescita illimitata del dict anti-spam
                if len(self._last_sent) > 200:
                    cutoff = now - 3600  # rimuovi le voci piu' vecchie di 1 ora
                    self._last_sent = {k: v for k, v in self._last_sent.items() if v > cutoff}

            is_critical = record.levelno >= logging.CRITICAL
            emoji = "🟥" if is_critical else "🟧"
            text = (
                f"{emoji} ALERT {record.levelname}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 {record.name}\n"
                f"{msg[:1200]}"
            )
            self._notifier(text)
        except Exception:
            pass  # un errore di notifica non deve mai bloccare il logging


# ---------------------------------------------------------------------------
# Listener per comandi in arrivo (polling getUpdates)
# ---------------------------------------------------------------------------

class TelegramCommandListener(threading.Thread):
    """Thread daemon che ascolta comandi Telegram via long-polling.

    Supporta:
        /status    -> richiama il callback on_status e invia la risposta
        /positions -> richiama il callback on_positions e invia la risposta
        /errors    -> richiama il callback on_errors e invia la risposta
        /help      -> invia la lista dei comandi

    Usage:
        listener = TelegramCommandListener(on_status=my_handler)
        listener.start()
        # ...
        listener.stop()
    """

    def __init__(
        self,
        on_status: Optional[Callable[[], str]] = None,
        on_positions: Optional[Callable[[], str]] = None,
        on_errors: Optional[Callable[[], str]] = None,
        poll_interval: Optional[int] = None,
    ) -> None:
        super().__init__(name="telegram-command-listener", daemon=True)
        self._token = config.TELEGRAM_BOT_TOKEN
        self._authorized_ids: set[str] = set(config.TELEGRAM_CHAT_IDS)
        self._on_status = on_status
        self._on_positions = on_positions
        self._on_errors = on_errors
        self._poll_interval = poll_interval or config.TELEGRAM_COMMAND_POLLING_SECONDS
        self._stop_event = threading.Event()
        self._offset: int = 0  # offset per getUpdates (confirma messaggi processati)
        self._api_base = f"https://api.telegram.org/bot{self._token}" if self._token else ""
        # Debounce errori: logga solo il primo errore o dopo 5 minuti di silenzio
        self._error_count: int = 0
        self._last_error_time: float = 0.0
        self._error_logged: bool = False

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not self._token or not self._authorized_ids:
            logger.warning("TelegramCommandListener: token/chat_id mancanti, listener disattivato.")
            return

        logger.info("TelegramCommandListener avviato (polling ogni %ss, %d utenti autorizzati).",
                    self._poll_interval, len(self._authorized_ids))

        while not self._stop_event.is_set():
            try:
                self._poll_once()
                # Poll riuscito: reset contatore errori
                self._error_count = 0
                self._error_logged = False
            except Exception as e:
                self._error_count += 1
                now = time.time()
                # Logga solo il primo errore o ogni 5 minuti
                if not self._error_logged or (now - self._last_error_time) > 300:
                    logger.warning(
                        "TelegramCommandListener errore polling (#%d in %.0fs): %s",
                        self._error_count, now - self._last_error_time if self._last_error_time > 0 else 0, e,
                    )
                    self._error_logged = True
                    self._last_error_time = now
                else:
                    logger.debug("Telegram polling silenzioso: errore #%d", self._error_count)
                # Backoff crescente: 10s base + 5s per ogni errore (max 60s)
                backoff = min(10 + self._error_count * 5, 60)
                self._stop_event.wait(timeout=backoff)
                continue
            # Attendi tra un poll e l'altro (interruptible)
            self._stop_event.wait(timeout=self._poll_interval)

        logger.info("TelegramCommandListener arrestato.")

    def _poll_once(self) -> None:
        """Esegue un singolo ciclo di getUpdates e processa i messaggi."""
        url = f"{self._api_base}/getUpdates"
        params = {
            "offset": self._offset,
            "timeout": 25,  # long-polling: resta in attesa fino a 25s
            "allowed_updates": '["message"]',
        }
        resp = requests.get(url, params=params, timeout=35)
        if not resp.ok:
            logger.warning("getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
            return

        data = resp.json()
        if not data.get("ok"):
            logger.warning("getUpdates not ok: %s", str(data)[:200])
            return

        updates = data.get("result", [])
        for update in updates:
            self._offset = update["update_id"] + 1
            self._process_update(update)

    def _process_update(self, update: dict) -> None:
        """Processa un singolo update Telegram."""
        message = update.get("message")
        if not message:
            return

        text = (message.get("text") or "").strip()
        from_chat = str(message.get("chat", {}).get("id", ""))

        # Sicurezza: accetta comandi solo da chat autorizzate
        if from_chat not in self._authorized_ids:
            logger.info("TelegramCommandListener: messaggio da chat non autorizzata (%s), ignoro.",
                        from_chat)
            return

        if not text.startswith("/"):
            return  # ignora messaggi non-comando

        command = text.lower().split()[0]  # es. "/status" o "/status@botname"
        # Rimuovi eventuale suffisso @botname
        command = command.split("@")[0]

        logger.info("TelegramCommandListener: comando '%s' da %s.", command, from_chat)

        if command == "/start":
            self._handle_start(from_chat)
        elif command == "/status":
            self._handle_status(from_chat)
        elif command == "/positions":
            self._handle_positions(from_chat)
        elif command == "/errors":
            self._handle_errors(from_chat)
        elif command == "/help":
            self._handle_help(from_chat)
        else:
            self._send_reply(f"Comando sconosciuto: {command}\nUsa /help per la lista comandi.", from_chat)

    def _handle_status(self, from_chat: str) -> None:
        """Gestisce /status: chiama il callback e invia la risposta al mittente."""
        if self._on_status:
            try:
                reply = self._on_status()
            except Exception as e:
                reply = f"Errore generazione status: {e}"
                logger.exception("Errore callback on_status: %s", e)
        else:
            reply = "Status non disponibile (nessun handler configurato)."
        self._send_reply(reply, from_chat)

    def _handle_positions(self, from_chat: str) -> None:
        """Gestisce /positions: chiama il callback e invia la risposta al mittente."""
        if self._on_positions:
            try:
                reply = self._on_positions()
            except Exception as e:
                reply = f"Errore generazione posizioni: {e}"
                logger.exception("Errore callback on_positions: %s", e)
        else:
            reply = "Posizioni non disponibili (nessun handler configurato)."
        self._send_reply(reply, from_chat)

    def _handle_errors(self, from_chat: str) -> None:
        """Gestisce /errors: invia gli ultimi errori dal file di log."""
        if self._on_errors:
            try:
                reply = self._on_errors()
            except Exception as e:
                reply = f"Errore generazione report errori: {e}"
                logger.exception("Errore callback on_errors: %s", e)
        else:
            reply = "Report errori non disponibile (nessun handler configurato)."
        self._send_reply(reply, from_chat)

    def _handle_start(self, from_chat: str) -> None:
        """Gestisce /start: invia il messaggio di benvenuto al mittente."""
        welcome_text = (
            "🤖 *BENVENUTO NEL BOT SMC!*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Riceverai in tempo reale tutte le operazioni del bot.\n\n"
            "📊 *Cosa fa questo bot*\n"
            "• Analizza XAUUSD, USDJPY, GBPUSD, EURUSD\n"
            "• Cerca setup SMC su H4 + M15 + M1\n"
            "• Piazza 1 ordine con il 100% del lotto e TP runner lontano\n"
            "• Gestisce chiusure parziali 30/30/40, break-even e trailing stop\n"
            "• Invia notifiche su ogni trade eseguito\n\n"
            "📋 *Comandi disponibili*\n"
            "/status  - Stato live del bot\n"
            "/positions - Posizioni aperte in tempo reale\n"
            "/errors  - Ultimi errori dal log\n"
            "/help    - Lista comandi\n"
            "/start   - Questo messaggio\n\n"
            "Buon trading! 🚀"
        )
        self._send_reply(welcome_text, from_chat)

    def _handle_help(self, from_chat: str) -> None:
        """Gestisce /help: invia la lista dei comandi al mittente."""
        help_text = (
            "🤖 *COMANDI BOT SMC*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/status    - Stato live: posizioni, equity, OB\n"
            "/positions - Solo posizioni aperte in tempo reale\n"
            "/errors    - Ultimi errori dal log\n"
            "/help      - Mostra questa lista comandi\n"
            "/start     - Messaggio di benvenuto\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Il bot analizza XAUUSD ogni 2s (H4+M15+M1)\n"
            "e piazza ordini solo in sessione Londra/NY."
        )
        self._send_reply(help_text, from_chat)

    def _send_reply(self, text: str, chat_id: str) -> None:
        """Invia un messaggio di risposta alla chat specifica.

        Prova prima con parse_mode Markdown (per i bold di /start e /help);
        se Telegram rifiuta (400 = caratteri Markdown non escapati, es. "*"
        o "_" nei report di /status), riprova in testo puro: un report
        deve arrivare SEMPRE, la formattazione e' opzionale.
        """
        url = f"{self._api_base}/sendMessage"
        payloads = (
            [{"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
             {"chat_id": chat_id, "text": text}]
            if text.strip().startswith("/") or any(c in text for c in ("*", "_", "[", "]"))
            else [{"chat_id": chat_id, "text": text}]
        )
        for payload in payloads:
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.ok:
                    return
                # 400 con parse_mode Markdown: riprova in testo puro
                if resp.status_code == 400 and "parse_mode" in payload:
                    continue
                logger.warning(
                    "Telegram reply error [%s]: %s %s",
                    chat_id, resp.status_code, resp.text[:200],
                )
                return
            except Exception as e:
                logger.warning("Errore invio reply Telegram a %s: %s", chat_id, e)
                return