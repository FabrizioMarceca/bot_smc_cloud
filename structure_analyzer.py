"""
structure_analyzer.py
=====================
Analisi SMC (Smart Money Concepts) completa per il bot di trading.

Pipeline di analisi:
    1. get_market_data()        -> scarica candele da MT5
    2. identify_swings()         -> pivot high/low (HH, HL, LL, LH)
    3. filter_alternating_swings()-> alternanza pulita
    4. label_structure()         -> etichetta HH/HL/LL/LH
    5. classify_strong_weak()    -> forti (causano SB) vs deboli
    6. detect_structure_breaks() -> BOS, MSS, TC
    7. identify_order_blocks()   -> candele prima degli swing
    8. filter_mitigated_obs()    -> rimuove OB gia' toccati
    9. apply_pd_matrix()         -> filtra per Premium/Discount
    10. detect_liquidity_sweeps()-> sweep di liquidita'
    11. find_liquidity_zones()   -> zone di liquidita' retail
    12. generate_signals()       -> segnali operativi finali
    13. analyze_symbol()         -> pipeline completa unificata
"""

from __future__ import annotations

import time  # per throttling log
import logging
from typing import Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

# --- Throttle log diagnostici OB (evita crash da log eccessivo) ---
# Le scansioni girano a SMC_SCAN_INTERVAL_SECONDS, ma i dettagli OB
# vengono stampati solo ogni LOG_OB_DEBUG_INTERVAL_SECONDS per simbolo.
_LOG_OB_INTERVAL = getattr(config, "LOG_OB_DEBUG_INTERVAL_SECONDS", 60)
_last_ob_log_time: dict[str, float] = {}


def _can_log_ob_debug(symbol: str) -> bool:
    """Throttle per i log diagnostici OB: max 1 stampa ogni _LOG_OB_INTERVAL secondi per simbolo."""
    now = time.time()
    last = _last_ob_log_time.get(symbol, 0)
    if now - last >= _LOG_OB_INTERVAL:
        _last_ob_log_time[symbol] = now
        return True
    return False

# R:R minimo configurabile per daytrading. Lo swing usa una
# regola separata e più severa: minimo 1:4, senza eccezioni counter-trend.
_MIN_RR = getattr(config, "MIN_RR", 3.0)
_MIN_RR_COUNTER_TREND = 2.0
_TP1_FALLBACK_MULT = 3.0


def _minimum_target_rr(mode: str | None, is_pro: bool) -> float:
    """R:R minimo per un target operativo della modalità indicata."""
    if mode == "swing":
        return config.get_min_rr("swing")
    if mode == "daytrading":
        return config.get_min_rr("daytrading")
    return _MIN_RR if is_pro else _MIN_RR_COUNTER_TREND


def _swing_target(entry: float, risk: float, direction: str, minimum_rr: float) -> float:
    """Costruisce un target swing minimo a 4R, senza alcun tetto in pip."""
    distance = risk * minimum_rr
    return entry + distance if direction == "buy" else entry - distance
# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 1. RECUPERO DATI
# ---------------------------------------------------------------------------

def get_market_data(symbol: str, timeframe: int, bars: int = 200) -> Optional[pd.DataFrame]:
    """Recupera i dati di mercato da MT5."""
    from mt5_adapter import mt5
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, bars)
    if rates is None or len(rates) == 0:
        logger.debug("Nessun dato per %s (TF=%s)", symbol, timeframe)  # debug, non warning (es. DXY non disponibile)
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


# ---------------------------------------------------------------------------
# 2. SWING DETECTION
# ---------------------------------------------------------------------------

def identify_swings(df: pd.DataFrame, window: int = 4) -> pd.DataFrame:
    """Identifica pivot high e low con finestra centrata 2*window+1."""
    df = df.copy()
    n = len(df)
    df["swing_high"] = False
    df["swing_low"] = False

    for i in range(window, n - window):
        seg_h = df["high"].iloc[i - window : i + window + 1]
        seg_l = df["low"].iloc[i - window : i + window + 1]
        if df["high"].iloc[i] == seg_h.max() and (seg_h == df["high"].iloc[i]).sum() == 1:
            df.loc[df.index[i], "swing_high"] = True
        if df["low"].iloc[i] == seg_l.min() and (seg_l == df["low"].iloc[i]).sum() == 1:
            df.loc[df.index[i], "swing_low"] = True

    df["swing"] = 0
    df.loc[df["swing_high"], "swing"] = 1
    df.loc[df["swing_low"], "swing"] = -1
    return df


# ---------------------------------------------------------------------------
# 3. ALTERNANZA SWING
# ---------------------------------------------------------------------------

def filter_alternating_swings(swings_df: pd.DataFrame) -> pd.DataFrame:
    """Garantisce alternanza high-low-high... tenendo il migliore di consecutivi."""
    df = swings_df[swings_df["swing"] != 0].copy()
    if df.empty:
        return df
    filtered, cur_type, cur_best = [], None, None
    for _, row in df.iterrows():
        if row["swing"] == 1:
            if cur_type == "high":
                if row["high"] > cur_best["high"]:
                    cur_best = row
            else:
                if cur_best is not None:
                    filtered.append(cur_best)
                cur_type, cur_best = "high", row
        else:
            if cur_type == "low":
                if row["low"] < cur_best["low"]:
                    cur_best = row
            else:
                if cur_best is not None:
                    filtered.append(cur_best)
                cur_type, cur_best = "low", row
    if cur_best is not None:
        filtered.append(cur_best)
    result = pd.DataFrame(filtered)
    if not result.empty:
        result = result.reset_index(drop=True)
    return result


