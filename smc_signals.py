"""
smc_signals.py
==============
Funzioni strategiche SMC per la generazione e validazione dei segnali.

Contiene le 6 funzioni chiave della strategia avanzata:
    - is_institutional_candle()      → classifica candele istituzionali vs retail
    - classify_reversal()            → inversione istituzionale o retail pullback
    - classify_setup_type()          → 3 tipi: manipulation / news / exhaustion
    - is_liquidity_cleared()         → check liquidità retail dietro entry/SL
    - find_opposite_liquidity_target() → TP da liquidità H4 opposta
    - has_h4_liquidity_sweep()       → condizione PRIMARIA: sweep H4 recente
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

import config
import utils
from structure_analyzer import find_liquidity_zones

logger = logging.getLogger(__name__)


def validate_swing_context(
    *,
    htf_ready: bool,
    htf_trend: str,
    sweep_check: dict,
    reversal: dict,
) -> tuple[bool, str]:
    """Applica i gate obbligatori della sequenza swing dei video.

    La direzione nasce dall'HTF; prima dell'ingresso devono essere presenti
    sweep della liquidità e inversione istituzionale. Un contesto incompleto
    non viene trasformato in un segnale tramite fallback.
    """
    if not htf_ready:
        return False, "analisi HTF non disponibile"
    if htf_trend not in ("bullish", "bearish"):
        return False, "trend HTF non direzionale"
    if not sweep_check.get("swept"):
        return False, "nessun liquidity sweep HTF recente"
    if sweep_check.get("bars_ago", 999) > 5:
        return False, "liquidity sweep HTF troppo vecchio"
    sweep_type = sweep_check.get("type")
    expected_direction = {"SSL": "buy", "BSL": "sell"}.get(sweep_type)
    expected_trend = {"buy": "bullish", "sell": "bearish"}.get(expected_direction)
    if expected_trend != htf_trend:
        return False, "sweep HTF non coerente con il trend"
    if reversal.get("type") != "institutional":
        return False, "inversione non istituzionale dopo lo sweep"
    if not reversal.get("displacement", False):
        return False, "inversione istituzionale senza displacement"
    return True, "ok"


def validate_swing_ltf_confirmation(
    *,
    ltf_ready: bool,
    ltf_obs_count: int,
    confirmed_count: int,
    structure_confirmed_count: int = 0,
) -> tuple[bool, str]:
    """Impedisce allo swing di entrare senza POI e conferma strutturale LTF."""
    if not ltf_ready or ltf_obs_count <= 0:
        return False, "conferma LTF non disponibile"
    if confirmed_count <= 0:
        return False, "nessun POI/OB LTF confermato"
    if structure_confirmed_count <= 0:
        return False, "nessuna conferma LTF TC o MSS+SB"
    return True, "ok"


def _directional_structure_event(events: object, direction: str) -> bool:
    """Ritorna True se gli eventi includono MSS o TC nella direzione richiesta."""
    if not events:
        return False
    event_set = {str(event) for event in events}
    event_direction = {"buy": "bullish", "sell": "bearish"}.get(
        direction.lower(), direction.lower()
    )
    return bool(event_set & {f"MSS_{event_direction}", f"TC_{event_direction}"})


def validate_daytrading_ltf_confirmation(
    *,
    direction: str,
    m5_ready: bool,
    m5_events: object,
    m1_ready: bool,
    m1_events: object,
) -> tuple[bool, str]:
    """Richiede una conferma strutturale reale sia su M5 sia su M1.

    La sola concordanza della direzione non è una conferma: ogni timeframe
    operativo deve mostrare almeno un MSS o TC nella direzione del segnale.
    """
    if not m5_ready:
        return False, "M5 non disponibile"
    if not _directional_structure_event(m5_events, direction):
        return False, f"M5 senza MSS/TC {direction}"
    if not m1_ready:
        return False, "M1 non disponibile"
    if not _directional_structure_event(m1_events, direction):
        return False, f"M1 senza MSS/TC {direction}"
    return True, "ok"


def validate_daytrading_counter_trend(
    *,
    direction: str,
    trend: str,
    sweep_check: dict,
    structure_events: object,
) -> tuple[bool, str]:
    """Applica il gate sweep + MSS/TC ai soli setup counter-trend."""
    is_pro = (direction == "buy" and trend == "bullish") or (
        direction == "sell" and trend == "bearish"
    )
    if is_pro:
        return True, "pro-trend"
    if not sweep_check.get("swept"):
        return False, "counter-trend senza liquidity sweep"
    if sweep_check.get("bars_ago", 999) > 5:
        return False, "counter-trend con sweep troppo vecchio"
    expected_sweep = {"buy": "SSL", "sell": "BSL"}.get(direction)
    if sweep_check.get("type") != expected_sweep:
        return False, "counter-trend con sweep non coerente"
    if not _directional_structure_event(structure_events, direction):
        return False, f"counter-trend senza MSS/TC {direction}"
    return True, "ok"


# ---------------------------------------------------------------------------
# 1. CANDELA ISTITUZIONALE
# ---------------------------------------------------------------------------

def is_institutional_candle(candle_row: pd.Series, avg_range: float = 0) -> str:
    """Classifica una candela come istituzionale o retail.

    Strategia SMC: le istituzioni si vedono dalle candele.
    - Istituzionale: corpo pieno (>60% del range), decisa, veloce.
    - Retail: doji, indecisione, tante ombre.

    Returns: 'institutional' | 'retail'
    """
    high = float(candle_row["high"])
    low = float(candle_row["low"])
    open_p = float(candle_row["open"])
    close_p = float(candle_row["close"])
    candle_range = high - low
    body = abs(close_p - open_p)

    if candle_range <= 0:
        return "retail"

    body_ratio = body / candle_range
    is_bullish = close_p > open_p  # noqa: F841 — usato per leggibilità futura

    # Istituzionale: corpo >60% del range, non una doji
    if body_ratio >= 0.60:
        return "institutional"
    # Range grosso ma corpo piccolo = retail (indecisione)
    if avg_range > 0 and candle_range > avg_range * 1.5 and body_ratio < 0.40:
        return "retail"
    # Doji: corpo <10% del range
    if body_ratio < 0.10:
        return "retail"
    # Default: corpo medio = potenziale retail
    return "retail"


# ---------------------------------------------------------------------------
# 2. CLASSIFICAZIONE INVERSIONE
# ---------------------------------------------------------------------------

def classify_reversal(
    df: pd.DataFrame, swings: pd.DataFrame,
    liquidity_price: float, direction: str,
) -> dict:
    """Classifica l'inversione a un livello di liquidita'.

    Strategia SMC: dopo uno sweep di liquidita', l'inversione puo' essere:
    - istituzionale (candela forte, full body, veloce, close oltre livello) → ENTRARE
    - retail (lenta, doji, close NON oltre livello) → ASPETTARE

    Returns: {'type': 'institutional'|'retail_pullback'|'unknown',
              'confidence': 0-100, 'candles_count': int}
    """
    if df is None or len(df) < 5:
        return {"type": "unknown", "confidence": 0, "candles_count": 0}

    # Trova l'indice della candela che ha causato lo sweep CON close oltre livello
    idx = len(df) - 1
    found = False
    for i in range(len(df) - 1, max(len(df) - 30, 0), -1):
        row = df.iloc[i]
        if direction == "buy":
            # Per SSL sweep: low buca sotto E close torna SOPRA = sweep valido
            if float(row["low"]) <= liquidity_price and float(row["close"]) > liquidity_price:
                idx = i
                found = True
                break
        else:
            # Per BSL sweep: high buca sopra E close torna SOTTO = sweep valido
            if float(row["high"]) >= liquidity_price and float(row["close"]) < liquidity_price:
                idx = i
                found = True
                break

    if not found:
        return {"type": "retail_pullback", "confidence": 90, "candles_count": 0}

    # Analizza le 5 candele dopo lo sweep per determinare tipo inversione
    institutional_count = 0
    candles_checked = 0
    for i in range(idx, min(idx + 5, len(df))):
        candle_type = is_institutional_candle(df.iloc[i])
        candles_checked += 1
        if candle_type == "institutional":
            institutional_count += 1

    if candles_checked == 0:
        return {"type": "unknown", "confidence": 0, "candles_count": 0}

    ratio = institutional_count / candles_checked
    # In assenza di volume affidabile, displacement viene definito in modo
    # conservativo: almeno una candela full-body nella sequenza di inversione.
    displacement = institutional_count > 0
    if ratio >= 0.50:  # almeno 50% candele istituzionali
        return {
            "type": "institutional", "confidence": int(ratio * 100),
            "candles_count": candles_checked, "displacement": displacement,
        }
    elif institutional_count == 0:
        return {
            "type": "retail_pullback", "confidence": 80,
            "candles_count": candles_checked, "displacement": False,
        }
    else:
        return {
            "type": "unknown", "confidence": int(ratio * 100),
            "candles_count": candles_checked, "displacement": displacement,
        }


# ---------------------------------------------------------------------------
# 3. CLASSIFICAZIONE SETUP (3 tipi del corso SMC)
# ---------------------------------------------------------------------------

def classify_setup_type(
    sweep_type: str,
    reversal: dict,
    near_news: bool,
    trend: str,
    direction: str,
    dxy_conflict: bool = False,
) -> tuple[str, str]:
    """Classifica il setup tra i 3 tipi della strategia SMC.

    Strategia del corso (Video 17, 18):
    (1) 'manipulation'  — Movimento pulito: sweep liquida + BOS + inversione istituzionale
    (2) 'news'          — Trading su news: orari chiave, liquidita' sopra/sotto
    (3) 'exhaustion'    — Sfinimento: trend stabile, rientro dopo pullback

    Il parametro dxy_conflict (True = trade contro trend DXY) riduce il livello
    del setup: un 'exhaustion' con conflitto DXY viene declassato a 'generic'
    perche' il macro-trend del dollaro invalida la continuazione locale.

    Returns:
        (setup_type: str, setup_detail: str)
    """
    is_pro = (direction == "buy" and trend == "bullish") or (direction == "sell" and trend == "bearish")

    # Etichetta di conflitto DXY da appendere al dettaglio
    dxy_note = " | ⚠️ DXY CONFLICT: trade contro trend del Dollaro" if dxy_conflict else ""

    # Caso 1: Manipulation — sweep pulito + inversione istituzionale
    if sweep_type != "none" and reversal.get("type") == "institutional":
        detail = (
            f"Manipolazione con sweep {sweep_type} + inversione ist. "
            f"({reversal.get('confidence', 0)}%){dxy_note}"
        )
        return ("manipulation", detail)

    # Caso 2: News — orari di news, volatilita' attesa
    if near_news:
        detail = f"News in corso: IPC/PIL/NFP/FOMC/disoccupazione. Ridotto rischio.{dxy_note}"
        return ("news", detail)

    # Caso 3: Exhaustion — trend stabile, rientro in zona PD
    # Se c'e' conflitto DXY, il setup non e' realmente 'pro-trend'
    # perche' il macro-trend del dollaro si oppone. Declassiamo a 'generic'.
    if is_pro:
        if dxy_conflict:
            detail = (
                "Trend continuazione MA con conflitto DXY: il macro-trend del dollaro "
                "si oppone. Rischio maggiore, setup declassato."
            )
            return ("generic", detail)
        detail = "Trend continuazione: rientro in zona PD dopo pullback."
        return ("exhaustion", detail)

    # Default
    detail = f"Setup generico (nessuna classificazione specifica).{dxy_note}"
    return ("generic", detail)


# ---------------------------------------------------------------------------
# 4. CHECK LIQUIDITA' DIETRO
# ---------------------------------------------------------------------------

def is_liquidity_cleared(
    df: pd.DataFrame, entry: float, sl: float, direction: str,
) -> tuple[bool, str]:
    """Verifica che tra entry e SL non ci sia liquidita' retail in sospeso.

    Strategia SMC: 'Hanno tolto la liquidita' o sono io la liquidita'?'
    Se tra entry e SL ci sono candele retail (doji, indecisione),
    il prezzo deve prima tornare a pulirle.

    Returns: (is_clear, detail_message)
    """
    if df is None or len(df) < 10:
        return True, "dati insufficienti"

    retail_candles = 0
    total_in_zone = 0

    for i in range(len(df) - 1, max(len(df) - 50, 0), -1):
        row = df.iloc[i]
        price = float(row["close"])

        if direction == "buy":
            in_zone = sl < price < entry
        else:
            in_zone = entry < price < sl

        if in_zone:
            total_in_zone += 1
            if is_institutional_candle(row) == "retail":
                retail_candles += 1

    if total_in_zone == 0:
        return True, "zona pulita"

    retail_pct = retail_candles / total_in_zone if total_in_zone > 0 else 0
    if retail_pct > 0.50:
        return False, f"{retail_candles}/{total_in_zone} candele retail tra entry e SL"
    return True, f"zona accettabile ({retail_candles}/{total_in_zone} retail)"


# ---------------------------------------------------------------------------
# 4b. FILTRO AMBIENTE DI LIQUIDITA' (prima dell'ingresso)
# ---------------------------------------------------------------------------

def validate_liquidity_environment(
    liq_zones: pd.DataFrame,
    entry: float,
    sl: float,
    direction: str,
    min_rr: float,
    buffer_r: float = 1.0,
    mode: str | None = None,
) -> tuple[bool, str]:
    """Filtro di liquidità applicato prima di ogni ingresso.

    Regola "Hanno tolto la liquidità o sono io la liquidità?":
      1. Nessuna zona di liquidità (di QUALSIASI lato) nel corridoio
         immediatamente davanti all'entry (buffer ``buffer_r`` rischi): una
         zona lì verrebbe spazzata contro la posizione prima del target.
      2. Liquidità opposta abbastanza lontana: per lo swing, il cui target
         nasce proprio dalle zone H4 opposte, la più vicina deve distare
         almeno ``min_rr``. Per il daytrading (target su M5/M15) la vicinanza
         di una zona H4 non riguarda il trade e NON è un motivo di rifiuto.

    Returns:
        (is_ok, detail)
    """
    if liq_zones is None or liq_zones.empty or min_rr <= 0:
        return True, "liquidità non disponibile"

    side = direction.lower()
    risk = abs(entry - sl)
    if risk <= 0:
        return True, "rischio non calcolabile"

    # 1. Corridoio davanti all'entry: consideriamo TUTTE le zone, perché una
    #    liquidità stessa-side o opposta a pochi tick può essere spazzata
    #    contro la posizione nel percorso verso il target.
    buffer_distance = buffer_r * risk
    blocked: Optional[float] = None
    blocked_type = ""
    for _, zone in liq_zones.iterrows():
        price = float(zone["price_level"])
        zone_type = str(zone.get("type", ""))
        if side == "buy" and entry < price < entry + buffer_distance:
            blocked = price
            blocked_type = zone_type
            break
        if side == "sell" and entry - buffer_distance < price < entry:
            blocked = price
            blocked_type = zone_type
            break
    if blocked is not None:
        return False, (
            f"liquidità {blocked_type} a {(abs(blocked - entry) / risk):.1f}R "
            f"davanti all'entry"
        )

    # 2. Distanza della liquidità opposta (solo dove il target la usa).
    opposite = {"buy": "BSL", "sell": "SSL"}.get(side, "")
    opposite_zones = []
    for _, zone in liq_zones.iterrows():
        if str(zone.get("type", "")) != opposite:
            continue
        price = float(zone["price_level"])
        if side == "buy" and price > entry:
            opposite_zones.append(price)
        elif side == "sell" and price < entry:
            opposite_zones.append(price)
    if not opposite_zones:
        return True, "nessuna liquidità opposta nelle zone H4"

    if mode == "swing":
        nearest_opposite = min(opposite_zones) if side == "buy" else max(opposite_zones)
        distance_r = abs(nearest_opposite - entry) / risk
        if distance_r < min_rr - 1e-9:
            return False, (
                f"liquidità opposta a {distance_r:.1f}R (< {min_rr:.1f}R richiesti)"
            )
        return True, f"liquidità ok (opposta a {distance_r:.1f}R)"

    return True, "liquidità ok"


# ---------------------------------------------------------------------------
# 4c. GATE LIQUIDITÀ PRE-ENTRY
# ---------------------------------------------------------------------------

def _iter_liquidity_levels(
    liq_zones: pd.DataFrame | None,
    swings: pd.DataFrame | None,
) -> list[tuple[float, str]]:
    """Raccoglie zone e livelli strutturali senza assumere colonne opzionali."""
    levels: list[tuple[float, str]] = []
    for frame, default_type in ((liq_zones, ""), (swings, "")):
        if frame is None or frame.empty:
            continue
        for _, row in frame.iterrows():
            raw_price = row.get("price_level", row.get("high", row.get("low")))
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(price) or price <= 0:
                continue
            level_type = str(row.get("type", row.get("liquidity_type", default_type)))
            if not level_type and "high" in row and "low" not in row:
                level_type = "BSL"
            levels.append((price, level_type))
    return levels


def _find_clean_sweep_candle(
    df: pd.DataFrame | None,
    sweep_check: dict,
    symbol: str = "EURUSD",
) -> tuple[bool, str]:
    """Verifica penetrazione e reclaim della candela che ha preso la liquidità."""
    if not sweep_check.get("swept"):
        return False, "nessun liquidity sweep"
    sweep_type = str(sweep_check.get("type", ""))
    try:
        level = float(sweep_check.get("price", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False, "sweep senza livello numerico"
    if sweep_type not in {"BSL", "SSL"} or not np.isfinite(level) or level <= 0:
        return False, "sweep senza tipo o livello valido"

    # Il rilevatore H4 annota l'indice della candela. Per payload/test esterni
    # accettiamo anche una candela esplicita, mantenendo la funzione testabile.
    candle = sweep_check.get("sweep_candle")
    if candle is None and df is not None and not df.empty:
        idx = sweep_check.get("sweep_idx")
        if idx is not None:
            try:
                candle = df.iloc[int(idx)]
            except (IndexError, TypeError, ValueError):
                candle = None
    if candle is None:
        # Senza OHLC non possiamo dimostrare penetrazione e reclaim. Il flag
        # legacy ``clean`` non è sufficiente: impedirebbe il controllo rigoroso
        # richiesto e potrebbe trasformare un livello non verificato in uno
        # sweep istituzionale.
        return False, "candela sweep non disponibile"

    try:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
    except (KeyError, TypeError, ValueError):
        return False, "candela sweep incompleta"

    if sweep_type == "BSL":
        penetration = high - level
        reclaim = level - close
    else:
        penetration = level - low
        reclaim = close - level
    # Un semplice attraversamento del livello (o una differenza dovuta a
    # rounding/tick) non è una presa di liquidità: richiediamo una penetrazione
    # reale e un reclaim positivo sopra la soglia configurata.
    if not np.isfinite(penetration) or not np.isfinite(reclaim):
        return False, f"{sweep_type} con dati non finiti"
    pip = utils.pip_size(symbol)
    min_penetration = max(
        0.0, float(config.LIQUIDITY_MIN_SWEEP_PENETRATION_PIPS)
    ) * pip
    # La pipeline autonoma aggiunge il simbolo al payload; i test/payload
    # legacy senza simbolo usano EURUSD come fallback conservativo.
    if penetration < max(min_penetration, np.finfo(float).eps):
        return False, (
            f"{sweep_type} penetrazione insufficiente "
            f"({penetration / pip:.2f} pip)"
        )
    if reclaim <= 0:
        return False, f"{sweep_type} senza reclaim oltre il livello"
    reclaim_ratio = reclaim / penetration
    if reclaim_ratio < config.LIQUIDITY_MIN_SWEEP_RECLAIM_RATIO:
        return False, (
            f"reclaim sweep insufficiente ({reclaim_ratio:.0%} < "
            f"{config.LIQUIDITY_MIN_SWEEP_RECLAIM_RATIO:.0%})"
        )
    return True, f"{sweep_type} pulito (reclaim {reclaim_ratio:.0%})"


def _is_truthy_flag(value: object) -> bool:
    """Interpreta un flag pandas senza considerare NaN/NA come True."""
    try:
        if value is None or not bool(pd.notna(value)):
            return False
        return bool(value)
    except (TypeError, ValueError):
        return False


def _has_unmitigated_retail_level(
    df: pd.DataFrame | None,
    swings: pd.DataFrame | None,
    entry: float,
    sl: float,
    direction: str,
    risk: float,
) -> tuple[bool, str]:
    """Cerca swing/retail levels ancora non mitigati nel corridoio di rischio."""
    if swings is not None and not swings.empty:
        tolerance = risk * config.LIQUIDITY_CLEARED_LEVEL_TOLERANCE_R
        for _, row in swings.iterrows():
            try:
                level = float(row.get("price_level"))
            except (TypeError, ValueError):
                continue
            in_risk_zone = (
                sl + tolerance < level < entry - tolerance
                if direction == "buy"
                else entry + tolerance < level < sl - tolerance
            )
            if not in_risk_zone:
                continue
            # Un flag proveniente da un payload esterno non è sufficiente da
            # solo: può essere stale o non verificato. Accettiamo il bypass
            # soltanto quando il producer dichiara esplicitamente che la
            # mitigazione è stata verificata su OHLC.
            mitigation_verified = _is_truthy_flag(
                row.get("mitigation_verified")
            )
            if mitigation_verified and (
                _is_truthy_flag(row.get("mitigated"))
                or _is_truthy_flag(row.get("liquidity_cleared"))
            ):
                continue

            # Un livello si considera pulito solo dopo una successiva
            # penetrazione: il semplice fatto che sia uno swing non basta.
            mitigated = False
            if df is not None and not df.empty and "time" in df.columns and "time" in row:
                try:
                    after = df[df["time"] > row["time"]]
                    if direction == "buy":
                        mitigated = bool((after["low"] <= level).any())
                    else:
                        mitigated = bool((after["high"] >= level).any())
                except (KeyError, TypeError):
                    mitigated = False
            if not mitigated:
                return True, f"livello retail non mitigato a {level:.5f} tra entry e SL"

    # Mantiene il controllo storico sulle candele indecise, ma lo applica in
    # aggiunta ai livelli strutturali e non come loro sostituto.
    if df is not None:
        clear, detail = is_liquidity_cleared(df, entry, sl, direction)
        if not clear:
            return True, detail
    return False, "corridoio entry-SL pulito"


def validate_pre_entry_liquidity(
    *,
    df: pd.DataFrame | None,
    swings: pd.DataFrame | None,
    liq_zones: pd.DataFrame | None,
    sweep_check: dict,
    reversal: dict | None,
    entry: float,
    sl: float,
    direction: str,
    min_rr: float,
    mode: str | None = None,
    target_levels: object = None,
    avg_range_price: float = 0.0,
    require_sweep: bool = True,
    symbol: str = "EURUSD",
) -> tuple[bool, str]:
    """Gate finale di liquidità: nessun ingresso se il percorso non è pulito.

    Controlli, nell'ordine:
      1. sweep coerente, recente e realmente reclaimato;
      2. nessun livello di liquidità immediatamente davanti all'entry;
      3. prima liquidità opposta utile a distanza sufficiente per il R:R;
      4. nessun livello retail non mitigato tra entry e SL;
      5. entry non già estesa rispetto allo sweep o all'ampiezza media.

    ``require_sweep=False`` è usato solo dal daytrading quando il segnale
    porta già uno sweep sul timeframe d'ingresso ma non sul contesto H4.
    """
    try:
        entry = float(entry)
        sl = float(sl)
        risk = abs(entry - sl)
    except (TypeError, ValueError):
        return False, "entry/SL non numerici"
    if not np.isfinite(entry) or not np.isfinite(sl) or risk <= 0:
        return False, "rischio non valido"

    if require_sweep:
        effective_symbol = str(sweep_check.get("symbol") or symbol)
        sweep_ok, sweep_reason = _find_clean_sweep_candle(
            df, sweep_check, symbol=effective_symbol,
        )
        if not sweep_ok:
            return False, sweep_reason
        sweep_type = str(sweep_check.get("type", ""))
        expected = {"buy": "SSL", "sell": "BSL"}.get(direction.lower())
        if sweep_type != expected:
            return False, f"sweep {sweep_type} non coerente con {direction}"
        if sweep_check.get("bars_ago", 999) > 5:
            return False, "liquidity sweep troppo vecchio"
        if reversal:
            if reversal.get("type") != "institutional":
                return False, "sweep seguito da inversione retail"
            if reversal.get("displacement") is False:
                return False, "sweep senza displacement istituzionale"

    # Davanti e per il target consideriamo le zone qualificate (cluster
    # BSL/SSL). Gli swing singoli vengono aggiunti al controllo degli ostacoli:
    # un livello strutturale non deve essere ignorato solo perché non forma un
    # cluster equal-high/equal-low.
    qualified_levels = _iter_liquidity_levels(liq_zones, None)
    structural_levels = _iter_liquidity_levels(None, swings)
    front_levels = qualified_levels + structural_levels
    if require_sweep:
        sweep_price = float(sweep_check.get("price", 0.0) or 0.0)
        sweep_type = str(sweep_check.get("type", ""))
        residual_type = sweep_type
        residual_limit = risk * config.LIQUIDITY_FRONT_BUFFER_R
        for price, level_type in qualified_levels:
            if level_type != residual_type:
                continue
            if abs(price - sweep_price) <= residual_limit and abs(price - sweep_price) > 1e-12:
                return False, (
                    f"liquidità {level_type} residua a "
                    f"{abs(price - sweep_price) / risk:.1f}R dallo sweep"
                )

    front_distance = config.LIQUIDITY_FRONT_BUFFER_R * risk
    side = direction.lower()
    for price, level_type in front_levels:
        in_front = (
            entry < price <= entry + front_distance
            if side == "buy" else
            entry - front_distance <= price < entry
        )
        if in_front:
            return False, (
                f"liquidità {level_type or 'strutturale'} a "
                f"{abs(price - entry) / risk:.1f}R davanti all'entry"
            )

    # Per lo swing il target deve essere una liquidità reale; per il
    # daytrading si controllano eventuali target esplicitamente passati.
    targets: list[float] = []
    if target_levels is not None:
        # TP3 può essere None: un elemento invalido non deve cancellare
        # TP1/TP2 validi e disattivare il controllo R:R.
        try:
            for value in target_levels:
                if value is None:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(numeric) and numeric > 0:
                    targets.append(numeric)
        except TypeError:
            targets = []
    if mode == "swing" or targets:
        opposite = {"buy": "BSL", "sell": "SSL"}.get(side, "")
        targets = [
            price for price in targets
            if (price > entry if side == "buy" else price < entry)
        ]
        if not targets:
            targets = [
                price for price, level_type in qualified_levels
                if level_type == opposite and (
                    price > entry if side == "buy" else price < entry
                )
            ]
        if not targets and mode == "swing":
            # Mantieni la diagnosi più specifica quando il setup è già
            # completamente esteso: l'assenza di un target non deve nascondere
            # il motivo operativo principale del rifiuto.
            sweep_price = float(sweep_check.get("price", 0.0) or 0.0)
            if sweep_price > 0:
                extension_r = abs(entry - sweep_price) / risk
                if extension_r > config.LIQUIDITY_MAX_ENTRY_EXTENSION_R:
                    return False, (
                        f"movimento esteso: entry a {extension_r:.1f}R dallo sweep "
                        f"(massimo {config.LIQUIDITY_MAX_ENTRY_EXTENSION_R:.1f}R)"
                    )
            return False, "nessuna liquidità opposta disponibile per lo swing"
        if targets:
            nearest = min(targets) if side == "buy" else max(targets)
            target_rr = abs(nearest - entry) / risk
            if target_rr < max(float(min_rr), 0.0) - 1e-9:
                return False, (
                    f"liquidità opposta a {target_rr:.1f}R "
                    f"(< {float(min_rr):.1f}R richiesti)"
                )

    if swings is not None or df is not None:
        retail_level, retail_reason = _has_unmitigated_retail_level(
            df, swings, entry, sl, side, risk,
        )
        if retail_level:
            return False, retail_reason

    if require_sweep:
        sweep_price = float(sweep_check.get("price", 0.0) or 0.0)
        extension_r = abs(entry - sweep_price) / risk if sweep_price > 0 else float("inf")
        if extension_r > config.LIQUIDITY_MAX_ENTRY_EXTENSION_R:
            return False, (
                f"movimento esteso: entry a {extension_r:.1f}R dallo sweep "
                f"(massimo {config.LIQUIDITY_MAX_ENTRY_EXTENSION_R:.1f}R)"
            )
        if avg_range_price > 0 and abs(entry - sweep_price) > (
            config.LIQUIDITY_MAX_ENTRY_EXTENSION_ATR * avg_range_price
        ):
            return False, (
                f"movimento esteso: distanza entry/sweep oltre "
                f"{config.LIQUIDITY_MAX_ENTRY_EXTENSION_ATR:.1f}x ATR"
            )

    return True, "liquidità pre-entry pulita"


# ---------------------------------------------------------------------------
# 4d. FILTRO SPREAD E VOLATILITA' (punto 6)
# ---------------------------------------------------------------------------

def validate_volatility_filter(
    symbol: str,
    direction: str,
    entry: float,
    sl: float,
    spread_pips: float,
    avg_range_pips: float,
    mode: str | None = None,
    *,
    tp_price: float | None = None,
    fast_avg_range_pips: float = 0.0,
    current_range_pips: float = 0.0,
    require_fast_range: bool = False,
    require_slow_range: bool = False,
) -> tuple[bool, str]:
    """Filtro spread/volatilità applicato prima di ogni ingresso.

    Un setup tecnicamente valido può perdere a causa di spread eccessivo o
    volatilità anomala. Controlli:
      1. Spread in pip: deve stare sotto ``MAX_SPREAD_PIPS`` del simbolo.
      2. Spread vs SL: lo spread non deve assorbire più di
         ``MAX_SPREAD_TO_SL_RATIO`` della distanza SL.
      3. Volatilità: lo SL deve essere proporzionato all'ampiezza media delle
         candele del timeframe operativo (``avg_range_pips``): troppo piccolo
         = stop da rumore; troppo grande = movimento già esteso.
      4. Il TP più lontano deve essere raggiungibile entro un numero massimo
         di range medi, usando sia il timeframe operativo sia quello veloce.
      5. La candela corrente non deve essere uno spike anomalo rispetto alla
         media del timeframe veloce.

    Args:
        symbol: simbolo (per il massimo spread per simbolo).
        direction: 'buy' | 'sell'.
        entry, sl: livelli del setup in prezzo.
        spread_pips: spread corrente in pip (ask-bid / pip_size).
        avg_range_pips: ampiezza media (high-low) del timeframe operativo
            (M15 per il daytrading, H1 per lo swing).
        mode: 'daytrading' | 'swing'.
        tp_price: TP più lontano del piano, opzionale per retrocompatibilità.
        fast_avg_range_pips: media del timeframe veloce (M5 per daytrading,
            M15 per swing).
        current_range_pips: ampiezza dell'ultima candela chiusa del timeframe
            veloce; zero significa dato non disponibile.
        require_fast_range: se True, rifiuta dati veloci mancanti (usato dalla
            pipeline autonoma; le chiamate legacy restano compatibili).
        require_slow_range: se True, rifiuta dati lenti mancanti invece di
            saltare i controlli SL/range (usato dalle pipeline live).

    Returns:
        (is_ok, detail)
    """
    # Dati spread non disponibili (tick assente): il controllo spread è
    # fail-open (spread=0), ma non deve disattivare i controlli indipendenti
    # di volatilità e raggiungibilità del TP. Dati esplicitamente invalidi,
    # invece, bloccano il trade: non è sicuro trattare NaN/valori negativi
    # come se il mercato fosse semplicemente privo di quotazione.
    try:
        entry = float(entry)
        sl = float(sl)
        spread_pips = float(spread_pips)
        avg_range_pips = float(avg_range_pips)
        fast_avg_range_pips = float(fast_avg_range_pips)
        current_range_pips = float(current_range_pips)
    except (TypeError, ValueError):
        return False, "dati spread/volatilità non numerici"

    values = (entry, sl, spread_pips, avg_range_pips,
              fast_avg_range_pips, current_range_pips)
    if not all(np.isfinite(value) for value in values):
        return False, "dati spread/volatilità non finiti"
    if any(value < 0 for value in (
        spread_pips, avg_range_pips, fast_avg_range_pips, current_range_pips,
    )):
        return False, "dati spread/volatilità negativi"

    spread_available = spread_pips > 0
    if spread_available:
        max_spread = config.get_max_spread_pips(symbol)
        if spread_pips > max_spread:
            return False, (
                f"spread {spread_pips:.1f} pip > massimo {max_spread:.0f} pip"
            )

    risk_pips = abs(entry - sl) / utils.pip_size(symbol)
    if not np.isfinite(risk_pips) or risk_pips <= 0:
        return False, "rischio non calcolabile"
    if require_slow_range and avg_range_pips <= 0:
        return False, "volatilità lenta non disponibile"

    spread_to_sl = spread_pips / risk_pips if spread_available else 0.0
    if spread_available and spread_to_sl > config.MAX_SPREAD_TO_SL_RATIO:
        return False, (
            f"spread {spread_pips:.1f} pip = {spread_to_sl:.0%} dello SL "
            f"({config.MAX_SPREAD_TO_SL_RATIO:.0%} massimo)"
        )

    if avg_range_pips > 0:
        sl_to_range = risk_pips / avg_range_pips
        if sl_to_range < config.MIN_SL_TO_AVG_RANGE_RATIO:
            return False, (
                f"SL {risk_pips:.0f} pip < {config.MIN_SL_TO_AVG_RANGE_RATIO:.1f}x "
                f"candela media/range lento ({avg_range_pips:.1f} pip)"
            )
        if sl_to_range > config.MAX_SL_TO_AVG_RANGE_RATIO:
            return False, (
                f"SL {risk_pips:.0f} pip > {config.MAX_SL_TO_AVG_RANGE_RATIO:.1f}x "
                f"range medio/candela media lento ({avg_range_pips:.1f} pip): "
                f"volatilità troppo bassa o movimento esteso"
            )

    if tp_price is not None:
        try:
            tp_price = float(tp_price)
        except (TypeError, ValueError):
            return False, "TP non numerico nel filtro volatilità"
        if not np.isfinite(tp_price):
            return False, "TP non finito nel filtro volatilità"
        if require_fast_range and fast_avg_range_pips <= 0:
            return False, "volatilità veloce non disponibile"
        reward_pips = abs(tp_price - entry) / utils.pip_size(symbol)
        if not np.isfinite(reward_pips) or reward_pips <= 0:
            return False, "TP non valido nel filtro volatilità"
        max_ratio = config.get_max_tp_to_avg_range_ratio(mode)
        reference_ranges = [value for value in (avg_range_pips, fast_avg_range_pips) if value > 0]
        if reference_ranges and any(
            reward_pips / value > max_ratio for value in reference_ranges
        ):
            slow_ratio = reward_pips / avg_range_pips if avg_range_pips > 0 else 0.0
            fast_ratio = reward_pips / fast_avg_range_pips if fast_avg_range_pips > 0 else 0.0
            return False, (
                f"TP a {reward_pips:.0f} pip: volatilità insufficiente "
                f"(rapporti slow={slow_ratio:.1f}x fast={fast_ratio:.1f}x, "
                f"massimo {max_ratio:.1f}x)"
            )

    if current_range_pips > 0 and fast_avg_range_pips > 0:
        current_ratio = current_range_pips / fast_avg_range_pips
        if current_ratio > config.MAX_CURRENT_RANGE_TO_AVG_RATIO:
            return False, (
                f"candela corrente anomala: {current_range_pips:.1f} pip = "
                f"{current_ratio:.1f}x la media veloce "
                f"(massimo {config.MAX_CURRENT_RANGE_TO_AVG_RATIO:.1f}x)"
            )

    return True, f"spread {spread_pips:.1f} pip e volatilità ok"


# ---------------------------------------------------------------------------
# 4d. BLOCCO CONFLITTO DXY (punto 5)
# ---------------------------------------------------------------------------

def detect_dxy_conflict(
    symbol: str,
    direction: str,
    dxy_trend: str | None,
) -> tuple[bool, str]:
    """Rileva un conflitto tra la direzione del segnale e il trend del DXY.

    Il DXY è inversamente correlato alle coppie ``XXXUSD`` principali e ai
    metalli quotati in USD; è direttamente correlato alle coppie ``USDXXX``.
    I cross senza USD vengono lasciati neutrali perché il DXY da solo non
    descrive la relazione tra le due valute del cross.

    Sono accettati anche i suffissi del broker (es. ``EURUSDm`` o
    ``XAUUSD.pro``), così il controllo non viene aggirato dalla nomenclatura
    del simbolo nel Market Watch.

    Returns:
        (is_conflict, reason). ``reason`` è vuota quando non c'è conflitto o
        il DXY non è disponibile.
    """
    if not dxy_trend:
        return False, ""

    side = str(direction).strip().lower()
    trend = str(dxy_trend).strip().lower()
    if side not in {"buy", "sell"} or trend not in {"bullish", "bearish"}:
        return False, ""

    # I broker spesso aggiungono suffissi alfabetici o separatori al simbolo:
    # confrontiamo il prefisso della coppia standard, non la stringa completa.
    symbol_upper = str(symbol).strip().upper()
    inverse_bases = (
        "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD",
        "XAUUSD", "XAGUSD", "GOLD", "SILVER",
    )
    direct_bases = (
        "USDJPY", "USDCAD", "USDCHF", "USDSEK", "USDNOK",
    )
    cross_bases = (
        # JPY crosses
        "GBPJPY", "EURJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
        # European/commodity crosses
        "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
        "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
        "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF",
        "CADCHF",
    )

    # Suffix comuni dei broker: ``EURUSDm``, ``EURUSDc``, ``EURUSD.pro``,
    # ``EURUSD-ECN``. Un suffisso arbitrario senza separatore non viene
    # accettato, per non scambiare un simbolo diverso come EURUSDJPY per
    # EURUSD.
    known_compact_suffixes = frozenset({"m", "c", "i", "p", "r", "a", "ecn", "pro"})

    def _matches_base(base: str) -> bool:
        if symbol_upper == base:
            return True
        if not symbol_upper.startswith(base):
            return False
        suffix = symbol_upper[len(base):]
        if suffix.lower() in known_compact_suffixes:
            return True
        return suffix[:1] in {".", "_", "-"} and len(suffix) > 1

    if any(_matches_base(base) for base in inverse_bases):
        # USD bullish -> gli strumenti inverse scendono: BUY è conflitto.
        conflict = (side == "buy" and trend == "bullish") or (
            side == "sell" and trend == "bearish"
        )
    elif any(_matches_base(base) for base in direct_bases):
        # USD bullish -> gli strumenti direct salgono: SELL è conflitto.
        conflict = (side == "buy" and trend == "bearish") or (
            side == "sell" and trend == "bullish"
        )
    elif any(_matches_base(base) for base in cross_bases):
        return False, "DXY non applicabile (cross)"
    else:
        # Simboli non mappati: fail-open, non inventiamo una correlazione.
        return False, ""

    if conflict:
        return True, f"{side.upper()} contro DXY {trend}"
    return False, ""


# ---------------------------------------------------------------------------
# 5. TP DA LIQUIDITA' H4 OPPOSTA
# ---------------------------------------------------------------------------

def find_opposite_liquidity_target(
    liq_zones: pd.DataFrame, entry: float, direction: str,
) -> tuple[float, float, Optional[float]]:
    """Trova TP basati su zone di liquidita' H4 opposte.

    Strategia SMC: TP = zona di liquidita' opposta (non moltiplicatore fisso).
    - BUY: TP1 = nearest BSL sopra entry, TP2 = BSL successiva, TP3 = terza
    - SELL: TP1 = nearest SSL sotto entry, TP2 = SSL successiva, TP3 = terza
    - Se non ci sono abbastanza zone, TP mancanti = 0.0 (TP2) o None (TP3)

    Returns: (tp1, tp2, tp3) — tp3 puo' essere None se non disponibile.
    """
    if liq_zones.empty:
        return 0.0, 0.0, None

    if direction == "buy":
        bsl = liq_zones[liq_zones["type"] == "BSL"].copy()
        if bsl.empty:
            return 0.0, 0.0, None
        bsl = bsl.sort_values("price_level")
        targets = [float(p) for p in bsl["price_level"] if float(p) > entry]
        tp1 = targets[0] if len(targets) >= 1 else 0.0
        tp2 = targets[1] if len(targets) >= 2 else 0.0
        tp3 = targets[2] if len(targets) >= 3 else None
        return tp1, tp2, tp3
    else:
        ssl = liq_zones[liq_zones["type"] == "SSL"].copy()
        if ssl.empty:
            return 0.0, 0.0, None
        ssl = ssl.sort_values("price_level", ascending=False)
        targets = [float(p) for p in ssl["price_level"] if float(p) < entry]
        tp1 = targets[0] if len(targets) >= 1 else 0.0
        tp2 = targets[1] if len(targets) >= 2 else 0.0
        tp3 = targets[2] if len(targets) >= 3 else None
        return tp1, tp2, tp3


# ---------------------------------------------------------------------------
# 6. SWEEP H4 — CONDIZIONE PRIMARIA
# ---------------------------------------------------------------------------

def generate_sweep_entry(
    sweep_check: dict,
    reversal: dict,
    trend: str,
    direction: str,
    min_sl_pips: int,
    pip: float,
    min_rr: float = 3.0,
    max_bars_ago: int = 5,
    entry_offset_pips: float = 10.0,
    mode: str | None = None,
) -> Optional[dict]:
    """Genera un segnale di entrata basato esclusivamente sullo sweep di liquidita'.

    Strategia SMC (avanzata): quando la liquidita' H4 viene spazzata (sweep
    istituzionale), il prezzo ha creato un vuoto di liquidita'. L'entrata
    puo' avvenire anche SENZA Order Block fresco, perche' lo sweep stesso
    e' il segnale che le istituzioni hanno preso posizione.

    Regola del corso SMC (Video 10, 11):
        'Se la liquidita' e' valida e lo sweep e' istituzionale,
         puoi entrare anche se non c'e' un OB fresco.'

    Args:
        sweep_check: dict da has_h4_liquidity_sweep()
        reversal: dict da classify_reversal()
        trend: trend HTF ('bullish'/'bearish')
        direction: direzione proposta ('buy'/'sell' — opposta allo sweep)
        min_sl_pips: SL minimo in pip per la modalita'
        pip: dimensione del pip (da utils.pip_size)
        min_rr: RR minimo richiesto
        max_bars_ago: sweep piu' vecchio di N barre viene scartato
        entry_offset_pips: offset dallo sweep per evitare lo spread (default 10 pip)

    Returns:
        dict con i campi del segnale, oppure None se lo sweep non e' valido.
    """
    if not sweep_check.get("swept"):
        return None

    # Non entrare su reversal retail
    if reversal.get("type") != "institutional":
        return None
    if mode == "swing" and not reversal.get("displacement", False):
        return None

    sweep_type = sweep_check["type"]
    sweep_price = sweep_check["price"]
    bars_ago = sweep_check.get("bars_ago", 999)

    # Sweep troppo vecchio: non e' piu' valido
    if bars_ago > max_bars_ago:
        return None

    # Verifica pro-trend
    is_pro = (direction == "buy" and trend == "bullish") or (direction == "sell" and trend == "bearish")

    # Entry: offset dallo sweep per evitare lo spread. Sullo swing usiamo
    # un offset minimo: l'entrata deve stare il più vicino possibile all'inizio
    # del movimento, non inseguirlo dopo una forte estensione.
    effective_offset_pips = (
        config.SWING_ENTRY_OFFSET_PIPS if mode == "swing" else entry_offset_pips
    )
    # BUY:  sweep_price + offset_pips  (entra sopra, lo sweep era sotto)
    # SELL: sweep_price - offset_pips  (entra sotto, lo sweep era sopra)
    offset_price = round(effective_offset_pips * pip, 5)
    if direction == "buy":
        entry = round(sweep_price + offset_price, 5)
    else:
        entry = round(sweep_price - offset_price, 5)

    # SL: oltre lo sweep (protezione da falso breakout)
    if direction == "buy":
        sl = round(entry - min_sl_pips * pip, 5)
    else:
        sl = round(entry + min_sl_pips * pip, 5)

    risk_price = abs(entry - sl)
    if risk_price <= 0:
        return None

    # Lo swing non ammette counter-trend a 1:2: il minimo è sempre 1:4.
    # Le altre modalità conservano la loro differenziazione.
    if mode == "swing":
        actual_min_rr = config.get_min_rr("swing")
    elif mode == "daytrading":
        actual_min_rr = config.get_min_rr("daytrading")
    else:
        actual_min_rr = min_rr if is_pro else max(2.0, min_rr * 0.66)

    # TP costruiti per garantire RR minimo
    # (rr >= actual_min_rr per costruzione)
    tp1 = round(entry + risk_price * actual_min_rr if direction == "buy" else entry - risk_price * actual_min_rr, 5)
    tp2 = round(entry + risk_price * (actual_min_rr + 2.0) if direction == "buy" else entry - risk_price * (actual_min_rr + 2.0), 5)
    rr = round(abs(tp1 - entry) / risk_price, 1) if risk_price > 0 else 0

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "setup_type": "pro_trend" if is_pro else "counter_trend",
        "pd_zone": "liquidity_sweep",
        "sweep_type": sweep_type,
        "sweep_price": sweep_price,
        "entry_offset_pips": effective_offset_pips,
        "bars_ago": bars_ago,
        "sweep_confidence": reversal.get("confidence", 50),
        "trend": trend,
        "probability": "high",
        "from_sweep": True,
    }


def has_h4_liquidity_sweep(
    df: pd.DataFrame, swings: pd.DataFrame,
    current_price: float, lookback_bars: int = 20,
) -> dict:
    """Verifica se il prezzo ha recentemente spazzato una liquidita' H4.

    Questa e' LA condizione primaria della strategia SMC:
    'Il movimento swing NON PUO' partire se c'e' liquidita' retail.
    Prima devono toglierla, poi parte.'

    Returns: {'swept': bool, 'type': 'BSL'|'SSL'|None,
              'price': float, 'bars_ago': int}
    """
    result = {"swept": False, "type": None, "price": 0.0, "bars_ago": 0}

    if df is None or len(df) < lookback_bars:
        return result

    # Trova zone di liquidita' H4
    liq_zones = find_liquidity_zones(df, swings)
    if liq_zones.empty:
        return result

    # Analizza le ultime lookback_bars barre per trovare sweep
    recent = df.iloc[-lookback_bars:]
    best_sweep = {
        "type": None, "price": 0.0, "bars_ago": 999, "sweep_idx": None,
    }

    for _, zone in liq_zones.iterrows():
        zone_type = str(zone["type"])
        zone_price = float(zone["price_level"])

        for i in range(len(recent) - 1, -1, -1):
            row = recent.iloc[i]
            bars_ago = len(recent) - 1 - i

            if zone_type == "BSL":
                # BSL sweep: high buca sopra e close torna sotto = sweep istituzionale
                if float(row["high"]) > zone_price and float(row["close"]) < zone_price:
                    if bars_ago < best_sweep["bars_ago"]:
                        best_sweep = {
                            "type": "BSL", "price": zone_price,
                            "bars_ago": bars_ago,
                            "sweep_idx": len(df) - lookback_bars + i,
                        }
                    break
            else:  # SSL
                # SSL sweep: low buca sotto e close torna sopra = sweep istituzionale
                if float(row["low"]) < zone_price and float(row["close"]) > zone_price:
                    if bars_ago < best_sweep["bars_ago"]:
                        best_sweep = {
                            "type": "SSL", "price": zone_price,
                            "bars_ago": bars_ago,
                            "sweep_idx": len(df) - lookback_bars + i,
                        }
                    break

    if best_sweep["type"] is not None:
        result.update({
            "swept": True,
            "type": best_sweep["type"],
            "price": best_sweep["price"],
            "bars_ago": best_sweep["bars_ago"],
            "sweep_idx": best_sweep.get("sweep_idx"),
        })

    return result
