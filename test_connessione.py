"""
test_connessione.py
===================
Script di test standalone per verificare:
    1. Connessione a MetaTrader 5
    2. Disponibilita' simboli (XAUUSD, EURUSD, GBPUSD)
    3. Caricamento e sintassi di tutti i moduli SMC
    4. Esecuzione di un'analisi SMC di prova (senza piazzare trade!)

NON invia ordini reali. Solo verifica connessione e analisi.

Eseguire da terminale:
    cd C:/Users/giova/Desktop/studio/bot_smc
    python test_connessione.py
"""

from __future__ import annotations

import logging
import sys
import traceback

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("test")


def test_imports() -> bool:
    """Verifica che tutti i moduli siano importabili."""
    logger.info("=" * 60)
    logger.info("TEST 1: Import moduli")
    logger.info("=" * 60)

    modules = [
        ("MetaTrader5", "MetaTrader5 (MT5)"),
        ("numpy", "NumPy"),
        ("pandas", "Pandas"),
        ("flask", "Flask (webhook)"),
        ("requests", "Requests (Telegram)"),
        ("config", "config.py"),
        ("structure_analyzer", "structure_analyzer.py"),
        ("smc_engine", "smc_engine.py"),
        ("risk_manager", "risk_manager.py"),
        ("telegram_notifier", "telegram_notifier.py"),
        ("market_data", "market_data.py"),
        ("position_monitor", "position_monitor.py"),
    ]

    all_ok = True
    for module, label in modules:
        try:
            __import__(module)
            logger.info("  ✅ %s", label)
        except Exception as e:
            logger.error("  ❌ %s: %s", label, e)
            all_ok = False

    # Verifica funzioni specifiche
    try:
        from telegram_notifier import build_notifier_from_config
        notifier = build_notifier_from_config()
        logger.info("  ✅ build_notifier_from_config() -> %s", type(notifier).__name__ if notifier else "None (Telegram non configurato)")
    except Exception as e:
        logger.warning("  ⚠️  build_notifier_from_config: %s", e)

    try:
        from structure_analyzer import analyze_symbol, get_trend_direction, get_fibonacci_zone
        logger.info("  ✅ structure_analyzer: analyze_symbol, get_trend_direction, get_fibonacci_zone")
    except Exception as e:
        logger.error("  ❌ structure_analyzer functions: %s", e)
        all_ok = False

    return all_ok


def test_mt5_connection() -> dict | None:
    """Verifica la connessione a MT5."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("TEST 2: Connessione MetaTrader 5")
    logger.info("=" * 60)

    try:
        from mt5_adapter import mt5

        # Tenta aggancio al terminale gia' aperto
        ok = mt5.initialize()
        if not ok:
            code, msg = mt5.last_error()
            logger.error("  ❌ MT5 initialize() fallita: (%s, '%s')", code, msg)
            logger.error("     Assicurati che MetaTrader 5 sia APERTO e loggato sul conto.")
            return None

        logger.info("  ✅ MT5 initialize() OK")

        account = mt5.account_info()
        if account is None:
            logger.error("  ❌ account_info() = None. Verifica login manuale su MT5.")
            mt5.shutdown()
            return None

        logger.info("  ✅ Account: login=%s | server=%s", account.login, account.server)
        logger.info("     Balance: %.2f | Equity: %.2f | Margin free: %.2f",
                    account.balance, account.equity, account.margin_free)

        info = {
            "login": account.login,
            "server": account.server,
            "balance": account.balance,
            "equity": account.equity,
            "margin_free": account.margin_free,
        }
        return info

    except Exception as e:
        logger.error("  ❌ Errore MT5: %s", e)
        traceback.print_exc()
        return None


def test_symbols(desired_symbols: list[str] = None) -> bool:
    """Verifica che i simboli siano disponibili."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("TEST 3: Disponibilita' simboli")
    logger.info("=" * 60)

    if desired_symbols is None:
        desired_symbols = ["XAUUSD", "EURUSD", "GBPUSD"]

    from mt5_adapter import mt5
    all_ok = True

    for sym in desired_symbols:
        info = mt5.symbol_info(sym)
        if info is None:
            logger.error("  ❌ %s: NON disponibile nel Market Watch", sym)
            all_ok = False
        else:
            logger.info("  ✅ %-10s | digits=%d | spread=%d | stops_level=%d",
                       sym, info.digits, info.spread,
                       getattr(info, 'stops_level', 0))
            # Verifica tick
            tick = mt5.symbol_info_tick(sym)
            if tick:
                spread_pips = (tick.ask - tick.bid) / (0.01 if "XAU" in sym else 0.0001)
                logger.info("     Bid: %.5f | Ask: %.5f | Spread: %.1f pips",
                           tick.bid, tick.ask, spread_pips)

    return all_ok


