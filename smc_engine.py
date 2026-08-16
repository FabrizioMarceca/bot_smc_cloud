"""
smc_engine.py
=============
Engine SMC di alto livello. Fornisce un'interfaccia compatta per l'analisi
di mercato usando structure_analyzer come backend.

Funzioni principali:
    - analyze_market(symbol, htf, mtf): analisi multi-timeframe completa
    - get_trade_bias(symbol): ritorna il bias operativo (long/short/neutral)
    - is_tradable_session(): verifica se siamo in una sessione attiva
    - get_dxy_correlation(): correlazione con l'indice del dollaro
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

import structure_analyzer as sa
import config

logger = logging.getLogger(__name__)

# Cache DXY proxy: evita ricalcoli inutili se i dati non cambiano
_DXY_PROXY_CACHE: dict = {"df": None, "last_build": 0.0}
_DXY_PROXY_TTL = 120  # ricalcola proxy ogni 2 minuti (i dati H4 cambiano ogni 4h)

# Pesi ICE US Dollar Index (fonte: Intercontinental Exchange)
# Usiamo 3 coppie (83.1% del peso) + costanti per le restanti (16.9%)
_DXY_WEIGHTS = {
    "EURUSD": -0.576,   # 57.6% inverso
    "USDJPY":  0.136,   # 13.6%
    "GBPUSD": -0.119,   # 11.9% inverso
}
_DXY_CONSTANT = 50.14348112
# Valori fissi per le coppie non disponibili (aggiorna periodicamente)
_USDCAD_FIXED = 1.375
_USDSEK_FIXED = 10.75
_USDCHF_FIXED = 0.885

# ---------------------------------------------------------------------------
# Orari delle sessioni di mercato (UTC)
# ---------------------------------------------------------------------------

SESSION_SCHEDULE = {
    "asian": {"open": 0, "close": 9},     # 00:00-09:00 UTC
    "london": {"open": 8, "close": 17},    # 08:00-17:00 UTC
    "newyork": {"open": 13, "close": 22},  # 13:00-22:00 UTC
}

# Orari chiave per le news (UTC) - orari di massima volatilita'
KEY_NEWS_HOURS = {8, 9, 13, 14, 15, 20}

# Orari apertura borse - trigger per movimenti istituzionali
# Strategia SMC: orari 1:00-2:00 (Asia), 8:00-9:00 (London), 14:30-16:00 (NY/news)
MARKET_OPEN_HOURS = {
    "asia_open": {"start": 1, "end": 2},     # 01:00-02:00 UTC
    "london_open": {"start": 8, "end": 9},    # 08:00-09:00 UTC
    "ny_open": {"start": 14, "end": 16},      # 14:00-16:00 UTC (include news 14:30)
}

# News specifiche che richiedono attenzione (date esatte non gestite senza API esterna).
# Gli orari chiave sono definiti in KEY_NEWS_HOURS.


def is_tradable_session() -> bool:
    """Verifica se siamo in una sessione di mercato attiva (Londra o NY)."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour

    london = SESSION_SCHEDULE["london"]
    newyork = SESSION_SCHEDULE["newyork"]

    return (london["open"] <= hour < london["close"]) or (newyork["open"] <= hour < newyork["close"])


def get_current_session() -> str:
    """Restituisce la sessione corrente: 'asian', 'london', 'newyork', 'closed'."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    weekday = now_utc.weekday()

    if weekday >= 5:  # Weekend
        return "closed"

    for name, times in SESSION_SCHEDULE.items():
        if times["open"] <= hour < times["close"]:
            return name
    return "closed"


def is_near_news_hour() -> bool:
    """Verifica se siamo vicini a un orario di news."""
    now_utc = datetime.now(timezone.utc)
    return now_utc.hour in KEY_NEWS_HOURS


def get_market_open_status() -> dict:
    """Verifica se siamo in una finestra di apertura borse (trigger istituzionale).

    Strategia SMC: le istituzioni muovono il mercato agli orari di apertura.
    - Asia open (1:00-2:00 UTC): liquidita' asiatica
    - London open (8:00-9:00 UTC): massima volatilita' europea
    - NY open (14:00-16:00 UTC): volatilita' americana + news

    Returns:
        {'in_open_window': bool, 'market': str, 'minutes_left': int}
    """
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    minute = now_utc.minute
    now_minutes = hour * 60 + minute

    for market, window in MARKET_OPEN_HOURS.items():
        start_min = window["start"] * 60
        end_min = window["end"] * 60
        if start_min <= now_minutes < end_min:
            minutes_left = end_min - now_minutes
            label = market.replace("_open", "")
            return {"in_open_window": True, "market": label, "minutes_left": minutes_left}

    return {"in_open_window": False, "market": "", "minutes_left": 0}


def is_high_volatility_window() -> bool:
    """Finestra di alta volatilita': news o apertura borse."""
    if is_near_news_hour():
        return True
    status = get_market_open_status()
    return status["in_open_window"]


