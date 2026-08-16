"""
position_monitor.py
===================
Monitoraggio posizioni aperte con:
  - Break-Even automatico (dinamico: R:R 1:1 = profitto >= distanza SL)
  - Chiusure parziali strategiche (30% TP1, 30% TP2, 40% TP3)

Strategia SMC: 1 sola posizione col 100% del lotto.
Il TP sull'ordine e' il target piu' lontano (TP3 > TP2 > TP1).
Le chiusure parziali sono gestite da questo modulo.

Regola BE del corso (Video 32): "uso sempre la regola del rischio
rendimento 1 a 1" — BE scatta quando il profitto in pip e' >= SL in pip.
Il Trailing Stop e' stato RIMOSSO (non previsto dalla strategia SMC).

Usa la connessione MT5 gia' attiva (gestita da run_master.py).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from mt5_adapter import mt5

import utils

logger = logging.getLogger(__name__)

import config

# Costanti
MAGIC_MAIN = 1000                  # magic legacy (webhook)
# Accetta i magic delle due modalità attive (daytrading=1002, swing=1003)
# + il magic legacy 1000 per retro-compatibilita'
ALLOWED_MAGICS: frozenset[int] = config.ALL_MODE_MAGICS | {MAGIC_MAIN}


# ==========================================================================
# PARTIAL CLOSE TRACKER
# ==========================================================================

class PartialCloseState:
    """Stato delle chiusure parziali per un ticket."""

    __slots__ = (
        "ticket", "tp1", "tp2", "tp3",
        "tp1_hit", "tp2_hit",
        "initial_volume", "direction",
    )

    def __init__(
        self,
        ticket: int,
        tp1: float,
        tp2: float,
        tp3: Optional[float],
        initial_volume: float,
        direction: str,
    ) -> None:
        self.ticket = ticket
        self.tp1 = tp1
        self.tp2 = tp2
        self.tp3 = tp3
        self.tp1_hit = False
        self.tp2_hit = False
        self.initial_volume = initial_volume
        self.direction = direction


class PartialCloseTracker:
    """Traccia le chiusure parziali per ogni posizione aperta."""

    def __init__(self) -> None:
        self._states: dict[int, PartialCloseState] = {}

    def register(
        self,
        ticket: int,
        tp1: float,
        tp2: float,
        tp3: Optional[float],
        initial_volume: float,
        direction: str,
    ) -> None:
        self._states[ticket] = PartialCloseState(
            ticket=ticket, tp1=tp1, tp2=tp2, tp3=tp3,
            initial_volume=initial_volume, direction=direction,
        )
        tp3_str = f"TP3={tp3:.2f}" if tp3 else "NO TP3"
        logger.info(
            "[PARTIAL] Registrato ticket=%s vol=%.2f TP1=%.2f TP2=%.2f %s dir=%s",
            ticket, initial_volume, tp1, tp2, tp3_str, direction,
        )

    def get_state(self, ticket: int) -> Optional[PartialCloseState]:
        return self._states.get(ticket)

    def update_ticket(self, old_ticket: int, new_ticket: int) -> None:
        if old_ticket in self._states and old_ticket != new_ticket:
            state = self._states.pop(old_ticket)
            state.ticket = new_ticket
            self._states[new_ticket] = state

    def mark_tp1_hit(self, ticket: int) -> None:
        if ticket in self._states:
            self._states[ticket].tp1_hit = True

    def mark_tp2_hit(self, ticket: int) -> None:
        if ticket in self._states:
            self._states[ticket].tp2_hit = True

    def remove(self, ticket: int) -> None:
        self._states.pop(ticket, None)

    def has_tp2(self, ticket: int) -> bool:
        state = self._states.get(ticket)
        if state is None:
            return False
        return abs(state.tp2 - state.tp1) > 0.0001

    def has_tp3(self, ticket: int) -> bool:
        state = self._states.get(ticket)
        if state is None:
            return False
        return state.tp3 is not None and abs(state.tp3 - state.tp2) > 0.0001

    def cleanup_closed(self, active_tickets: set[int]) -> None:
        closed = set(self._states.keys()) - active_tickets
        for ticket in closed:
            self._states.pop(ticket, None)


_tracker = PartialCloseTracker()


def get_tracker() -> PartialCloseTracker:
    """Ritorna l'istanza globale del PartialCloseTracker."""
    return _tracker


# ==========================================================================
# GESTIONE CHIUSURE PARZIALI
# ==========================================================================