# ---------------------------------------------------------------------------
# 4. LABEL STRUTTURA
# ---------------------------------------------------------------------------

def label_structure(swings: pd.DataFrame) -> pd.DataFrame:
    """Etichetta swing come HH, HL, LL, LH."""
    df = swings.copy()
    df["type"] = ""
    df["label"] = ""
    df["price_level"] = 0.0
    last_high: Optional[float] = None
    last_low: Optional[float] = None

    for i in range(len(df)):
        if df.loc[df.index[i], "swing"] == 1:
            df.loc[df.index[i], "type"] = "high"
            df.loc[df.index[i], "price_level"] = df.loc[df.index[i], "high"]
            cur = df.loc[df.index[i], "high"]
            if last_high is not None:
                df.loc[df.index[i], "label"] = "HH" if cur > last_high else "LH"
            last_high = cur
        else:
            df.loc[df.index[i], "type"] = "low"
            df.loc[df.index[i], "price_level"] = df.loc[df.index[i], "low"]
            cur = df.loc[df.index[i], "low"]
            if last_low is not None:
                df.loc[df.index[i], "label"] = "LL" if cur < last_low else "HL"
            last_low = cur
    return df


# ---------------------------------------------------------------------------
# 5. CLASSIFICAZIONE FORTI / DEBOLI
# ---------------------------------------------------------------------------

def classify_strong_weak(swings: pd.DataFrame) -> pd.DataFrame:
    """Classifica: Forte solo se causa la PRIMA rottura struttura immediatamente dopo.
    Un high e' forte se il PRIMO swing futuro di tipo low e' un LL (ha rotto struttura).
    Un low e' forte se il PRIMO swing futuro di tipo high e' un HH."""
    df = swings.copy()
    df["strength"] = ""
    for i in range(len(df)):
        if df.loc[df.index[i], "type"] == "high":
            found = False
            for j in range(i + 1, len(df)):
                if df.loc[df.index[j], "type"] == "low":
                    found = True
                    df.loc[df.index[i], "strength"] = (
                        "Strong" if df.loc[df.index[j], "label"] == "LL" else "Weak"
                    )
                    break
                elif df.loc[df.index[j], "type"] == "high":
                    break
            if not found:
                df.loc[df.index[i], "strength"] = "Weak"
        elif df.loc[df.index[i], "type"] == "low":
            found = False
            for j in range(i + 1, len(df)):
                if df.loc[df.index[j], "type"] == "high":
                    found = True
                    df.loc[df.index[i], "strength"] = (
                        "Strong" if df.loc[df.index[j], "label"] == "HH" else "Weak"
                    )
                    break
                elif df.loc[df.index[j], "type"] == "low":
                    break
            if not found:
                df.loc[df.index[i], "strength"] = "Weak"
    return df


# ---------------------------------------------------------------------------
# 6. BOS / MSS / TC
# ---------------------------------------------------------------------------

def detect_structure_breaks(swings: pd.DataFrame) -> pd.DataFrame:
    """Rileva BOS, MSS e TC."""
    df = swings.copy()
    df["structure_event"] = ""
    last_hh_idx: Optional[int] = None
    last_ll_idx: Optional[int] = None
    trend = "neutral"

    for i in range(len(df)):
        label = df.loc[df.index[i], "label"]
        sw_type = df.loc[df.index[i], "type"]

        if sw_type == "high":
            if label == "HH":
                if last_hh_idx is not None:
                    df.loc[df.index[i], "structure_event"] = "SB_bullish"
                last_hh_idx = i
            elif label == "LH":
                if last_hh_idx is not None and trend == "bullish":
                    df.loc[df.index[i], "structure_event"] = "MSS_bearish"
        elif sw_type == "low":
            if label == "LL":
                if last_ll_idx is not None:
                    df.loc[df.index[i], "structure_event"] = "SB_bearish"
                last_ll_idx = i
            elif label == "HL":
                if last_ll_idx is not None and trend == "bearish":
                    df.loc[df.index[i], "structure_event"] = "MSS_bullish"

        # Trend Change (TC)
        if label == "HL" and trend == "bearish" and df.loc[df.index[i], "strength"] == "Strong":
            for j in range(i-1, 0, -1):
                if df.loc[df.index[j], "label"] == "LH" and df.loc[df.index[j], "strength"] == "Strong":
                    if df.loc[df.index[i], "price_level"] > df.loc[df.index[j], "price_level"]:
                        df.loc[df.index[i], "structure_event"] = "TC_bullish"
                    break
        elif label == "LH" and trend == "bullish" and df.loc[df.index[i], "strength"] == "Strong":
            for j in range(i-1, 0, -1):
                if df.loc[df.index[j], "label"] == "HL" and df.loc[df.index[j], "strength"] == "Strong":
                    if df.loc[df.index[i], "price_level"] < df.loc[df.index[j], "price_level"]:
                        df.loc[df.index[i], "structure_event"] = "TC_bearish"
                    break

        # Aggiorna trend
        labels = df.loc[df.index[:i+1], "label"].tolist()
        if labels.count("HH") >= 2 and labels.count("HL") >= 1:
            trend = "bullish"
        elif labels.count("LL") >= 2 and labels.count("LH") >= 1:
            trend = "bearish"

    return df


