"""
market_data.py
==============
Recupero dati di mercato da MT5. Richiede che MT5 sia gia' inizializzato
(la sessione e' gestita da run_master.py o main.py).

NON fa init/shutdown autonomamente: usa la connessione persistente.

Nota: questa funzione e' un alias di structure_analyzer.get_market_data()
per retrocompatibilita' con script standalone e test.
"""

from __future__ import annotations

# Ri-esporta get_market_data da structure_analyzer (evita duplicati)
from structure_analyzer import get_market_data  # noqa: F401

__all__ = ["get_market_data"]


if __name__ == "__main__":
    from mt5_adapter import mt5

    if not mt5.initialize():
        print("Errore: terminale MT5 non disponibile.")
        exit(1)

    print("Scaricando dati XAUUSD H4...")
    df = get_market_data("XAUUSD", mt5.TIMEFRAME_H4, 100)
    if df is not None:
        print(df[["time", "open", "high", "low", "close"]].tail(5))

    mt5.shutdown()
