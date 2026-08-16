"""
test_notifiche.py
=================
MODULO RIUTILIZZABILE per analisi SMC + esecuzione trade reale su MT5 + notifica Telegram.
Può essere:
  - Eseguito standalone: python test_notifiche.py
  - Importato da altri moduli: from test_notifiche import analizza_e_trada

Esempio d'uso:
  >>> from test_notifiche import analizza_e_trada
  >>> risultato = analizza_e_trada("XAUUSD", lotto_fisso=0.01)
  >>> if risultato["eseguito"]:
  ...     print(f"Trade aperto! Ticket: #{risultato['ticket']}")

Ogni chiamata:
  1. Si connette a MT5 (se non già connesso)
  2. Analizza H4 + M15
  3. Se trova setup valido, calcola lotto e piazza ordine con SL/TP
  4. Invia notifica Telegram
  5. Restituisce risultato strutturato
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# Configura logging minimo
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("test_notifiche")

# ============================================================================
# Import modulo MT5 (pigro: importabile anche senza terminale)
# ============================================================================
_mt5 = None

def _get_mt5():
    global _mt5
    if _mt5 is None:
        from mt5_adapter import mt5
        _mt5 = mt5
    return _mt5


# ============================================================================
# Risultato dell'operazione
# ============================================================================

@dataclass
class TradeResult:
    """Risultato completo di un'operazione di trading."""
    eseguito: bool = False
    ticket: Optional[int] = None
    simbolo: str = ""
    direzione: str = ""
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    volume: float = 0.0
    prezzo_esecuzione: float = 0.0
    retcode: int = -1
    retcode_desc: str = ""
    rr: float = 0.0
    analisi: dict = field(default_factory=dict)
    errore: Optional[str] = None


# ============================================================================
# Connessione MT5 (condivisa tra chiamate)
# ============================================================================

_mt5_inizializzato = False

def _connetti_mt5() -> bool:
    """Connette a MT5 una sola volta (riutilizzabile tra chiamate successive)."""
    global _mt5_inizializzato
    if _mt5_inizializzato:
        return True

    mt5 = _get_mt5()
    if not mt5.initialize():
        err = mt5.last_error()
        logger.error(f"[ERRORE] MT5 non disponibile: {err}")
        return False

    info = mt5.account_info()
    if info:
        logger.info(f"[OK] MT5 connesso | Account: {info.login} | Server: {info.server} | Balance: {info.balance:.2f} USD")
        _mt5_inizializzato = True
        return True
    else:
        logger.error("[ERRORE] MT5 inizializzato ma nessun account loggato")
        return False


def _disconnetti_mt5():
    """Chiude MT5 (chiamare alla fine del programma)."""
    global _mt5_inizializzato
    if _mt5_inizializzato:
        mt5 = _get_mt5()
        mt5.shutdown()
        _mt5_inizializzato = False
        logger.info("[OK] MT5 disconnesso")


# ============================================================================
# Funzioni di analisi SMC
# ============================================================================

def _import_moduli_smc():
    """Importa i moduli SMC (import ritardato per evitare dipendenze circolari)."""
    import smc_engine as smc
    import structure_analyzer as sa
    import smc_signals as sig
    return smc, sa, sig