# ---------------------------------------------------------------------------
# 7. ORDER BLOCKS
# ---------------------------------------------------------------------------

def identify_order_blocks(df: pd.DataFrame, swings: pd.DataFrame, lookback: int = 10) -> pd.DataFrame:
    """Identifica Order Block col pattern a 3 candele del corso SMC (Video 25).

    Pattern Demand (BUY) — 3 candele:
        (1) CANDELA PUSH: forte giu', bearish, full body (body >= 60% range).
            NO doji. E' il carburante che spinge verso il minimo.
        (2) SWING: minimo pulito (HL o HH). Candela pivot identificata
            dal rilevatore di swing.
        (3) CANDELA INVERSIONE: forte su, bullish, full body subito dopo
            lo swing. NO doji. Conferma che le istituzioni sono entrate long.

    Pattern Supply (SELL) — 3 candele:
        (1) CANDELA PUSH: forte su, bullish, full body (body >= 60% range).
        (2) SWING: massimo pulito (LH o LL).
        (3) CANDELA INVERSIONE: forte giu', bearish, full body subito dopo.

    Soglia body 60% dal corso: 'Non voglio doji. Voglio candele full body.'
    Una doji (body < 10% range) o una candela con corpo medio (< 60%)
    indica indecisione retail — non e' un OB istituzionale valido.

    Il parametro 'lookback' e' mantenuto per retrocompatibilita' ma
    non viene piu' usato: il pattern e' sempre sulle 3 candele
    adiacenti allo swing (push -> swing -> inversione).
    """
    _MIN_BODY_RATIO = 0.60  # soglia minima: corpo >= 60% del range (Video 25)

    obs_list: list[dict] = []
    n = len(df)

    for _, swing in swings.iterrows():
        label = swing.get("label", "")
        if label == "":
            continue
        mask = df["time"] == swing["time"]
        if not mask.any():
            continue
        idx_s = int(mask.values.argmax())

        # Servono almeno 1 candela prima (push) e 1 dopo (inversione)
        if idx_s < 1 or idx_s >= n - 1:
            continue

        # ----------------------------------------------------------------
        # CANDELA PUSH (idx_s - 1): la spinta VERSO lo swing
        # ----------------------------------------------------------------
        push = df.iloc[idx_s - 1]
        push_body = abs(float(push["close"]) - float(push["open"]))
        push_range = float(push["high"]) - float(push["low"])
        if push_range <= 0:
            continue
        push_ratio = push_body / push_range
        push_bullish = float(push["close"]) > float(push["open"])
        push_bearish = float(push["close"]) < float(push["open"])

        # ----------------------------------------------------------------
        # CANDELA INVERSIONE (idx_s + 1): la conferma dopo lo swing
        # ----------------------------------------------------------------
        rev = df.iloc[idx_s + 1]
        rev_body = abs(float(rev["close"]) - float(rev["open"]))
        rev_range = float(rev["high"]) - float(rev["low"])
        if rev_range <= 0:
            continue
        rev_ratio = rev_body / rev_range
        rev_bullish = float(rev["close"]) > float(rev["open"])
        rev_bearish = float(rev["close"]) < float(rev["open"])

        # ----------------------------------------------------------------
        # DEMAND OB: push BEARISH full body + reversal BULLISH full body
        #   Solo su swing LOW (HL): gli OB demand si formano ai minimi.
        # ----------------------------------------------------------------
        if label in ("HL",):
            if (push_bearish and push_ratio >= _MIN_BODY_RATIO
                    and rev_bullish and rev_ratio >= _MIN_BODY_RATIO):
                obs_list.append({
                    "time_swing": swing["time"],
                    "label_swing": label,
                    "tipo_zona": "Demand (Bullish OB)",
                    "top_ob": float(max(push["open"], push["close"])),
                    "bottom_ob": float(push["low"]),
                    "idx_swing": idx_s,
                    "idx_ob": idx_s - 1,
                })

        # ----------------------------------------------------------------
        # SUPPLY OB: push BULLISH full body + reversal BEARISH full body
        #   Solo su swing HIGH (LH): gli OB supply si formano ai massimi.
        # ----------------------------------------------------------------
        elif label in ("LH",):
            if (push_bullish and push_ratio >= _MIN_BODY_RATIO
                    and rev_bearish and rev_ratio >= _MIN_BODY_RATIO):
                obs_list.append({
                    "time_swing": swing["time"],
                    "label_swing": label,
                    "tipo_zona": "Supply (Bearish OB)",
                    "top_ob": float(push["high"]),
                    "bottom_ob": float(min(push["open"], push["close"])),
                    "idx_swing": idx_s,
                    "idx_ob": idx_s - 1,
                })

    return pd.DataFrame(obs_list)


# ---------------------------------------------------------------------------
# 8. FILTRO OB MITIGATI
# ---------------------------------------------------------------------------