def get_asian_range(symbol: str) -> dict:
    """Traccia high/low della sessione asiatica (1:00-7:00 UTC).

    Strategia Video 27: la sessione asiatica crea un range di liquidita'.
    Quando Londra apre (8:00-9:00) e manipola uno degli estremi,
    e' un segnale forte di inversione.

    Returns:
        {'high': float, 'low': float, 'valid': bool,
         'in_london_open': bool, 'near_high': bool, 'near_low': bool}
    """
    from mt5_adapter import mt5

    result = {"high": 0.0, "low": 0.0, "valid": False,
              "in_london_open": False, "near_high": False, "near_low": False}

    now_utc = datetime.now(timezone.utc)
    # Serve solo durante London open (8:00-10:00 UTC)
    if not (8 <= now_utc.hour <= 10):
        return result

    try:
        # Scarica candele M15 delle ultime 24h
        df = sa.get_market_data(symbol, mt5.TIMEFRAME_M15, bars=96)
        if df is None or len(df) < 4:
            return result

        # Filtra solo candele tra 1:00 e 7:00 UTC
        df_utc = df[df["time"].dt.hour >= 1]
        df_asian = df_utc[df_utc["time"].dt.hour < 7]
        if df_asian.empty:
            return result

        asian_high = float(df_asian["high"].max())
        asian_low = float(df_asian["low"].min())
        current_price = float(df["close"].iloc[-1])

        # Tolleranza: 0.3% del range asiatico, minimo 5 pips (0.05 per XAU)
        range_size = asian_high - asian_low
        tol = max(range_size * 0.003, 0.05) if range_size > 0 else 0.5

        result.update({
            "high": asian_high,
            "low": asian_low,
            "valid": True,
            "in_london_open": True,
            "near_high": abs(current_price - asian_high) < tol,
            "near_low": abs(current_price - asian_low) < tol,
        })

    except Exception as e:
        logger.debug("Asian range non disponibile per %s: %s", symbol, e)

    return result


# ---------------------------------------------------------------------------
# Bias operativo
# ---------------------------------------------------------------------------

def get_trade_bias(symbol: str) -> dict:
    """Analizza il bias operativo per un simbolo (long/short/neutral).

    Usa H4 per la direzione e M15 per i segnali di entrata.

    Returns:
        Dizionario con 'bias', 'trend', 'signals', 'confidence'.
    """
    from mt5_adapter import mt5

    result = {"bias": "neutral", "trend": "sideways", "signals": 0, "confidence": 0}

    try:
        h4 = sa.analyze_symbol(symbol, mt5.TIMEFRAME_H4, bars=200, pivot_window=3)
        if not h4["success"]:
            return result

        trend = h4["trend"]
        result["trend"] = trend

        if trend == "sideways":
            return result

        m15 = sa.analyze_symbol(symbol, mt5.TIMEFRAME_M15, bars=200, pivot_window=4)
        if not m15["success"]:
            return result

        signals = m15.get("signals", [])
        result["signals"] = len(signals)

        buy_signals = [s for s in signals if s["direction"] == "buy"]
        sell_signals = [s for s in signals if s["direction"] == "sell"]

        if trend == "bullish" and buy_signals:
            high_prob = [s for s in buy_signals if s["probability"] == "high"]
            result["bias"] = "long"
            result["confidence"] = 80 if high_prob else 60
        elif trend == "bearish" and sell_signals:
            high_prob = [s for s in sell_signals if s["probability"] == "high"]
            result["bias"] = "short"
            result["confidence"] = 80 if high_prob else 60
        elif trend == "bullish" and sell_signals:
            # Possibile inversione: segnali contro-trend
            result["bias"] = "short_counter"
            result["confidence"] = 50
        elif trend == "bearish" and buy_signals:
            result["bias"] = "long_counter"
            result["confidence"] = 50

        return result

    except Exception as e:
        logger.exception("Errore trade bias per %s: %s", symbol, e)
        return result


# ---------------------------------------------------------------------------
# Analisi DXY
# ---------------------------------------------------------------------------