def analisi_completa(simbolo: str = "XAUUSD") -> dict:
    """Esegue analisi SMC completa multi-timeframe.

    Args:
        simbolo: Simbolo da analizzare (default XAUUSD)

    Returns:
        Dizionario con analisi H4, M15, DXY, sessione, e trade suggerito.
    """
    smc, sa, sig = _import_moduli_smc()
    mt5 = _get_mt5()

    if not _connetti_mt5():
        return {"errore": "MT5 non connesso"}

    now = datetime.now(timezone.utc)
    risultato = {
        "timestamp": now.isoformat(),
        "sessione": smc.get_current_session(),
        "simbolo": simbolo,
        "h4": {},
        "m15": {},
        "dxy": None,
        "trade_suggerito": None,
        "struttura_h4": [],
        "errore": None,
    }

    try:
        # --- H4 (HTF - Direzione) ---
        df_h4 = sa.get_market_data(simbolo, mt5.TIMEFRAME_H4, bars=200)
        if df_h4 is None or df_h4.empty:
            risultato["errore"] = "Nessun dato H4"
            return risultato

        current_price = float(df_h4["close"].iloc[-1])
        risultato["prezzo_corrente"] = current_price

        df_h4 = sa.identify_swings(df_h4, window=3)
        swings_h4 = sa.filter_alternating_swings(df_h4)
        swings_h4 = sa.label_structure(swings_h4)
        swings_h4 = sa.classify_strong_weak(swings_h4)
        swings_h4 = sa.detect_structure_breaks(swings_h4)
        trend_h4 = sa.get_trend_direction(swings_h4)

        # Estrai ultimi swing per debug
        ultimi_swing = []
        for i in range(max(0, len(swings_h4) - 10), len(swings_h4)):
            r = swings_h4.iloc[i]
            ultimi_swing.append({
                "label": r["label"],
                "prezzo": float(r["price_level"]),
                "forza": str(r.get("strength", "")),
                "evento": str(r.get("structure_event", "")),
            })
        risultato["struttura_h4"] = ultimi_swing

        # Range PD
        labeled_h4 = swings_h4[swings_h4["label"] != ""]
        hh_price = None
        ll_price = None
        if not labeled_h4.empty:
            hh_data = labeled_h4[labeled_h4["label"] == "HH"]
            ll_data = labeled_h4[labeled_h4["label"] == "LL"]
            if not hh_data.empty:
                hh_price = float(hh_data["price_level"].iloc[-1])
            if not ll_data.empty:
                ll_price = float(ll_data["price_level"].iloc[-1])

        equilibrium = None
        if hh_price and ll_price:
            equilibrium = (hh_price + ll_price) / 2

        # Trova ultimo HL (per SL) e ultimo HH (per TP)
        ultimo_hl = None
        ultimo_hh = None
        for i in range(len(swings_h4) - 1, -1, -1):
            r = swings_h4.iloc[i]
            if r["label"] == "HL" and ultimo_hl is None:
                ultimo_hl = float(r["price_level"])
            if r["label"] == "HH" and ultimo_hh is None:
                ultimo_hh = float(r["price_level"])

        risultato["h4"] = {
            "trend": trend_h4,
            "prezzo": current_price,
            "equilibrium": equilibrium,
            "hh": hh_price,
            "ll": ll_price,
            "ultimo_hl": ultimo_hl,
            "ultimo_hh": ultimo_hh,
            "zona": "DISCOUNT" if equilibrium and current_price < equilibrium else "PREMIUM" if equilibrium else "EQUILIBRIUM",
        }

        # --- M15 (MTF - Struttura minore) ---
        df_m15 = sa.get_market_data(simbolo, mt5.TIMEFRAME_M15, bars=200)
        if df_m15 is not None and not df_m15.empty:
            df_m15 = sa.identify_swings(df_m15, window=4)
            swings_m15 = sa.filter_alternating_swings(df_m15)
            swings_m15 = sa.label_structure(swings_m15)
            swings_m15 = sa.classify_strong_weak(swings_m15)
            swings_m15 = sa.detect_structure_breaks(swings_m15)
            trend_m15 = sa.get_trend_direction(swings_m15)

            risultato["m15"] = {
                "trend": trend_m15,
                "prezzo": float(df_m15["close"].iloc[-1]),
            }

        # --- DXY ---
        dxy = smc.get_dxy_bias()
        if dxy:
            risultato["dxy"] = {
                "prezzo": dxy["current_price"],
                "trend": dxy["trend"],
                "bias": dxy["bias"],
            }

        # --- Trade suggerito (SMC logic) ---
        if trend_h4 != "sideways" and ultimo_hl and ultimo_hh and equilibrium:
            if trend_h4 == "bullish" and current_price < equilibrium:
                # Uptrend + Discount → BUY
                tick = mt5.symbol_info_tick(simbolo)
                entry = round(float(tick.ask if tick else current_price), 2)
                sl = round(ultimo_hl - 2.0, 2)  # sotto l'HL protetto
                tp1 = round(equilibrium, 2)      # primo target: equilibrio
                tp2 = round(ultimo_hh, 2)        # secondo target: HH (liquidità opposta)
                risk = abs(entry - sl)
                reward1 = abs(tp1 - entry)
                reward2 = abs(tp2 - entry)
                rr1 = round(reward1 / risk, 2) if risk > 0 else 0
                rr2 = round(reward2 / risk, 2) if risk > 0 else 0

                risultato["trade_suggerito"] = {
                    "direzione": "BUY",
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "rr1": rr1,
                    "rr2": rr2,
                    "logica": "Uptrend H4 + Discount + vicino HL protetto",
                    "probabilita": "MEDIA" if not risultato.get("sweep") else "ALTA",
                }

            elif trend_h4 == "bearish" and current_price > equilibrium:
                # Downtrend + Premium → SELL
                tick = mt5.symbol_info_tick(simbolo)
                entry = round(float(tick.bid if tick else current_price), 2)
                sl = round(ultimo_hh + 2.0, 2)   # sopra l'LH protetto
                tp1 = round(equilibrium, 2)       # primo target: equilibrio
                tp2 = round(ultimo_hl, 2)         # secondo target: LL (liquidità opposta)
                risk = abs(sl - entry)
                reward1 = abs(entry - tp1)
                reward2 = abs(entry - tp2)
                rr1 = round(reward1 / risk, 2) if risk > 0 else 0
                rr2 = round(reward2 / risk, 2) if risk > 0 else 0

                risultato["trade_suggerito"] = {
                    "direzione": "SELL",
                    "entry": entry,
                    "sl": sl,
                    "tp1": tp1,
                    "tp2": tp2,
                    "rr1": rr1,
                    "rr2": rr2,
                    "logica": "Downtrend H4 + Premium + vicino LH protetto",
                    "probabilita": "MEDIA",
                }

    except Exception as e:
        logger.exception(f"Errore durante l'analisi: {e}")
        risultato["errore"] = str(e)

    return risultato