def filter_mitigated_obs(df: pd.DataFrame, obs_df: pd.DataFrame) -> pd.DataFrame:
    """Rimuove OB gia' toccati dal prezzo dopo la formazione."""
    if obs_df.empty:
        return obs_df
    unmitigated = []
    for _, ob in obs_df.iterrows():
        mitigated = False
        idx_s = int(ob["idx_swing"])
        for i in range(idx_s + 2, len(df)):
            if "Demand" in str(ob["tipo_zona"]):
                # SMC: mitigato solo se il prezzo CHIUDE sotto il top dell'OB
                if df.loc[df.index[i], "close"] < ob["top_ob"]:
                    mitigated = True
                    break
            elif "Supply" in str(ob["tipo_zona"]):
                if df.loc[df.index[i], "close"] > ob["bottom_ob"]:
                    mitigated = True
                    break
        if not mitigated:
            unmitigated.append(ob)
    result = pd.DataFrame(unmitigated)
    if not result.empty:
        result = result.drop(columns=["idx_swing", "idx_ob"], errors="ignore")
    return result


# ---------------------------------------------------------------------------
# 9. MATRICE PREMIUM / DISCOUNT
# ---------------------------------------------------------------------------

def apply_pd_matrix(
    swings: pd.DataFrame, obs_df: pd.DataFrame,
    h4_equilibrium: Optional[float] = None,
    shallow_pd_pct: Optional[float] = None,
    pd_range_high: Optional[float] = None,
    pd_range_low: Optional[float] = None,
) -> pd.DataFrame:
    """Tiene solo Demand in Discount e Supply in Premium.

    Strategia SMC (pag. 30 manuale): 'Il range 4H prevale sempre sul 15M.'
    Se h4_equilibrium e' fornito (es. da analisi M15 con contesto H4),
    lo usa per il filtraggio PD invece di calcolarlo dagli swing locali.

    shallow_pd_pct (es. 0.30 per XAUUSD): per simboli momentum-driven che
    NON ritracciano fino all'equilibrio. Restringe la zona valida:
      - Demand: top_ob <= low + range * shallow_pd_pct  (0-30% anziche' 0-50%)
      - Supply: bottom_ob >= high - range * shallow_pd_pct (70-100% anziche' 50-100%)
    None = usa l'equilibrio classico (50%).

    pd_range_high / pd_range_low: range esplicito per il calcolo shallow.
    Utile quando h4_equilibrium viene dall'HTF ma serve il range completo
    (es. M15 eredita equilibrio H4 + range H4 per shallow pullback).
    """
    if obs_df.empty:
        return obs_df

    if h4_equilibrium is not None:
        equilibrium = h4_equilibrium
        # Usa il range esplicito se fornito, altrimenti non disponibile
        range_high = pd_range_high if pd_range_high is not None else 0.0
        range_low = pd_range_low if pd_range_low is not None else 0.0
    else:
        if len(swings) < 2:
            return obs_df
        labeled = swings[swings["label"] != ""]
        if len(labeled) < 2:
            return obs_df

        highs = labeled[labeled["type"] == "high"]["price_level"]
        lows = labeled[labeled["type"] == "low"]["price_level"]
        if highs.empty or lows.empty:
            return obs_df

        range_high = highs.iloc[-1]
        range_low = lows.iloc[-1]
        hh_data = labeled[labeled["label"] == "HH"]
        ll_data = labeled[labeled["label"] == "LL"]
        if not hh_data.empty:
            range_high = hh_data["price_level"].iloc[-1]
        if not ll_data.empty:
            range_low = ll_data["price_level"].iloc[-1]

        equilibrium = (range_high + range_low) / 2

    # --- Boundary per PD matrix ---
    if shallow_pd_pct is not None and range_high > range_low:
        # Shallow pullback: restringe la zona valida al 30% (es. XAUUSD)
        demand_boundary = range_low + (range_high - range_low) * shallow_pd_pct
        supply_boundary = range_high - (range_high - range_low) * shallow_pd_pct
    else:
        demand_boundary = equilibrium
        supply_boundary = equilibrium

    valid = []
    for _, ob in obs_df.iterrows():
        if "Demand" in str(ob["tipo_zona"]) and ob["top_ob"] <= demand_boundary:
            d = ob.to_dict()
            d["pd_zone"] = "Discount"
            d["equilibrium"] = equilibrium
            valid.append(d)
        elif "Supply" in str(ob["tipo_zona"]) and ob["bottom_ob"] >= supply_boundary:
            d = ob.to_dict()
            d["pd_zone"] = "Premium"
            d["equilibrium"] = equilibrium
            valid.append(d)
    return pd.DataFrame(valid)


# ---------------------------------------------------------------------------
# 10. LIQUIDITY SWEEPS
# ---------------------------------------------------------------------------