def get_dxy_bias() -> Optional[dict]:
    """Calcola il bias del DXY (indice del dollaro) come PROXY dai dati MT5.

    Strategia: il Dollar Index non e' disponibile sulla maggior parte dei broker
    demo. Invece di cercare il simbolo, lo calcoliamo dalla formula ICE usando
    i prezzi H4 di EURUSD, USDJPY, GBPUSD (83.1% del peso) scaricati da MT5.

    DXY = 50.14348112 x EURUSD^(-0.576) x USDJPY^(0.136) x GBPUSD^(-0.119)
                           x USDCAD^(0.091) x USDSEK^(0.042) x USDCHF^(0.036)

    Le 3 coppie mancanti (CAD, SEK, CHF) usano valori fissi recenti.

    Per il trend, usiamo EMA crossover (20 vs 50 barre H4) invece di swing
    detection, perche' il range del DXY (~2 punti) e' troppo stretto per
    trovare pivot significativi.

    Returns:
        Dizionario con 'trend', 'bias', 'current_price' o None.
    """
    import time as _time
    from mt5_adapter import mt5

    try:
        now = _time.time()

        # --- Cache: ricalcola solo ogni 2 minuti ---
        if (_DXY_PROXY_CACHE["df"] is not None
                and (now - _DXY_PROXY_CACHE["last_build"] < _DXY_PROXY_TTL)):
            df = _DXY_PROXY_CACHE["df"]
        else:
            # Scarica H4 per le 3 coppie principali
            dfs = {}
            for _sym in _DXY_WEIGHTS:
                data = sa.get_market_data(_sym, mt5.TIMEFRAME_H4, 200)
                if data is None or len(data) < 20:
                    logger.debug("DXY proxy: dati insufficienti per %s", _sym)
                    return None
                dfs[_sym] = data.set_index("time")[["close"]].rename(columns={"close": _sym})

            # Merge su timestamp comune
            merged = dfs["EURUSD"].join(dfs["USDJPY"], how="inner").join(dfs["GBPUSD"], how="inner")
            if len(merged) < 50:
                logger.debug("DXY proxy: meno di 50 barre sovrapposte")
                return None

            # Calcola DXY proxy per ogni barra
            dxy_values = (
                _DXY_CONSTANT
                * (merged["EURUSD"] ** _DXY_WEIGHTS["EURUSD"])
                * (merged["USDJPY"] ** _DXY_WEIGHTS["USDJPY"])
                * (merged["GBPUSD"] ** _DXY_WEIGHTS["GBPUSD"])
                * (_USDCAD_FIXED ** 0.091)
                * (_USDSEK_FIXED ** 0.042)
                * (_USDCHF_FIXED ** 0.036)
            )

            # Sanity check: DXY dovrebbe essere ~90-110
            last_price = float(dxy_values.iloc[-1])
            if last_price < 70 or last_price > 130:
                logger.warning("DXY proxy: valore anomalo %.2f (range atteso 80-120).", last_price)
                return None

            df = dxy_values.to_frame(name="close")
            _DXY_PROXY_CACHE["df"] = df
            _DXY_PROXY_CACHE["last_build"] = now
            logger.debug("DXY proxy ricalcolato: %.2f (%d barre)", last_price, len(df))

        # --- Analisi trend con EMA crossover (20 vs 50) ---
        close = df["close"]
        ema_fast = close.ewm(span=20, adjust=False).mean()
        ema_slow = close.ewm(span=50, adjust=False).mean()

        current_price = float(close.iloc[-1])
        fast_now = float(ema_fast.iloc[-1])
        slow_now = float(ema_slow.iloc[-1])

        # Differenza percentuale tra le due EMA
        diff_pct = (fast_now - slow_now) / slow_now * 100

        # Soglie: >0.15% = trend definito, altrimenti sideways
        if diff_pct > 0.15:
            trend = "bullish"
        elif diff_pct < -0.15:
            trend = "bearish"
        else:
            trend = "sideways"

        return {
            "trend": trend,
            "current_price": current_price,
            "bias": ("bullish_usd" if trend == "bullish"
                     else "bearish_usd" if trend == "bearish"
                     else "neutral"),
        }

    except Exception as e:
        logger.warning("Analisi DXY proxy non disponibile: %s", e)
        return None

def analyze_market(df: pd.DataFrame) -> dict:
    """Analisi rapida su un DataFrame gia' scaricato (retrocompatibile)."""
    try:
        df = sa.identify_swings(df, window=4)
        swings = sa.filter_alternating_swings(df)
        if swings.empty:
            return {"zone": "Equilibrium", "trend": "sideways", "signals": []}

        swings = sa.label_structure(swings)
        swings = sa.classify_strong_weak(swings)
        swings = sa.detect_structure_breaks(swings)

        trend = sa.get_trend_direction(swings)

        obs = sa.identify_order_blocks(df, swings)
        obs = sa.filter_mitigated_obs(df, obs)
        obs = sa.apply_pd_matrix(swings, obs)
        obs = sa.detect_liquidity_sweeps(df, obs)

        liq = sa.find_liquidity_zones(df, swings)
        # Percorso legacy equivalente al daytrading: applica il minimo 1:3.
        signals = sa.generate_signals(df, swings, obs, liq, trend, mode="daytrading")

        # Zone info
        current_price = float(df["close"].iloc[-1])
        if "equilibrium" in obs.columns and not obs.empty:
            eq = float(obs["equilibrium"].iloc[-1])
            zone = "Premium" if current_price > eq else "Discount"
        else:
            zone = "Equilibrium"

        return {
            "zone": zone,
            "trend": trend,
            "signals": signals,
            "current_price": current_price,
            "swings_count": len(swings),
            "obs_count": len(obs),
        }
    except Exception as e:
        logger.exception("analyze_market fallita: %s", e)
        return {"zone": "Equilibrium", "trend": "sideways", "signals": []}


# ---------------------------------------------------------------------------
# Retrocompatibilità con vecchia interfaccia
# ---------------------------------------------------------------------------

def identify_market_structure(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """Wrapper retrocompatibile per identify_swings."""
    return sa.identify_swings(df, window=window)


def get_fibonacci_zone(price: float, high: float, low: float) -> str:
    """Wrapper retrocompatibile."""
    return sa.get_fibonacci_zone(price, high, low)