# ============================================================================
# Esecuzione trade su MT5
# ============================================================================

def esegui_trade(
    simbolo: str = "XAUUSD",
    direzione: str = "BUY",
    entry: float = 0.0,
    sl: float = 0.0,
    tp: float = 0.0,
    lotto: float = 0.01,
) -> TradeResult:
    """Esegue un ordine reale su MT5 con SL e TP.

    Args:
        simbolo: Simbolo MT5 (es. XAUUSD)
        direzione: "BUY" o "SELL"
        entry: Prezzo di entrata (0 = market order)
        sl: Stop Loss
        tp: Take Profit
        lotto: Volume dell'ordine

    Returns:
        TradeResult con esito dell'operazione.
    """
    mt5 = _get_mt5()

    if not _connetti_mt5():
        return TradeResult(errore="MT5 non connesso")

    try:
        # Ottieni prezzi di mercato
        tick = mt5.symbol_info_tick(simbolo)
        if tick is None:
            return TradeResult(errore=f"Tick non disponibile per {simbolo}")

        # Risolvi entry: se non specificata, usa prezzo market
        if entry == 0.0:
            entry = float(tick.ask if direzione == "BUY" else tick.bid)

        # Calcola R:R
        rr = 0.0
        if sl and tp:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = round(reward / risk, 2) if risk > 0 else 0.0

        # Prepara la richiesta ordine
        prezzo_market = float(tick.ask if direzione == "BUY" else tick.bid)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": simbolo,
            "volume": lotto,
            "type": mt5.ORDER_TYPE_BUY if direzione == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": prezzo_market,
            "sl": float(sl) if sl else 0.0,
            "tp": float(tp) if tp else 0.0,
            "deviation": 20,
            "magic": 9999,
            "comment": "SMC_AUTO_TRADE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(f"Invio ordine {direzione} {lotto} {simbolo} @ {prezzo_market:.2f} "
                     f"SL={sl:.2f} TP={tp:.2f}")

        # Invia ordine
        response = mt5.order_send(request)

        if response is None:
            return TradeResult(
                errore=f"order_send() ha ritornato None: {mt5.last_error()}",
                entry=entry, sl=sl, tp=tp, volume=lotto,
                simbolo=simbolo, direzione=direzione,
            )

        if response.retcode == mt5.TRADE_RETCODE_DONE:
            result = TradeResult(
                eseguito=True,
                ticket=response.order,
                simbolo=simbolo,
                direzione=direzione,
                entry=entry,
                sl=sl,
                tp=tp,
                volume=lotto,
                prezzo_esecuzione=prezzo_market,
                retcode=response.retcode,
                retcode_desc="DONE",
                rr=rr,
            )
            logger.info(f"ORDINE ESEGUITO! Ticket: #{result.ticket}")
            logger.info(f"   Entry: {prezzo_market:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | R:R 1:{rr}")
            return result
        else:
            desc = _descrivi_retcode(response.retcode)
            logger.error(f"Ordine rifiutato: retcode={response.retcode} ({desc})")
            return TradeResult(
                errore=f"Ordine rifiutato: retcode={response.retcode} ({desc})",
                entry=entry, sl=sl, tp=tp, volume=lotto,
                simbolo=simbolo, direzione=direzione,
                retcode=response.retcode, retcode_desc=desc,
            )

    except Exception as e:
        logger.exception(f"Eccezione durante l'ordine: {e}")
        return TradeResult(
            errore=str(e), entry=entry, sl=sl, tp=tp,
            volume=lotto, simbolo=simbolo, direzione=direzione,
        )