def detect_liquidity_sweeps(df: pd.DataFrame, obs_df: pd.DataFrame) -> pd.DataFrame:
    """Verifica sweep BSL/SSL o Inducement sugli OB."""
    if obs_df.empty:
        return obs_df
    if "idx_ob" not in obs_df.columns:
        obs_df["liquidity_sweep"] = "none"
        return obs_df

    cur_idx = len(df) - 1
    valid = []
    for _, ob in obs_df.iterrows():
        idx_ob = int(ob.get("idx_ob", -1))
        if idx_ob < 1:
            d = ob.to_dict()
            d["liquidity_sweep"] = "none"
            valid.append(d)
            continue

        tipo = str(ob["tipo_zona"])
        has_sweep, sw_type = False, "none"
        cc, pc = df.iloc[idx_ob], df.iloc[idx_ob - 1]

        if "Supply" in tipo:
            if cc["high"] > pc["high"] and cc["close"] < pc["high"]:
                has_sweep, sw_type = True, "BSL_sweep"
            if not has_sweep and cur_idx - idx_ob >= 5:
                pa = df.iloc[idx_ob + 1 : cur_idx]
                if not pa.empty and df.iloc[cur_idx]["high"] >= pa["high"].max():
                    has_sweep, sw_type = True, "IDM"
        elif "Demand" in tipo:
            if cc["low"] < pc["low"] and cc["close"] > pc["low"]:
                has_sweep, sw_type = True, "SSL_sweep"
            if not has_sweep and cur_idx - idx_ob >= 5:
                pa = df.iloc[idx_ob + 1 : cur_idx]
                if not pa.empty and df.iloc[cur_idx]["low"] <= pa["low"].min():
                    has_sweep, sw_type = True, "IDM"

        d = ob.to_dict()
        d["liquidity_sweep"] = sw_type if has_sweep else "none"
        valid.append(d)

    result = pd.DataFrame(valid)
    if not result.empty:
        result = result.drop(columns=["idx_ob"], errors="ignore")
    return result


# ---------------------------------------------------------------------------
# 11. ZONE DI LIQUIDITA'
# ---------------------------------------------------------------------------

def find_liquidity_zones(df: pd.DataFrame, swings: pd.DataFrame) -> pd.DataFrame:
    """Identifica zone di liquidita' (equal highs/lows cluster)."""
    zones = []
    TOL = 0.001
    price = df["close"].iloc[-1]

    def cluster(vals):
        if not vals:
            return []
        sv = sorted(vals)
        clusters, cur = [], [sv[0]]
        for v in sv[1:]:
            if abs(v - cur[-1]) / price <= TOL:
                cur.append(v)
            else:
                clusters.append(cur)
                cur = [v]
        clusters.append(cur)
        return clusters

    for lvl_type, col in [("BSL", "high"), ("SSL", "low")]:
        vals = swings[swings["type"] == ("high" if lvl_type == "BSL" else "low")]["price_level"].tolist()
        for cl in cluster(vals):
            if len(cl) >= 2:
                zones.append({
                    "type": lvl_type, "price_level": np.mean(cl),
                    "candle_count": len(cl), "is_strong": len(cl) >= 3,
                })
    return pd.DataFrame(zones)


# ---------------------------------------------------------------------------
# 12. FUNZIONI DI SUPPORTO
# ---------------------------------------------------------------------------

def get_trend_direction(swings: pd.DataFrame) -> str:
    """Determina trend: 'bullish', 'bearish', 'sideways'."""
    if swings.empty or "label" not in swings.columns:
        return "sideways"
    labels = swings["label"].tolist()
    if labels.count("HH") >= 2 and labels.count("HL") >= 1:
        return "bullish"
    if labels.count("LL") >= 2 and labels.count("LH") >= 1:
        return "bearish"
    return "sideways"


def get_fibonacci_zone(price: float, high: float, low: float) -> str:
    """Restituisce 'Discount', 'Premium' o 'Equilibrium'."""
    if high <= low:
        return "Equilibrium"
    ratio = (price - low) / (high - low)
    if ratio < 0.5:
        return "Discount"
    elif ratio > 0.5:
        return "Premium"
    return "Equilibrium"


def get_fibonacci_levels(high: float, low: float) -> dict[str, float]:
    """Livelli Fibonacci: 0, 0.5, 1. Strategia SMC (pag. 31 manuale):
    'Ti servono solo i livelli 1, 0.5 e -1 sul Fib. Mantienilo pulito!!'"""
    diff = high - low
    return {"0": low, "0.5": low + diff * 0.5, "1": high}


# ---------------------------------------------------------------------------
# SL STRUTTURALE H4 (swing trade)
# ---------------------------------------------------------------------------

def find_h4_structural_sl(
    swings: pd.DataFrame,
    entry: float,
    direction: str,
    min_sl_pips: float,
    max_sl_pips: float,
    pip: float,
) -> float:
    """Trova lo SL basato sulla struttura H4 (swing trade con SL largo)."""
    if swings.empty:
        if direction == "buy":
            return round(entry - min_sl_pips * pip, 5)
        else:
            return round(entry + min_sl_pips * pip, 5)

    min_sl_dist = min_sl_pips * pip
    max_sl_dist = max_sl_pips * pip

    if direction == "buy":
        lows = swings[swings["type"] == "low"].copy()
        if lows.empty:
            return round(entry - min_sl_dist, 5)
        lows = lows.sort_values("price_level", ascending=False)
        best_sl: Optional[float] = None
        for _, row in lows.iterrows():
            sl_candidate = float(row["price_level"]) - 2.0 * pip
            if sl_candidate < entry and (entry - sl_candidate) >= min_sl_dist:
                best_sl = sl_candidate
                break
        if best_sl is None:
            best_sl = entry - min_sl_dist
        if (entry - best_sl) > max_sl_dist:
            best_sl = entry - max_sl_dist
    else:
        highs = swings[swings["type"] == "high"].copy()
        if highs.empty:
            return round(entry + min_sl_dist, 5)
        highs = highs.sort_values("price_level", ascending=True)
        best_sl = None
        for _, row in highs.iterrows():
            sl_candidate = float(row["price_level"]) + 2.0 * pip
            if sl_candidate > entry and (sl_candidate - entry) >= min_sl_dist:
                best_sl = sl_candidate
                break
        if best_sl is None:
            best_sl = entry + min_sl_dist
        if (best_sl - entry) > max_sl_dist:
            best_sl = entry + max_sl_dist

    return round(best_sl, 5)