def manage_partial_closes(symbol: str) -> dict[str, list[dict]]:
    """Controlla e esegue chiusure parziali ai TP.

    Logica:
    - Se prezzo >= TP1 e non ancora chiuso -> chiudi 30% (o 100% se no TP2)
    - Se prezzo >= TP2 e non ancora chiuso -> chiudi 30% (o 100% se no TP3)
    - TP3: MT5 chiude automaticamente il remainder (TP ordine = TP3)

    Returns: {'tp1': [{ticket, volume_closed, close_price, pct, direction}, ...],
              'tp2': [...], 'tp3': [...]}
    """
    positions = mt5.positions_get(symbol=symbol)
    result: dict[str, list[dict]] = {"tp1": [], "tp2": [], "tp3": []}

    if not positions:
        _tracker.cleanup_closed(set())
        return result

    active_tickets: set[int] = set()
    for pos in positions:
        ticket = int(pos.ticket)
        active_tickets.add(ticket)

        if int(pos.magic) not in ALLOWED_MAGICS:
            continue

        state = _tracker.get_state(ticket)
        if state is None:
            continue

        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue

            direction = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
            info = mt5.symbol_info(symbol)
            if info is None:
                continue

            if direction == "BUY":
                current_price = float(tick.bid)
                if not state.tp1_hit and current_price >= state.tp1:
                    ratio = 1.0 if not _tracker.has_tp2(ticket) else 0.3
                    detail = _do_partial_close(symbol, ticket, state.initial_volume,
                                                ratio, current_price, direction, "TP1")
                    if detail:
                        _tracker.mark_tp1_hit(ticket)
                        result["tp1"].append(detail)
                        if not _tracker.has_tp2(ticket):
                            _tracker.remove(ticket)
                    continue

                if (state.tp1_hit and not state.tp2_hit
                        and _tracker.has_tp2(ticket)
                        and current_price >= state.tp2):
                    ratio = 1.0 if not _tracker.has_tp3(ticket) else 0.3
                    detail = _do_partial_close(symbol, ticket, state.initial_volume,
                                                ratio, current_price, direction, "TP2")
                    if detail:
                        _tracker.mark_tp2_hit(ticket)
                        result["tp2"].append(detail)
                    continue
            else:
                current_price = float(tick.ask)
                if not state.tp1_hit and current_price <= state.tp1:
                    ratio = 1.0 if not _tracker.has_tp2(ticket) else 0.3
                    detail = _do_partial_close(symbol, ticket, state.initial_volume,
                                                ratio, current_price, direction, "TP1")
                    if detail:
                        _tracker.mark_tp1_hit(ticket)
                        result["tp1"].append(detail)
                        if not _tracker.has_tp2(ticket):
                            _tracker.remove(ticket)
                    continue

                if (state.tp1_hit and not state.tp2_hit
                        and _tracker.has_tp2(ticket)
                        and current_price <= state.tp2):
                    ratio = 1.0 if not _tracker.has_tp3(ticket) else 0.3
                    detail = _do_partial_close(symbol, ticket, state.initial_volume,
                                                ratio, current_price, direction, "TP2")
                    if detail:
                        _tracker.mark_tp2_hit(ticket)
                        result["tp2"].append(detail)
                    continue

        except Exception as e:
            logger.warning(
                "[%s] Errore partial close ticket=%s: %s", symbol, ticket, e,
            )

    _tracker.cleanup_closed(active_tickets)
    return result


def _do_partial_close(
    symbol: str,
    ticket: int,
    initial_volume: float,
    close_ratio: float,
    current_price: float,
    direction: str,
    tp_label: str,
) -> Optional[dict]:
    """Esegue una chiusura parziale su MT5.

    Returns: dict con {ticket, volume_closed, close_price, pct, direction}
             oppure None se la chiusura fallisce.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return None

    vol_to_close = round(initial_volume * close_ratio, 2)
    min_vol = float(info.volume_min)
    if vol_to_close < min_vol:
        vol_to_close = min_vol

    # Arrotonda al volume_step del broker
    vol_step = float(info.volume_step)
    if vol_step > 0:
        steps = round(vol_to_close / vol_step)
        vol_to_close = round(steps * vol_step, 6)
        if vol_to_close < min_vol:
            vol_to_close = min_vol

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return None
    current_vol = float(positions[0].volume)
    if vol_to_close > current_vol:
        vol_to_close = current_vol
    if vol_to_close <= 0:
        return None

    close_type = mt5.ORDER_TYPE_SELL if direction == "BUY" else mt5.ORDER_TYPE_BUY
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "position": ticket,
        "volume": vol_to_close,
        "type": close_type,
        "price": current_price,
        "deviation": config.ORDER_DEVIATION,
        "magic": MAGIC_MAIN,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # --- Negozia il type_filling: prova IOC, poi FOK, poi RETURN ---
    # (il broker puo' supportare modalita' diverse per simbolo: evita 10030)
    invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
    result = None
    for filling in _candidate_fillings(symbol):
        req["type_filling"] = filling
        result = mt5.order_send(req)
        if result is None:
            logger.error("[%s] Partial close #%s %s: order_send None", symbol, ticket, tp_label)
            return None
        if result.retcode in (mt5.TRADE_RETCODE_DONE,
                              getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1)):
            break
        if result.retcode != invalid_fill:
            break  # errore diverso: niente retry

    if result is None:
        return None
    if result.retcode in (mt5.TRADE_RETCODE_DONE,
                          getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", -1)):
        new_ticket = int(getattr(result, "order", 0))
        if new_ticket and new_ticket != ticket:
            _tracker.update_ticket(ticket, new_ticket)
        pct = int(close_ratio * 100)
        vol_closed = vol_to_close
        logger.info(
            "[%s] Partial close %s: #%s chiuso %d%% (%.2f lotti) @ %.5f",
            symbol, tp_label, ticket, pct, vol_closed, current_price,
        )
        return {
            "ticket": ticket,
            "volume_closed": vol_closed,
            "close_price": current_price,
            "pct": pct,
            "direction": direction,
        }
    else:
        logger.error(
            "[%s] Partial close #%s %s FALLITO: retcode=%s",
            symbol, ticket, tp_label, result.retcode,
        )
        return None


def _candidate_fillings(symbol: str) -> list[int]:
    """Ordine di preferenza dei ``type_filling`` da provare sul simbolo.

    Parte dalle modalita' dichiarate supportate dal ``filling_mode`` del
    simbolo (IOC, poi FOK), quindi aggiunge le restanti come fallback: cosi'
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