def _descrivi_retcode(code: int) -> str:
    """Restituisce descrizione leggibile del retcode MT5."""
    descrizioni = {
        10004: "Prezzo non valido/invecchiato",
        10006: "Troppe richieste",
        10007: "Errore interno MT5",
        10008: "Symbol timeout",
        10009: "Volume non valido",
        10010: "Market non attivo",
        10011: "Trade non consentito",
        10012: "Tipo ordine non supportato",
        10013: "Posizione già chiusa",
        10014: "Volume fuori limite",
        10015: "Ordine non trovato",
        10016: "Mercato chiuso",
        10017: "Solo limite/FIFO",
        10018: "Divieto hedging",
        10019: "Ordine bloccato da server",
        10020: "Modifica rifiutata",
        10021: "Troppi ordini",
        10022: "Slippage eccessivo",
        10023: "Ordine in coda (frozen)",
        10024: "Margin call/stop out",
        10025: "Ordini pendenti bloccati",
        10026: "Ordini market bloccati",
        10027: "AutoTrading disabilitato",
        10028: "Expert Advisor bloccato",
        10029: "Nessun prezzo",
        10030: "Filling mode non supportato",
        10031: "Stale tick (prezzo vecchio)",
        -1: "Errore generico",
    }
    return descrizioni.get(code, f"Codice {code}")


# ============================================================================
# Notifica Telegram
# ============================================================================

def invia_notifica_trade(risultato: TradeResult) -> bool:
    """Invia notifica Telegram del trade eseguito."""
    try:
        from telegram_notifier import send_telegram_message

        if not risultato.eseguito:
            msg = (
                f"[ERRORE] *OPERAZIONE FALLITA*\n"
            f"{'-'*30}\n"
            f"{risultato.simbolo}: {risultato.direzione}\n"
            f"Errore: {risultato.errore or 'Sconosciuto'}\n"
            f"{'-'*30}"
            )
            ok = send_telegram_message(msg)
            logger.info(f"Notifica errore inviata: {ok}")
            return ok

        # Calcola pips
        mt5 = _get_mt5()
        pip = _get_pip_size(risultato.simbolo)
        risk_pips = abs(risultato.entry - risultato.sl) / pip if risultato.sl else 0
        reward_pips = abs(risultato.tp - risultato.entry) / pip if risultato.tp else 0

        segno = "+" if risultato.direzione == "BUY" else "-"

        msg = (
            f"[{segno}] *TRADE ESEGUITO*\n"
            f"{risultato.simbolo}: {risultato.direzione}\n"
            f"{'-'*30}\n"
            f"Ticket: #{risultato.ticket}\n"
            f"Volume: {risultato.volume}\n"
            f"Entry: {risultato.prezzo_esecuzione:.2f}\n"
            f"SL: {risultato.sl:.2f} ({risk_pips:.0f} pip)\n"
            f"TP: {risultato.tp:.2f} ({reward_pips:.0f} pip)\n"
            f"R:R: 1:{risultato.rr}\n"
            f"{'-'*30}\n"
            f"*SMC Trading Bot*"
        )

        ok = send_telegram_message(msg)
        logger.info(f"Notifica trade inviata: {ok}")
        return ok

    except Exception as e:
        logger.warning(f"Notifica Telegram fallita: {e}")
        return False