# ---------------------------------------------------------------------------
# 13. GENERAZIONE SEGNALI
# ---------------------------------------------------------------------------

def generate_signals(
    df: pd.DataFrame, swings: pd.DataFrame, obs_df: pd.DataFrame,
    liquidity_zones: pd.DataFrame, trend: str,
    mode: str | None = None,
) -> list[dict]:
    """Genera segnali operativi dagli OB validati.

    Il target finale segue la liquidità opposta e il minimo R:R della modalità;
    il TP può estendersi liberamente quando la struttura lo giustifica.
    """
    if obs_df.empty:
        return []

    liq_map: dict[str, list[float]] = {"BSL": [], "SSL": []}
    if not liquidity_zones.empty:
        for _, lz in liquidity_zones.iterrows():
            liq_map[lz["type"]].append(float(lz["price_level"]))

    structure_events = swings["structure_event"].tolist() if "structure_event" in swings.columns else []
    signals = []

    for _, ob in obs_df.iterrows():
        tipo = str(ob["tipo_zona"])
        sweep = str(ob.get("liquidity_sweep", "none"))
        pd_zone = str(ob.get("pd_zone", ""))

        if "Demand" in tipo and pd_zone == "Discount":
            direction, entry, sl = "buy", ob["top_ob"], ob["bottom_ob"]
            risk = entry - sl
            tps = sorted([lz for lz in liq_map.get("BSL", []) if lz > entry])
            # Lo swing entra sul bordo iniziale dell'OB. Una liquidità sotto
            # 4R non è un target swing sufficiente: non la usiamo come TP1.
            target_min_rr = _minimum_target_rr(mode, True)
            fallback_rr = _TP1_FALLBACK_MULT
            if mode == "swing":
                tp1 = next((target for target in tps
                            if (target - entry) / risk >= target_min_rr),
                           _swing_target(entry, risk, "buy", target_min_rr))
            else:
                tp1 = tps[0] if tps else entry + risk * fallback_rr
            rr = (tp1 - entry) / risk if risk > 0 else 0
        elif "Supply" in tipo and pd_zone == "Premium":
            direction, entry, sl = "sell", ob["bottom_ob"], ob["top_ob"]
            risk = sl - entry
            tps = sorted([lz for lz in liq_map.get("SSL", []) if lz < entry], reverse=True)
            # Lo swing entra sul bordo iniziale dell'OB. Una liquidità sopra
            # 4R non è un target swing sufficiente: non la usiamo come TP1.
            target_min_rr = _minimum_target_rr(mode, True)
            fallback_rr = _TP1_FALLBACK_MULT
            if mode == "swing":
                tp1 = next((target for target in tps
                            if (entry - target) / risk >= target_min_rr),
                           _swing_target(entry, risk, "sell", target_min_rr))
            else:
                tp1 = tps[0] if tps else entry - risk * fallback_rr
            rr = (entry - tp1) / risk if risk > 0 else 0
        else:
            continue

        if direction == "buy" and trend == "bearish":
            if "TC_bullish" not in structure_events and "MSS_bullish" not in structure_events:
                continue
        if direction == "sell" and trend == "bullish":
            if "TC_bearish" not in structure_events and "MSS_bearish" not in structure_events:
                continue

        # Per lo swing il minimo 1:4 vale già da TP1 e quindi anche sul
        # target finale. Il daytrading usa il minimo 1:2 del materiale base.
        is_pro = (direction == "buy" and trend == "bullish") or (direction == "sell" and trend == "bearish")
        min_rr = _minimum_target_rr(mode, is_pro)
        final_rr = min_rr
        final_distance = max(risk * final_rr, abs(tp1 - entry) + risk)
        tp2 = entry + final_distance if direction == "buy" else entry - final_distance
        rr_final = abs(tp2 - entry) / risk if risk > 0 else 0
        if rr_final < final_rr:
            continue

        signals.append({
            "direction": direction, "entry": round(entry, 5), "sl": round(sl, 5),
            "tp1": round(tp1, 5), "tp2": round(tp2, 5), "rr": round(rr_final, 2),
            "setup_type": "pro_trend" if is_pro else "counter_trend",
            "pd_zone": pd_zone, "sweep_type": sweep, "trend": trend,
            "probability": "high" if sweep != "none" else "medium",
        })
    return signals


# ---------------------------------------------------------------------------
# 14. DETTAGLIO OB POTENZIALI (per notifiche Telegram)
# ---------------------------------------------------------------------------