# ==========================================================================
# GESTIONE POSIZIONI (SOLO BE — Trailing Stop RIMOSSO)
# ==========================================================================


def manage_positions(
    symbol: str,
    be_pips: float = 0.0,
) -> dict[str, list[int]]:
    """Gestisce Break-Even su tutte le posizioni del simbolo.

    Strategia SMC (Video 32): BE quando profitto >= SL (R:R 1:1).
    Il parametro be_pips DEVE essere la distanza SL del trade.
    Se be_pips=0, viene calcolato dalla posizione stessa (SL distance).

    Trailing Stop RIMOSSO — non previsto dalla strategia SMC.

    Ritorna: {'be': [ticket...], 'trail': [], 'partial': []}
    """
    positions = mt5.positions_get(symbol=symbol)
    result = {"be": [], "trail": [], "partial": []}

    if not positions:
        return result

    pip = utils.pip_size(symbol)

    for pos in positions:
        try:
            ticket = int(pos.ticket)
            # Solo posizioni delle modalita' SMC (1000-1003)
            if int(pos.magic) not in ALLOWED_MAGICS:
                continue

            entry = float(pos.price_open)
            current_sl = float(pos.sl)
            current_tp = float(pos.tp)

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue

            # Calcolo pips in profitto
            if pos.type == mt5.POSITION_TYPE_BUY:
                current_price = float(tick.bid)
                pips_profit = (current_price - entry) / pip
            else:
                current_price = float(tick.ask)
                pips_profit = (entry - current_price) / pip

            # Calcola la soglia BE: se be_pips non fornito, usa la distanza SL
            # della posizione (R:R 1:1 = profitto >= SL_distance)
            if be_pips > 0:
                threshold = be_pips
            else:
                sl_distance = abs(entry - current_sl) / pip if current_sl > 0 else 0
                threshold = sl_distance if sl_distance > 0 else 20.0

            # Break-Even: sposta SL a entry quando profitto >= distanza SL
            if pips_profit >= threshold and abs(current_sl - entry) > pip * 0.5:
                req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "sl": entry, "tp": current_tp}
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    result["be"].append(ticket)
                    logger.info("[%s] BE ticket=%s @ %.5f (profit=%.1f pip, threshold=%.0f pip)",
                                symbol, ticket, entry, pips_profit, threshold)

        except Exception as e:
            logger.warning("Errore posizione ticket=%s: %s", getattr(pos, 'ticket', '?'), e)

    return result


def reset_tracker() -> None:
    """Resetta il PartialCloseTracker (utile per test)."""
    global _tracker
    _tracker = PartialCloseTracker()


def clear_partial_closures() -> None:
    """Alias di reset_tracker per retro-compatibilita' con vecchi script/test."""
    reset_tracker()


# ======================================================================
# Loop autonomo (opzionale: per testing)
# ======================================================================

def main() -> None:
    """Loop standalone per test."""
    import config

    if not mt5.initialize():
        logger.critical("MT5 non disponibile.")
        return

    logger.info("Monitor posizioni avviato su: %s", config.SYMBOLS)
    try:
        while True:
            for symbol in config.SYMBOLS:
                # Partial closes
                pc_res = manage_partial_closes(symbol)
                if sum(len(v) for v in pc_res.values()) > 0:
                    logger.info("[%s] Partial closes: %s", symbol, pc_res)
                # BE + Trailing
                res = manage_positions(symbol)
                total = sum(len(v) for v in res.values())
                if total > 0:
                    logger.info("[%s] Modifiche: %s", symbol, res)
            time.sleep(config.BREAK_EVEN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Interrotto.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()