def test_smc_analysis() -> bool:
    """Esegue un'analisi SMC di prova su XAUUSD H4."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("TEST 4: Analisi SMC su XAUUSD H4")
    logger.info("=" * 60)

    try:
        from mt5_adapter import mt5
        import structure_analyzer as sa

        result = sa.analyze_symbol("XAUUSD", mt5.TIMEFRAME_H4, bars=200, pivot_window=3)

        if not result["success"]:
            logger.error("  ❌ Analisi fallita: %s", result.get("error"))
            return False

        logger.info("  ✅ Analisi completata")
        logger.info("     Trend: %s", result["trend"])
        logger.info("     Swings trovati: %s", result["swings_count"])
        logger.info("     Order Blocks validi: %s", result["obs_count"])
        logger.info("     Prezzo corrente: %s", result.get("current_price"))
        logger.info("     Segnali generati: %s", len(result.get("signals", [])))

        if result["signals"]:
            logger.info("     --- SEGNALI ---")
            for i, sig in enumerate(result["signals"], 1):
                logger.info("     [%d] %s | entry=%.5f sl=%.5f tp1=%.5f | rr=%s | %s | %s",
                           i, sig["direction"].upper(), sig["entry"], sig["sl"],
                           sig["tp1"], sig["rr"], sig["setup_type"], sig["probability"])

        # Test anche M15
        result_m15 = sa.analyze_symbol("XAUUSD", mt5.TIMEFRAME_M15, bars=200, pivot_window=4)
        if result_m15["success"]:
            logger.info("")
            logger.info("  ✅ Analisi M15: trend=%s | swings=%s | OB=%s | segnali=%s",
                       result_m15["trend"], result_m15["swings_count"],
                       result_m15["obs_count"], len(result_m15.get("signals", [])))

        return True

    except Exception as e:
        logger.error("  ❌ Errore analisi SMC: %s", e)
        traceback.print_exc()
        return False


def test_dati_storici() -> bool:
    """Verifica che i dati storici siano disponibili."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("TEST 5: Dati storici XAUUSD")
    logger.info("=" * 60)

    try:
        from mt5_adapter import mt5

        for tf_name, tf_val in [("M5", mt5.TIMEFRAME_M5), ("M15", mt5.TIMEFRAME_M15),
                                 ("H1", mt5.TIMEFRAME_H1), ("H4", mt5.TIMEFRAME_H4),
                                 ("D1", mt5.TIMEFRAME_D1)]:
            rates = mt5.copy_rates_from_pos("XAUUSD", tf_val, 0, 10)
            if rates is None or len(rates) == 0:
                logger.warning("  ⚠️  %s: nessun dato (forse non nel Market Watch)", tf_name)
            else:
                last_time = pd.to_datetime(rates[-1]["time"], unit="s")
                logger.info("  ✅ %-4s: %3d candele | ultima: %s | close=%.2f",
                           tf_name, len(rates), last_time, rates[-1]["close"])

        return True
    except Exception as e:
        logger.error("  ❌ Errore dati storici: %s", e)
        return False


# ======================================================================
# MAIN
# ======================================================================

def main():
    logger.info("")
    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║   TEST CONNESSIONE SMC BOT - SOLO VERIFICA, NO TRADE    ║")
    logger.info("╚══════════════════════════════════════════════════════════╝")
    logger.info("")

    results = {}

    # Test 1: Import moduli
    results["imports"] = test_imports()

    # Test 2: Connessione MT5
    mt5_info = test_mt5_connection()
    results["mt5"] = mt5_info is not None

    if not results["mt5"]:
        logger.critical("")
        logger.critical("❌❌❌ MT5 NON DISPONIBILE ❌❌❌")
        logger.critical("Apri MetaTrader 5, fai login sul conto e riprova.")
        logger.critical("Il terminale MT5 deve essere APERTO e FUNZIONANTE.")
        sys.exit(1)

    # Test 3: Simboli
    results["symbols"] = test_symbols(["XAUUSD"])

    # Test 4: Analisi SMC
    results["smc"] = test_smc_analysis()

    # Test 5: Dati storici
    results["data"] = test_dati_storici()

    # Pulizia
    from mt5_adapter import mt5
    mt5.shutdown()

    # Riepilogo
    logger.info("")
    logger.info("=" * 60)
    logger.info("RIEPILOGO FINALE")
    logger.info("=" * 60)
    all_pass = True
    for name, passed in results.items():
        icon = "✅" if passed else "❌"
        logger.info("  %s %s: %s", icon, name.upper(), "OK" if passed else "FALLITO")
        if not passed:
            all_pass = False

    logger.info("")
    if all_pass:
        logger.info("🎉 TUTTI I TEST SUPERATI! Il bot e' pronto per l'esecuzione.")
        logger.info("   Esegui: python main.py")
    else:
        logger.warning("⚠️  Alcuni test sono falliti. Controlla gli errori sopra.")
        logger.info("   Problemi comuni:")
        logger.info("   1. MetaTrader 5 non e' aperto/loggato")
        logger.info("   2. XAUUSD non e' nel Market Watch (tasto destro -> Show All)")
        logger.info("   3. Connessione internet assente")
        logger.info("   4. Dipendenze Python mancanti: pip install MetaTrader5 pandas numpy flask requests python-dotenv")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