def compute_ob_potentials(
    obs_df: pd.DataFrame, liquidity_zones: pd.DataFrame,
    trend: str, structure_events: list[str],
    mode: str | None = None,
) -> list[dict]:
    """Calcola entry/sl/tp1/tp2/rr per ogni OB valido e il motivo di scarto."""
    if obs_df.empty:
        return []

    liq_map: dict[str, list[float]] = {"BSL": [], "SSL": []}
    if not liquidity_zones.empty:
        for _, lz in liquidity_zones.iterrows():
            liq_map[lz["type"]].append(float(lz["price_level"]))

    potentials = []
    for _, ob in obs_df.iterrows():
        tipo = str(ob["tipo_zona"])
        pd_zone = str(ob.get("pd_zone", ""))
        top = float(ob["top_ob"])
        bottom = float(ob["bottom_ob"])

        entry = sl = tp1 = tp2 = 0.0
        rr = 0.0
        direction = "none"
        status = ""
        reasons: list[str] = []

        if "Demand" in tipo and pd_zone == "Discount":
            direction = "buy"
            entry, sl = top, bottom
            risk = entry - sl
            tps = sorted([lz for lz in liq_map.get("BSL", []) if lz > entry])
            is_pro = (trend == "bullish")
            min_rr_buy = _minimum_target_rr(mode, is_pro)
            fallback_rr = _TP1_FALLBACK_MULT
            if mode == "swing":
                tp1 = next((target for target in tps
                            if (target - entry) / risk >= min_rr_buy),
                           _swing_target(entry, risk, "buy", min_rr_buy))
            else:
                tp1 = tps[0] if tps else entry + risk * fallback_rr
            final_rr = min_rr_buy
            final_distance = max(risk * final_rr, abs(tp1 - entry) + risk)
            tp2 = entry + final_distance
            rr = (tp2 - entry) / risk if risk > 0 else 0
            if rr < final_rr:
                reasons.append(f"R:R target finale {rr:.2f} < {final_rr}")
            if not is_pro and trend == "bearish":
                if "TC_bullish" not in structure_events and "MSS_bullish" not in structure_events:
                    reasons.append("contro-trend senza TC/MSS")
            status = "ready" if not reasons else "; ".join(reasons)

        elif "Supply" in tipo and pd_zone == "Premium":
            direction = "sell"
            entry, sl = bottom, top
            risk = sl - entry
            tps = sorted([lz for lz in liq_map.get("SSL", []) if lz < entry], reverse=True)
            is_pro = (trend == "bearish")
            min_rr_sell = _minimum_target_rr(mode, is_pro)
            fallback_rr = _TP1_FALLBACK_MULT
            if mode == "swing":
                tp1 = next((target for target in tps
                            if (entry - target) / risk >= min_rr_sell),
                           _swing_target(entry, risk, "sell", min_rr_sell))
            else:
                tp1 = tps[0] if tps else entry - risk * fallback_rr
            final_rr = min_rr_sell
            final_distance = max(risk * final_rr, abs(tp1 - entry) + risk)
            tp2 = entry - final_distance
            rr = (entry - tp2) / risk if risk > 0 else 0
            if rr < final_rr:
                reasons.append(f"R:R target finale {rr:.2f} < {final_rr}")
            if not is_pro and trend == "bullish":
                if "TC_bearish" not in structure_events and "MSS_bearish" not in structure_events:
                    reasons.append("contro-trend senza TC/MSS")
            status = "ready" if not reasons else "; ".join(reasons)

        else:
            status = f"zona non compatibile ({tipo} in {pd_zone})"

        potentials.append({
            "direction": direction,
            "entry": round(entry, 5),
            "sl": round(sl, 5),
            "tp1": round(tp1, 5),
            "tp2": round(tp2, 5),
            "rr": round(rr, 2),
            "pd_zone": pd_zone,
            "sweep": str(ob.get("liquidity_sweep", "none")),
            "ob_top": round(top, 2),
            "ob_bottom": round(bottom, 2),
            "status": status,
        })
    return potentials


# ---------------------------------------------------------------------------
# 15. POI H4 (Fibonacci)
# ---------------------------------------------------------------------------

_POI_FIB_LEVELS = (0.5,)  # solo equilibrio (50%) — strategia SMC pag. 31
_POI_TOLERANCE_PCT = 0.02


def find_h4_poi(swings: pd.DataFrame, current_price: float) -> dict:
    """Identifica il POI su H4 usando Fibonacci HH-LL."""
    result: dict = {
        "poi_found": False, "nearest_level": "", "nearest_price": 0.0,
        "distance_pct": 1.0, "fib_levels": {}, "hh": 0.0, "ll": 0.0,
    }
    if swings.empty or current_price is None:
        return result
    labeled = swings[swings["label"] != ""]
    if len(labeled) < 2:
        return result
    hh_data = labeled[labeled["label"] == "HH"]
    ll_data = labeled[labeled["label"] == "LL"]
    if hh_data.empty or ll_data.empty:
        return result
    hh = float(hh_data["price_level"].iloc[-1])
    ll = float(ll_data["price_level"].iloc[-1])
    if hh <= ll:
        return result
    fib_levels = get_fibonacci_levels(hh, ll)
    result["fib_levels"] = fib_levels
    result["hh"] = hh
    result["ll"] = ll
    range_pips = hh - ll
    best_dist = float("inf")
    for level_name in _POI_FIB_LEVELS:
        level_key = str(level_name)
        if level_key in fib_levels:
            level_price = fib_levels[level_key]
            dist_pct = abs(current_price - level_price) / range_pips
            if dist_pct < best_dist:
                best_dist = dist_pct
                result["nearest_level"] = level_key
                result["nearest_price"] = level_price
    result["distance_pct"] = round(best_dist, 4)
    if best_dist <= _POI_TOLERANCE_PCT:
        result["poi_found"] = True
    return result