def invia_notifica_analisi(analisi: dict) -> bool:
    """Invia analisi di mercato via Telegram."""
    try:
        from telegram_notifier import send_telegram_message

        h4 = analisi.get("h4", {})
        trade = analisi.get("trade_suggerito")

        parte_trade = ""
        if trade:
            parte_trade = (
                f"\n[TRADE] {trade['direzione']}\n"
                f"Entry: {trade['entry']:.2f} | SL: {trade['sl']:.2f}\n"
                f"TP1: {trade['tp1']:.2f} | TP2: {trade['tp2']:.2f}\n"
                f"R:R: 1:{trade['rr1']} / 1:{trade['rr2']}\n"
                f"Logica: {trade['logica']}"
            )

        msg = (
            f"[ANALISI] *ANALISI SMC - {analisi['simbolo']}*\n"
            f"{'-'*30}\n"
            f"Ora: {analisi['timestamp'][:19].replace('T', ' ')} UTC\n"
            f"H4: {h4.get('trend', 'N/D').upper()}\n"
            f"Prezzo: {analisi.get('prezzo_corrente', 0):.2f}\n"
            f"Zona: {h4.get('zona', 'N/D')}\n"
            f"DXY: {analisi.get('dxy', {}).get('trend', 'N/D')}\n"
            f"{analisi.get('sessione', 'N/D').upper()}{parte_trade}\n"
            f"{'-'*30}\n"
            f"*SMC Trading Bot*"
        )

        ok = send_telegram_message(msg)
        logger.info(f"Analisi inviata: {ok}")
        return ok

    except Exception as e:
        logger.warning(f"Notifica analisi fallita: {e}")
        return False


# ============================================================================
# Funzione principale: ANALISI + TRADE + NOTIFICA (tutto in uno)
# ============================================================================

