"""
mt5_adapter.py
==============
Adattatore unificato per MetaTrader 5: locale (Windows) o cloud (mt5linux su Linux).
Tutti i moduli del bot importano MT5 da qui invece che direttamente.

Uso:
    from mt5_adapter import mt5
    mt5.initialize(...)
    mt5.order_send(...)

Configurazione (config.py / .env):
    MT5_BACKEND = "local"   → usa MetaTrader5 nativo (Windows)
    MT5_BACKEND = "mt5linux" → usa mt5linux (Docker Linux)
    MT5_HOST = "localhost"   → host del container MT5 (solo mt5linux)
    MT5_PORT = 18812         → porta del bridge MQL5 (solo mt5linux)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger("mt5_adapter")

# ---------------------------------------------------------------------------
# Rilevamento backend
# ---------------------------------------------------------------------------
_backend = "local"
_mt5_linux_host = "localhost"
_mt5_linux_port = 18812

try:
    import config  # noqa: E402
    _configured = getattr(config, "MT5_BACKEND", "local").lower()
    if _configured in ("local", "mt5linux"):
        _backend = _configured
    _mt5_linux_host = getattr(config, "MT5_HOST", "localhost")
    _mt5_linux_port = getattr(config, "MT5_PORT", 18812)
except Exception:
    pass


# ---------------------------------------------------------------------------
# Costanti MT5 (valori standard, immutabili tra versioni)
# ---------------------------------------------------------------------------
# Su mt5linux l'istanza potrebbe non esporre le costanti di modulo.
# Le iniettiamo come attributi dell'oggetto per compatibilità totale.
_MT5_CONSTANTS: dict[str, Any] = {
    # Timeframe
    "TIMEFRAME_M1": 1,
    "TIMEFRAME_M5": 5,
    "TIMEFRAME_M15": 15,
    "TIMEFRAME_M30": 30,
    "TIMEFRAME_H1": 16385,
    "TIMEFRAME_H4": 16388,
    "TIMEFRAME_D1": 16408,
    "TIMEFRAME_W1": 32769,
    "TIMEFRAME_MN1": 49153,
    # Position types
    "POSITION_TYPE_BUY": 0,
    "POSITION_TYPE_SELL": 1,
    # Trade actions
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_PENDING": 5,
    "TRADE_ACTION_SLTP": 6,
    "TRADE_ACTION_MODIFY": 7,
    "TRADE_ACTION_REMOVE": 8,
    # Order types
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "ORDER_TYPE_BUY_LIMIT": 2,
    "ORDER_TYPE_SELL_LIMIT": 3,
    "ORDER_TYPE_BUY_STOP": 4,
    "ORDER_TYPE_SELL_STOP": 5,
    # Order filling
    "ORDER_FILLING_FOK": 0,
    "ORDER_FILLING_IOC": 1,
    "ORDER_FILLING_RETURN": 2,
    # Order time
    "ORDER_TIME_GTC": 0,
    "ORDER_TIME_DAY": 1,
    "ORDER_TIME_SPECIFIED": 2,
    "ORDER_TIME_SPECIFIED_DAY": 3,
    # Return codes
    "TRADE_RETCODE_DONE": 10009,
    "TRADE_RETCODE_DONE_PARTIAL": 10010,
    "TRADE_RETCODE_REJECT": 10011,
    "TRADE_RETCODE_CANCEL": 10012,
    "TRADE_RETCODE_PLACED": 10013,
    "TRADE_RETCODE_INVALID_FILL": 10030,
    "TRADE_RETCODE_INVALID_VOLUME": 10014,
    "TRADE_RETCODE_NO_MONEY": 10019,
    "TRADE_RETCODE_MARKET_CLOSED": 10018,
    # Symbol filling flags
    "SYMBOL_FILLING_FOK": 1,
    "SYMBOL_FILLING_IOC": 2,
    # Deal entry (usate da webhook_server per lo storico posizioni)
    "DEAL_ENTRY_IN": 0,
    "DEAL_ENTRY_OUT": 1,
    "DEAL_ENTRY_INOUT": 2,
    "DEAL_ENTRY_OUT_BY": 3,
    # Deal types
    "DEAL_TYPE_BUY": 0,
    "DEAL_TYPE_SELL": 1,
}


def _inject_constants(obj: Any) -> None:
    """Inietta le costanti MT5 come attributi dell'oggetto."""
    for name, value in _MT5_CONSTANTS.items():
        setattr(obj, name, value)


# ---------------------------------------------------------------------------
# Import condizionale
# ---------------------------------------------------------------------------
if _backend == "mt5linux":
    try:
        from mt5linux import MetaTrader5 as _raw_mt5  # type: ignore[import-untyped]
        mt5 = _raw_mt5(host=_mt5_linux_host, port=_mt5_linux_port)
        _inject_constants(mt5)
        logger.info(
            "MT5 Adapter: backend mt5linux attivo (host=%s port=%s) + %d costanti iniettate",
            _mt5_linux_host, _mt5_linux_port, len(_MT5_CONSTANTS),
        )
    except ImportError:
        logger.critical(
            "MT5_BACKEND=mt5linux ma 'mt5linux' non installato. "
            "Installa con: pip install mt5linux"
        )
        sys.exit(1)
else:
    # Windows / locale: MetaTrader5 nativo (le costanti sono già nel modulo)
    import MetaTrader5 as mt5  # type: ignore[import-untyped]
    logger.info("MT5 Adapter: backend locale (MetaTrader5 nativo)")

__all__ = ["mt5"]