# ---------------------------------------------------------------------------
# 16. PIPELINE COMPLETA
# ---------------------------------------------------------------------------

def analyze_symbol(
    symbol: str, timeframe: int, bars: int = 200, pivot_window: int = 4,
    h4_equilibrium: Optional[float] = None,
    ob_lookback: int = 10,
    shallow_pd_pct: Optional[float] = None,
    pd_range_high: Optional[float] = None,
    pd_range_low: Optional[float] = None,
    mode: str | None = None,
) -> dict:
    """Pipeline completa SMC per un simbolo.

    Args:
        h4_equilibrium: se fornito, lo usa per la matrice PD invece del
                        range locale (strategia: 'H4 prevale su M15').
        ob_lookback: quante barre indietro cercare per l'OB (default 10,
                     ridotto a 5 per M15 per OB piu' freschi).
    """
    result = {
        "symbol": symbol, "timeframe": timeframe, "success": False,
        "trend": "sideways", "signals": [], "swings_count": 0, "obs_count": 0,
        "current_price": None, "error": None, "ob_potentials": [],
        "structure_events": [],
    }
    try:
        df = get_market_data(symbol, timeframe, bars)
        if df is None or df.empty:
            result["error"] = "Nessun dato"
            return result

        df = identify_swings(df, window=pivot_window)
        swings = filter_alternating_swings(df)
        if swings.empty:
            result["error"] = "Nessuno swing"
            return result

        swings = label_structure(swings)
        swings = classify_strong_weak(swings)
        swings = detect_structure_breaks(swings)

        # Throttle log diagnostici OB: valutato UNA volta per tutta la pipeline
        # Usa symbol+timeframe come chiave così H4 e M15 non condividono lo slot
        _ob_log = _can_log_ob_debug(f"{symbol}_TF{timeframe}")

        obs = identify_order_blocks(df, swings, lookback=ob_lookback)
        raw_obs = len(obs)
        raw_demand = len(obs[obs["tipo_zona"].str.contains("Demand", na=False)]) if not obs.empty else 0
        raw_supply = len(obs[obs["tipo_zona"].str.contains("Supply", na=False)]) if not obs.empty else 0
        if _ob_log:
            logger.info(
                "[%s] [OB-RAW] %d trovati: %d Demand, %d Supply",
                symbol, raw_obs, raw_demand, raw_supply,
            )
        if obs.empty:
            result["swings_count"] = len(swings)
            result["trend"] = get_trend_direction(swings)
            result["current_price"] = float(df["close"].iloc[-1])
            result["success"] = True
            result["obs_raw_count"] = 0
            return result

        obs = filter_mitigated_obs(df, obs)
        after_mit = len(obs)
        mitigated_count = raw_obs - after_mit
        if _ob_log:
            logger.info(
                "[%s] [OB-MIT] %d mitigati → rimossi | %d rimanenti",
                symbol, mitigated_count, after_mit,
            )

        obs = apply_pd_matrix(swings, obs, h4_equilibrium=h4_equilibrium,
                               shallow_pd_pct=shallow_pd_pct,
                               pd_range_high=pd_range_high,
                               pd_range_low=pd_range_low)
        after_pd = len(obs)
        pd_discarded = after_mit - after_pd
        premium_obs = len(obs[obs["pd_zone"] == "Premium"]) if not obs.empty else 0
        discount_obs = len(obs[obs["pd_zone"] == "Discount"]) if not obs.empty else 0
        if _ob_log:
            logger.info(
                "[%s] [OB-PD] %d scartati dalla matrice PD | %d rimanenti (%d Premium, %d Discount)",
                symbol, pd_discarded, after_pd, premium_obs, discount_obs,
            )

        obs = detect_liquidity_sweeps(df, obs)
        obs_with_sweep = len(obs[obs["liquidity_sweep"] != "none"]) if not obs.empty else 0
        if obs_with_sweep > 0 and _ob_log:
            logger.info(
                "[%s] [OB-SWEEP] %d OB con liquidity sweep rilevato",
                symbol, obs_with_sweep,
            )

        liq_zones = find_liquidity_zones(df, swings)
        trend = get_trend_direction(swings)
        signals = generate_signals(df, swings, obs, liq_zones, trend, mode=mode)

        structure_events = swings["structure_event"].tolist() if "structure_event" in swings.columns else []
        ob_potentials = compute_ob_potentials(obs, liq_zones, trend, structure_events, mode=mode)

        result.update({
            "success": True, "trend": trend, "signals": signals,
            "structure_events": structure_events,
            "swings_count": len(swings), "obs_count": len(obs),
            "current_price": float(df["close"].iloc[-1]),
            "ob_potentials": ob_potentials,
            "obs_raw_count": raw_obs,
            "obs_after_mit": after_mit,
            "obs_after_pd": after_pd,
            "obs_with_sweep": obs_with_sweep,
        })
    except Exception as e:
        logger.exception("Errore analisi SMC %s: %s", symbol, e)
        result["error"] = str(e)
    return result