def analizza_e_trada(
    simbolo: str = "XAUUSD",
    lotto_fisso: Optional[float] = None,
    forza_esecuzione: bool = False,
) -> TradeResult:
    """Analisi SMC + esecuzione trade + notifica Telegram in un colpo solo.

    Args:
        simbolo: Simbolo da analizzare e tradare (default XAUUSD)
        lotto_fisso: Se specificato, usa questo lotto invece del calcolo automatico
        forza_esecuzione: Se True, esegue il trade anche se RR < 1:5

    Returns:
        TradeResult con esito completo dell'operazione.
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"[ANALISI] {simbolo}")
    logger.info(f"{'='*60}")

    # Fase 1: Analisi
    logger.info(f"\n[FASE 1] Analisi SMC...")
    analisi = analisi_completa(simbolo)

    if analisi.get("errore"):
        logger.error(f"[ERRORE] Analisi fallita: {analisi['errore']}")
        return TradeResult(errore=analisi["errore"], simbolo=simbolo)

    # Stampa riepilogo analisi
    h4 = analisi.get("h4", {})
    trade = analisi.get("trade_suggerito")
    print(f"\n{'-'*50}")
    print(f"  H4: {h4.get('trend', 'N/D').upper()} @ {analisi.get('prezzo_corrente', 0):.2f}")
    print(f"  Zona: {h4.get('zona', 'N/D')}")
    print(f"  DXY: {analisi.get('dxy', {}).get('trend', 'N/D')}")
    print(f"  Sessione: {analisi.get('sessione', 'N/D')}")
    print(f"{'-'*50}")

    # Fase 2: Notifica analisi (sempre)
    logger.info(f"\n[FASE 2] Invio analisi Telegram...")
    invia_notifica_analisi(analisi)

    # Fase 3: Esecuzione trade (se c'è un segnale)
    if not trade:
        msg = f"[NESUN TRADE] {simbolo} - nessun setup valido ora"
        logger.warning(msg)
        risultato = TradeResult(errore=msg, simbolo=simbolo, analisi=analisi)

        # Notifica comunque che non c'è trade
        try:
            from telegram_notifier import send_telegram_message
            send_telegram_message(
                f"[ANALISI] *ANALISI COMPLETATA - NESSUN TRADE*\n"
                f"{'-'*30}\n"
                f"{simbolo} | {analisi['sessione'].upper()}\n"
                f"H4: {h4.get('trend', 'N/D').upper()} @ {analisi.get('prezzo_corrente',0):.2f}\n"
                f"Zona: {h4.get('zona', 'N/D')}\n"
                f"{'-'*30}\n"
                f"Nessun setup valido al momento"
            )
        except Exception:
            pass

        return risultato

    logger.info(f"\n[FASE 3] Trade trovato! {trade['direzione']} "
                f"entry={trade['entry']:.2f} SL={trade['sl']:.2f} "
                f"TP1={trade['tp1']:.2f} RR 1:{trade['rr1']}")

    # Check RR minimo 1:5 (bypassabile con --forza)
    if trade['rr1'] < 5.0 and not forza_esecuzione:
        msg = (f"RR {trade['rr1']} < 1:5 e --forza non attivo. Trade saltato."
               f" Usa --forza per bypassare il limite.")
        logger.warning(f"[WARN] {msg}")
        return TradeResult(errore=msg, simbolo=simbolo, analisi=analisi)

    # Determina lotto
    if lotto_fisso is not None:
        lotto = lotto_fisso
        logger.info(f"Lotto fisso: {lotto}")
    else:
        # Calcolo automatico dal risk manager
        try:
            from risk_manager import calculate_lot_size
            balance = _get_balance()
            lotto = calculate_lot_size(
                symbol=simbolo,
                entry=trade["entry"],
                sl=trade["sl"],
                risk_pct=1.0,  # 1% di rischio
                balance=balance,
            )
            logger.info(f"Lotto calcolato: {lotto} (balance={balance:.2f}, rischio 1%)")
        except Exception as e:
            lotto = 0.01  # fallback minimo
            logger.warning(f"Calcolo lotto fallito, uso lotto minimo: {lotto} ({e})")

    # Fase 4: Esegui trade
    logger.info(f"\n[FASE 4] Esecuzione ordine...")
    risultato = esegui_trade(
        simbolo=simbolo,
        direzione=trade["direzione"],
        entry=trade["entry"],
        sl=trade["sl"],
        tp=trade["tp1"],  # Usa TP1 come primo target
        lotto=lotto,
    )
    risultato.analisi = analisi

    # Fase 5: Notifica risultato
    logger.info(f"\n[FASE 5] Notifica Telegram...")
    invia_notifica_trade(risultato)

    # Riepilogo finale
    if risultato.eseguito:
        logger.info(f"\n{'='*60}")
        logger.info(f"[OK] OPERAZIONE COMPLETATA CON SUCCESSO!")
        logger.info(f"   {simbolo} {risultato.direzione} {risultato.volume} @ {risultato.prezzo_esecuzione:.2f}")
        logger.info(f"   Ticket: #{risultato.ticket} | SL: {risultato.sl:.2f} | TP: {risultato.tp:.2f}")
        logger.info(f"{'='*60}")
    else:
        logger.warning(f"\n{'='*60}")
        logger.warning(f"[WARN] OPERAZIONE NON ESEGUITA")
        logger.warning(f"   Motivo: {risultato.errore}")
        logger.warning(f"{'='*60}")

    return risultato


# ============================================================================
# Utility
# ============================================================================

def _get_pip_size(simbolo: str) -> float:
    """Restituisce la dimensione del pip per un simbolo.

    Delega a utils.pip_size() per mantenere la convenzione UNICA del bot
    (XAU: 1 pip = 0.10, JPY: 0.01, forex standard: 0.0001).
    """
    from utils import pip_size
    return pip_size(simbolo)


def _get_balance() -> float:
    """Restituisce il balance del conto MT5."""
    mt5 = _get_mt5()
    info = mt5.account_info()
    return float(info.balance) if info else 10000.0


# ============================================================================
# Esecuzione standalone
# ============================================================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Analisi SMC + Trade automatico su MT5 + Notifica Telegram",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python test_notifiche.py                          # Analisi + trade su XAUUSD
  python test_notifiche.py --simbolo EURUSD         # Analisi su EURUSD
  python test_notifiche.py --simbolo XAUUSD --lotto 0.01 --forza
  python test_notifiche.py --solo-analisi           # Solo analisi, nessun trade
        """
    )
    parser.add_argument("--simbolo", type=str, default="XAUUSD",
                        help="Simbolo MT5 da analizzare (default: XAUUSD)")
    parser.add_argument("--lotto", type=float, default=None,
                        help="Lotto fisso (default: calcolo automatico 1% rischio)")
    parser.add_argument("--forza", action="store_true", default=False,
                        help="Esegue trade anche se RR < 1:5 (default: False)")
    parser.add_argument("--solo-analisi", action="store_true",
                        help="Solo analisi, nessun trade eseguito")
    parser.add_argument("--mantieni-connessione", action="store_true",
                        help="Mantieni MT5 connesso dopo l'esecuzione")

    args = parser.parse_args()

    logger.info(f"[START] TEST_NOTIFICHE")
    logger.info(f"   Simbolo: {args.simbolo}")
    logger.info(f"   Modalità: {'SOLO ANALISI' if args.solo_analisi else 'ANALISI + TRADE'}")

    try:
        if args.solo_analisi:
            # Solo analisi
            analisi = analisi_completa(args.simbolo)
            if analisi.get("errore"):
                print(f"\n[ERRORE] {analisi['errore']}")
            else:
                trade = analisi.get("trade_suggerito")
                print(f"\n[OK] Analisi completata!")
                print(f"   H4: {analisi['h4'].get('trend', 'N/D')}")
                print(f"   Prezzo: {analisi.get('prezzo_corrente', 0):.2f}")
                print(f"   Zona: {analisi['h4'].get('zona', 'N/D')}")
                if trade:
                    print(f"\n[TRADE] SUGGERITO:")
                    print(f"   {trade['direzione']} @ {trade['entry']:.2f}")
                    print(f"   SL: {trade['sl']:.2f} | TP1: {trade['tp1']:.2f} | TP2: {trade['tp2']:.2f}")
                    print(f"   R:R: 1:{trade['rr1']} / 1:{trade['rr2']}")
                else:
                    print(f"\n[NESUN TRADE] Nessun trade valido al momento")
        else:
            # Analisi + Trade + Notifica
            risultato = analizza_e_trada(
                simbolo=args.simbolo,
                lotto_fisso=args.lotto,
                forza_esecuzione=args.forza,
            )

            if risultato.eseguito:
                print(f"\n[OK] TRADE ESEGUITO CON SUCCESSO!")
                print(f"   Ticket: #{risultato.ticket}")
                print(f"   {risultato.direzione} {risultato.volume} {risultato.simbolo}")
                print(f"   Entry: {risultato.prezzo_esecuzione:.2f}")
            else:
                print(f"\n[WARN] Trade non eseguito: {risultato.errore}")

    except KeyboardInterrupt:
        logger.info("\nInterrotto dall'utente")
    except Exception as e:
        logger.exception(f"[ERRORE] Errore globale: {e}")

    finally:
        if not args.mantieni_connessione:
            _disconnetti_mt5()
        logger.info(f"\n{'='*60}")
        logger.info(f"  COMPLETATO")
        logger.info(f"{'='*60}")
