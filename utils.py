"""
utils.py
========
Helper utility functions used across the SMC trading bot.

Contains small, stateless, easily-testable functions that do NOT depend on
any particular business-logic state. They centralize duplicated logic found in
run_master.py, position_monitor.py and other modules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

XAU_SYMBOLS: frozenset[str] = frozenset({"XAUUSD", "XAUEUR", "GOLD", "XAGUSD", "SILVER"})


def _symbol_matches_base(symbol: str, base: str) -> bool:
    """Matcha una base strumento con i suffissi comuni dei broker."""
    normalized = str(symbol).strip().upper()
    base = base.upper()
    if normalized == base:
        return True
    if not normalized.startswith(base):
        return False
    suffix = normalized[len(base):]
    if suffix.lower() in {"m", "c", "i", "p", "r", "a", "ecn", "pro"}:
        return True
    return suffix[:1] in {".", "_", "-"} and len(suffix) > 1


def is_xau(symbol: str) -> bool:
    """Return True if the symbol is a precious metal (gold/silver style)."""
    return any(_symbol_matches_base(symbol, base) for base in XAU_SYMBOLS)


def pip_size(symbol: str) -> float:
    """Return the value of 1 pip for the given symbol.

    Convenzione utente:
      - XAU / precious metals: 1 pip = 0.10
        (4000 → 4001 = 10 pips, 4000 → 4010 = 100 pips)
      - JPY pairs: 1 pip = 0.01
        (150.00 → 150.01 = 1 pip, 150.00 → 150.10 = 10 pips)
      - Standard forex: 1 pip = 0.0001

    NOTA: la convenzione XAU dell'utente e' 10x rispetto a 1 pip = 0.01
    usata da molti broker. Il fattore e' coerente con le tabelle SL in
    config.py e con le soglie in pip delle modalità attive.
    """
    sym = str(symbol).upper()
    if any(_symbol_matches_base(sym, base) for base in XAU_SYMBOLS):
        return 0.10
    if "JPY" in sym:
        return 0.01
    return 0.0001


# ---------------------------------------------------------------------------
# Money formatting
# ---------------------------------------------------------------------------


def validate_intraday_levels(
    symbol: str,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    market_price: float,
    mode: str,
    tp_levels: Optional[Sequence[float]] = None,
) -> tuple[bool, str]:
    """Validate the price geometry and scale of an autonomous trade.

    The validator intentionally rejects stale setups instead of converting
    them into long-lived pending orders.  Distances are measured in *pips*
    via :func:`pip_size`, never in broker points. TP has no maximum distance:
    only its direction, optional ordering, and the mode minimum R:R are
    checked.
    """
    import config  # local import avoids a config -> utils import cycle

    side = direction.lower()
    if side not in {"buy", "sell"}:
        return False, f"direzione non valida: {direction!r}"
    if min(entry, sl, tp, market_price) <= 0:
        return False, "livello prezzo non positivo"

    pip = pip_size(symbol)
    market_distance_pips = abs(entry - market_price) / pip
    max_pending_pips = config.get_max_pending_distance_pips(mode)
    # Tolleranza in pip per evitare che 12.0000000001 venga interpretato
    # come una violazione del limite esattamente pari a 12 pip.
    epsilon_pips = 1e-6
    if market_distance_pips > max_pending_pips + epsilon_pips:
        return False, (
            f"entry distante {market_distance_pips:.1f} pip dal mercato; "
            f"limite {max_pending_pips} pip per {mode}"
        )

    if side == "buy":
        if sl >= entry:
            return False, f"BUY con SL {sl:.5f} >= entry {entry:.5f}"
        if tp <= entry:
            return False, f"BUY con TP {tp:.5f} <= entry {entry:.5f}"
    else:
        if sl <= entry:
            return False, f"SELL con SL {sl:.5f} <= entry {entry:.5f}"
        if tp >= entry:
            return False, f"SELL con TP {tp:.5f} >= entry {entry:.5f}"

    risk_pips = abs(entry - sl) / pip
    min_sl_pips = config.get_sl_min_pips(symbol, mode)
    max_sl_pips = config.get_sl_max_pips(symbol, mode)
    if risk_pips < min_sl_pips - epsilon_pips:
        return False, (
            f"SL distante {risk_pips:.1f} pip; "
            f"minimo {min_sl_pips} pip per {mode}"
        )
    if risk_pips > max_sl_pips + epsilon_pips:
        return False, (
            f"SL distante {risk_pips:.1f} pip; "
            f"massimo {max_sl_pips} pip per {mode}"
        )

    # Nessun tetto al TP: il target può essere lontano quanto richiede
    # l'analisi della liquidità/struttura del mercato. Per lo swing il target
    # finale deve però essere almeno 4R: 1:2 e 1:3 sono vietati.
    targets = [float(tp)] if tp_levels is None else [float(level) for level in tp_levels]
    if not targets:
        return False, "nessun TP valido"

    mode_lower = mode.lower()
    min_rr = (
        4.0 if mode_lower == "swing"
        else 2.0 if mode_lower == "daytrading"
        else None
    )
    previous = None
    for index, target in enumerate(targets, start=1):
        if side == "buy" and target <= entry:
            return False, f"BUY con TP{index} {target:.5f} <= entry {entry:.5f}"
        if side == "sell" and target >= entry:
            return False, f"SELL con TP{index} {target:.5f} >= entry {entry:.5f}"
        if previous is not None:
            if side == "buy" and target < previous - epsilon_pips * pip:
                return False, f"TP{index} non è più lontano di TP{index - 1}"
            if side == "sell" and target > previous + epsilon_pips * pip:
                return False, f"TP{index} non è più lontano di TP{index - 1}"
        previous = target

    # Nello swing ogni target operativo deve avere almeno 4R: non basta
    # nascondere un TP1 a 1:2 dietro a un TP2 più lontano. Nel daytrading
    # il vincolo resta sul target finale, come previsto dal materiale base.
    if min_rr is not None:
        rr_values = [abs(target - entry) / pip / risk_pips for target in targets]
        if mode_lower == "swing":
            for index, rr in enumerate(rr_values, start=1):
                if rr < min_rr - epsilon_pips:
                    return False, (
                        f"R:R TP{index} {rr:.2f} inferiore al minimo "
                        f"1:{min_rr:.0f} per swing"
                    )
        else:
            rr = rr_values[-1] if rr_values else 0.0
            if rr < min_rr - 1e-9:
                return False, (
                    f"R:R target finale {rr:.2f} inferiore al minimo "
                    f"1:{min_rr:.0f} per {mode_lower}"
                )

    return True, "ok"


def format_money(amount: float, currency: str = "EUR", *, always_sign: bool = True) -> str:
    """Format a monetary amount with currency and an optional sign prefix.

    Args:
        amount: the monetary value to format.
        currency: ISO currency code (default EUR).
        always_sign: if True, always prepend '+' or '-' before the value.
    """
    if always_sign:
        sign = "-" if amount < 0 else "+"
        return f"{sign}{abs(amount):,.2f} {currency}"
    return f"{amount:,.2f} {currency}"


# ---------------------------------------------------------------------------
# Time helpers for MT5
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def mt5_history_window(
    days_back: int = 1,
    hours_back: Optional[int] = None,
    minutes_ahead: int = 1,
) -> tuple[datetime, datetime]:
    """Build a timezone-aware (UTC) datetime window suitable for mt5.history_deals_get.

    Args:
        days_back: how many days to go back from now for the start date.
        hours_back: alternative to days_back; overrides days_back if provided.
        minutes_ahead: how many minutes ahead of now to set the end date
            (MT5's 'to' argument is exclusive-ish, so a small buffer helps).

    Returns:
        (from_date, to_date) tuple of timezone-aware UTC datetimes.
    """
    now = utc_now()
    if hours_back is not None:
        from_dt = now - timedelta(hours=hours_back)
    else:
        from_dt = now - timedelta(days=days_back)
    to_dt = now + timedelta(minutes=minutes_ahead)
    return from_dt, to_dt


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def is_error_log_line(line: str) -> bool:
    """Return True if a log line is an ERROR/CRITICAL record.

    Log format: ``"2026-07-31 03:30:44,542 | ERROR | logger | message"``.
    Analizza il segmento "| LIVELLO |" (evita falsi positivi da messaggi che
    contengono la parola "ERROR" nel testo) e riconosce anche i messaggi
    generati dalle API stesse che iniziano con "[ERROR]".
    """
    if line.startswith("[ERROR]"):
        return True
    parts = line.split(" | ")
    return len(parts) > 1 and parts[1].strip() in ("ERROR", "CRITICAL")


