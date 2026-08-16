"""
mt5_engine.py
=============
Esecuzione ordini su MetaTrader 5 a partire dagli ``OrderPlan`` prodotti da
``TradeValidator.build_order()``. Il modulo:

    1. Si aggancia UNA SOLA VOLTA al terminale MT5 gia' aperto (``initialize()``,
       credenziali lette da ``config.py`` / ``.env``, mai in chiaro nel codice).
       La sessione NON viene ne' riaperta ne' chiusa a ogni chiamata: e' il
       loop chiamante (``run_master``) a gestirne il ciclo di vita.
    2. Costruisce la ``request`` per ``mt5.order_send()`` gestendo ``action``,
       ``type``, ``type_filling`` e ``deviation`` (mercato o pending limit).
    3. Invia 1 solo ordine (magic 1000 = MAGIC_MAIN) col 100% del lotto.
       Le chiusure parziali sono gestite dal PartialCloseTracker.

Coerenza con ``trade_manager``: stesse eccezioni base (``TradeManagerError``),
stesso stile tipizzato, import pigro di ``MetaTrader5`` per restare testabile su
macchine senza terminale (il modulo ``mt5`` reale e' iniettabile per i test).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import config
from trade_manager import (
    OrderPlan,
    Side,
    TradeManagerError,
)
import utils

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Eccezioni custom (coerenti con la gerarchia di trade_manager)
# ---------------------------------------------------------------------------

class MT5ConnectionError(TradeManagerError):
    """Sollevata quando l'inizializzazione o l'aggancio al terminale fallisce."""


class OrderExecutionError(TradeManagerError):
    """Sollevata su errori infrastrutturali dell'invio ordine.

    Casi tipici: simbolo non selezionabile nel Market Watch, tick di prezzo
    non disponibile, ``order_send`` che ritorna ``None``. Il rifiuto del broker
    con retcode != DONE NON solleva: viene incapsulato in un ``OrderResult``
    con ``success=False`` cosi' l'invio dello split puo' riportare esiti parziali.
    """


# ---------------------------------------------------------------------------
# Tipi di dominio
# ---------------------------------------------------------------------------

class ExecutionMode(str, Enum):
    """Modalita' di esecuzione dell'ordine."""
    MARKET = "market"
    PENDING_LIMIT = "pending_limit"


@dataclass(frozen=True)
class OrderResult:
    """Esito normalizzato di un singolo ``order_send()``."""
    plan_key: str          # "tp1" | "tp2"
    magic: int
    success: bool
    retcode: int
    ticket: Optional[int]
    volume: float
    price: float
    comment: str

    @property
    def ok(self) -> bool:
        return self.success


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class MT5Engine:
    """Wrapper stateful sulla sessione MT5.

    Parameters
    ----------
    mt5_module:
        Modulo ``MetaTrader5`` (o mock) iniettabile. Se ``None`` viene importato
        pigramente al primo utilizzo, mantenendo il modulo importabile senza
        terminale installato.
    deviation:
        Slippage massimo (in points) per gli ordini a mercato.
    execution_mode:
        ``ExecutionMode.MARKET`` (default) o ``ExecutionMode.PENDING_LIMIT``.
    """

    def __init__(
        self,
        *,
        mt5_module: Optional[object] = None,
        deviation: int = config.ORDER_DEVIATION,
        execution_mode: ExecutionMode = ExecutionMode.MARKET,
    ) -> None:
        self._mt5 = mt5_module
        self._deviation = deviation
        self._execution_mode = execution_mode
        self._initialized = False

    # -- Ciclo di vita sessione --------------------------------------------

    def _lib(self) -> object:
        if self._mt5 is None:
            from mt5_adapter import mt5  # import locale: resta testabile senza MT5
            self._mt5 = mt5
        return self._mt5

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def initialize(self) -> bool:
        """Aggancia il terminale MT5 gia' aperto. Idempotente.

        Se ``config`` espone credenziali complete le passa a
        ``mt5.initialize(login=, password=, server=)``; altrimenti tenta
        l'aggancio puro al terminale gia' loggato manualmente.

        Raises
        ------
        MT5ConnectionError
            Se ``initialize()`` fallisce.
        """
        if self._initialized:
            return True

        mt5 = self._lib()
        kwargs: dict[str, object] = {}
        if config.MT5_TERMINAL_PATH:
            kwargs["path"] = config.MT5_TERMINAL_PATH
        # Login via codice SOLO se esplicitamente richiesto (MT5_ATTACH_ONLY=false)
        # e con credenziali complete; altrimenti aggancio puro al terminale loggato.
        if not config.MT5_ATTACH_ONLY and config.MT5_LOGIN and config.MT5_PASSWORD and config.MT5_SERVER:
            kwargs.update(
                login=config.MT5_LOGIN,
                password=config.MT5_PASSWORD,
                server=config.MT5_SERVER,
            )

        ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
        if not ok:
            code, message = mt5.last_error()
            hint = ""
            if code == -6:  # Authorization failed
                hint = (
                    " | Suggerimento: apri MetaTrader 5 e fai login a mano sul conto, "
                    "poi riavvia (MT5_ATTACH_ONLY=true si aggancia senza credenziali). "
                    "Se vuoi il login da codice imposta MT5_ATTACH_ONLY=false e "
                    "verifica login/password/server nel .env."
                )
            raise MT5ConnectionError(
                f"initialize() MT5 fallita: ({code}, '{message}'){hint}"
            )

        self._initialized = True
        account = mt5.account_info()
        if account is not None:
            logger.info(
                "MT5 connesso | server=%s | equity=%s | margine_libero=%s",
                getattr(account, "server", "?"),
                getattr(account, "equity", "?"),
                getattr(account, "margin_free", "?"),
            )
        else:
            logger.warning(
                "MT5 inizializzato ma account_info() e' None: verifica il login "
                "sull'interfaccia del terminale."
            )
        return True

    def shutdown(self) -> None:
        """Chiude la sessione MT5 (chiamata una sola volta dal loop master)."""
        if not self._initialized:
            return
        try:
            self._lib().shutdown()
        finally:
            self._initialized = False
            logger.info("Sessione MT5 chiusa.")

    def health_check(self, symbols: tuple[str, ...] = ()) -> bool:
        """Verifica che terminale, bridge, conto e mercato siano raggiungibili.

        Deve essere chiamato dal main thread, come tutte le API MT5 del bot.
        ``None`` da account/posizioni/ordini/tick è trattato come connessione
        non pronta; una lista vuota è invece uno stato valido (nessuna posizione
        o pending aperto).
        """
        if not self._initialized:
            return False
        mt5 = self._lib()
        try:
            account = mt5.account_info()
            if account is None:
                return False
            if not math.isfinite(float(account.balance)):
                return False

            # Queste chiamate distinguono una connessione viva da un semplice
            # flag locale rimasto True dopo la caduta del terminale/bridge.
            if mt5.positions_get() is None or mt5.orders_get() is None:
                return False

            for symbol in symbols:
                if mt5.symbol_info(symbol) is None:
                    return False
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    return False
                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
                if not (math.isfinite(bid) and math.isfinite(ask)
                        and bid > 0 and ask > 0 and ask >= bid):
                    return False
            return True
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        except Exception:
            # Un bridge remoto può sollevare errori di trasporto invece di
            # restituire None: anche quello è un health check fallito.
            return False

    def account_balance(self) -> Optional[float]:
        """Balance reale del conto (per il sizing). None se non disponibile."""
        if not self._initialized:
            return None
        info = self._lib().account_info()
        return float(info.balance) if info is not None else None

    def account_equity(self) -> Optional[float]:
        """Equity reale del conto. None se non disponibile."""
        if not self._initialized:
            return None
        info = self._lib().account_info()
        return float(info.equity) if info is not None else None

    def ensure_symbol(self, symbol: str) -> None:
        """Garantisce che il simbolo sia visibile nel Market Watch."""
        mt5 = self._lib()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise OrderExecutionError(f"Simbolo '{symbol}' non trovato sul broker.")
        if not getattr(info, "visible", True):
            if not mt5.symbol_select(symbol, True):
                raise OrderExecutionError(
                    f"Impossibile selezionare '{symbol}' nel Market Watch."
                )

    # -- Costruzione request ------------------------------------------------

    def _candidate_fillings(self, symbol: str) -> list[int]:
        """Ordine di preferenza dei ``type_filling`` da provare sul simbolo.

        Parte dalle modalita' dichiarate supportate dal ``filling_mode`` del
        simbolo (IOC, poi FOK), quindi aggiunge le restanti come fallback: cosi'
        ``place_order`` puo' rilanciare finche' il broker non ne accetta una,
        evitando il retcode 10030 (Unsupported filling mode).
        """
        mt5 = self._lib()
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

    def _resolve_filling(self, symbol: str) -> int:
        """Prima modalita' di filling candidata (usata come default della request)."""
        return self._candidate_fillings(symbol)[0]

    def _check_spread(self, symbol: str, plan: dict, tick: object, market_price: float) -> float:
        """Rifiuta gli ordini con spread eccessivo (punto 6 del corso).

        Due barriere, condivise con il percorso SMC autonomo:
          1. Spread corrente > massimo per simbolo (config.MAX_SPREAD_PIPS).
          2. Spread assorbe più del massimo consentito della distanza SL.

        Il tick usato è quello live del momento dell'ordine: per i market
        coincide con il fill, per i pending rappresenta le condizioni alla
        creazione dell'ordine.
        """
        try:
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            if ask <= 0 or bid <= 0:
                raise OrderExecutionError(
                    f"Tick bid/ask non valido per '{symbol}': bid={bid}, ask={ask}."
                )
            if ask < bid:
                raise OrderExecutionError(
                    f"Spread negativo/non valido per '{symbol}': bid={bid}, ask={ask}."
                )
            pip = utils.pip_size(symbol)
            spread_pips = (ask - bid) / pip
            max_spread = config.get_max_spread_pips(symbol)
            if spread_pips > max_spread:
                raise OrderExecutionError(
                    f"Spread {spread_pips:.1f} pip su '{symbol}' supera il massimo "
                    f"{max_spread:.0f} pip: ordine rifiutato."
                )
            try:
                entry = float(plan.get("entry", market_price) or market_price)
                sl = float(plan.get("sl", 0.0) or 0.0)
            except (TypeError, ValueError):
                raise OrderExecutionError(
                    f"Entry/SL non numerici per '{symbol}': "
                    f"entry={plan.get('entry')!r}, sl={plan.get('sl')!r}."
                ) from None
            if not all(math.isfinite(value) for value in (entry, sl)):
                raise OrderExecutionError(
                    f"Entry/SL non finiti per '{symbol}': entry={entry}, sl={sl}."
                )
            risk_pips = abs(entry - sl) / pip
            max_ratio = float(config.MAX_SPREAD_TO_SL_RATIO)
            if risk_pips > 0 and (spread_pips / risk_pips) > max_ratio:
                raise OrderExecutionError(
                    f"Spread {spread_pips:.1f} pip = {spread_pips / risk_pips:.0%} dello SL "
                    f"({max_ratio:.0%} massimo) su '{symbol}': ordine rifiutato."
                )
            return spread_pips
        except (TypeError, ValueError):
            # Difesa finale per un mock/tick non standard. In condizioni live
            # i dati invalidi vengono rifiutati esplicitamente sopra.
            raise OrderExecutionError(
                f"Dati spread non validi per '{symbol}': tick={tick!r}."
            ) from None

    def _check_volatility(self, symbol: str, plan: dict, spread_pips: float) -> None:
        """Applica il filtro volatilità agli ordini webhook quando MT5 lo espone.

        I mock/minimal bridge privi di ``copy_rates_from_pos`` restano
        compatibili e vengono lasciati passare: il filtro autonomo continua a
        richiedere esplicitamente entrambi i timeframe. Con MT5 completo,
        invece, si usano solo barre chiuse (shift=1), con H1/M15 nello swing e
        M15/M5 nel daytrading.
        """
        mt5 = self._lib()
        copy_rates = getattr(mt5, "copy_rates_from_pos", None)
        slow_tf = getattr(mt5, "TIMEFRAME_H1", None)
        fast_tf = getattr(mt5, "TIMEFRAME_M15", None)
        mode = str(plan.get("mode", "daytrading")).lower()
        if mode == "daytrading":
            slow_tf = getattr(mt5, "TIMEFRAME_M15", None)
            fast_tf = getattr(mt5, "TIMEFRAME_M5", None)
        if not callable(copy_rates) or slow_tf is None or fast_tf is None:
            return

        bars = max(3, int(config.VOLATILITY_BARS or 20))

        def _ranges(timeframe: object) -> list[tuple[float, float]]:
            try:
                raw = copy_rates(symbol, timeframe, 1, bars + 1)
            except Exception as exc:
                raise OrderExecutionError(
                    f"Storico volatilità non disponibile per '{symbol}': {exc}"
                ) from exc
            if raw is None:
                raise OrderExecutionError(
                    f"Storico volatilità non disponibile per '{symbol}'."
                )
            values: list[tuple[float, float]] = []
            for row in raw:
                try:
                    try:
                        high_raw = row["high"]
                        low_raw = row["low"]
                    except (KeyError, IndexError, TypeError):
                        high_raw = row.high
                        low_raw = row.low
                    high = float(high_raw)
                    low = float(low_raw)
                    value = high - low
                    timestamp = 0.0
                    try:
                        timestamp = float(row["time"])
                    except (KeyError, IndexError, TypeError, AttributeError):
                        try:
                            timestamp = float(row.time)
                        except (AttributeError, TypeError, ValueError):
                            pass
                except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    values.append((timestamp, value))
            if not values:
                raise OrderExecutionError(
                    f"Barre volatilità non valide per '{symbol}'."
                )
            if any(timestamp for timestamp, _ in values):
                values.sort(key=lambda item: item[0])
            return values

        slow_data = _ranges(slow_tf)
        fast_data = _ranges(fast_tf)
        if len(slow_data) < 2 or len(fast_data) < 3:
            raise OrderExecutionError(
                f"Storico volatilità insufficiente per '{symbol}'."
            )
        slow_ranges = [value for _, value in slow_data]
        fast_ranges = [value for _, value in fast_data]

        # Le barre sono ordinate cronologicamente quando il bridge fornisce
        # ``time``; l'ultima chiusa viene controllata separatamente e non
        # attenua la baseline usata per il controllo spike.
        slow_window = slow_ranges[-bars:]
        slow_avg_pips = (sum(slow_window) / len(slow_window)) / utils.pip_size(symbol)
        fast_baseline = fast_ranges[:-1][-bars:]
        fast_avg_pips = (sum(fast_baseline) / len(fast_baseline)) / utils.pip_size(symbol)
        current_fast_pips = fast_ranges[-1] / utils.pip_size(symbol)

        from smc_signals import validate_volatility_filter
        ok, reason = validate_volatility_filter(
            symbol,
            str(plan["side"]),
            float(plan["entry"]),
            float(plan["sl"]),
            spread_pips,
            slow_avg_pips,
            mode,
            tp_price=float(plan["tp"]),
            fast_avg_range_pips=fast_avg_pips,
            current_range_pips=current_fast_pips,
            require_fast_range=True,
            require_slow_range=True,
        )
        if not ok:
            raise OrderExecutionError(
                f"Spread/volatilità non validi per '{symbol}': {reason}"
            )


    def _order_type(self, side: Side) -> int:
        mt5 = self._lib()
        if self._execution_mode is ExecutionMode.PENDING_LIMIT:
            return (
                mt5.ORDER_TYPE_BUY_LIMIT
                if side is Side.BUY
                else mt5.ORDER_TYPE_SELL_LIMIT
            )
        return mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL

    def build_request(self, plan: OrderPlan) -> dict:
        """Traduce un ``OrderPlan`` nella ``request`` per ``mt5.order_send()``."""
        mt5 = self._lib()
        symbol = plan["symbol"]
        side = Side(plan["side"])

        if self._execution_mode is ExecutionMode.PENDING_LIMIT:
            action = mt5.TRADE_ACTION_PENDING
            price = float(plan["entry"])
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise OrderExecutionError(
                    f"Tick di prezzo non disponibile per '{symbol}'."
                )
            market_price = float(tick.ask if side is Side.BUY else tick.bid)
            spread_pips = self._check_spread(symbol, plan, tick, market_price)
            self._check_volatility(symbol, plan, spread_pips)
            # Un LIMIT deve stare dalla parte di rientro: BUY sotto ask,
            # SELL sopra bid. Altrimenti il webhook invierebbe uno STOP
            # mascherato da LIMIT.
            if side is Side.BUY and price >= market_price:
                raise OrderExecutionError(
                    f"BUY LIMIT entry {price:.5f} non sotto il mercato {market_price:.5f}."
                )
            if side is Side.SELL and price <= market_price:
                raise OrderExecutionError(
                    f"SELL LIMIT entry {price:.5f} non sopra il mercato {market_price:.5f}."
                )
            pip = utils.pip_size(symbol)
            distance_pips = abs(price - market_price) / pip
            # Il piano porta la modalità effettiva: lo swing può attendere
            # più lontano e più a lungo; il daytrading resta strettamente
            # intraday. Non ricadere sempre sul daytrading.
            order_mode = plan.get("mode", "daytrading")
            max_pending_pips = config.get_max_pending_distance_pips(order_mode)
            if distance_pips > max_pending_pips + 1e-6:
                raise OrderExecutionError(
                    f"Pending entry distante {distance_pips:.1f} pip dal mercato "
                    f"(massimo {max_pending_pips} pip)."
                )
            levels_ok, levels_reason = utils.validate_intraday_levels(
                symbol, plan["side"], price, float(plan["sl"]),
                float(plan["tp"]), market_price, order_mode,
                tp_levels=(float(plan["tp"]),),
            )
            if not levels_ok:
                raise OrderExecutionError(
                    f"Livelli pending non intraday per '{symbol}': {levels_reason}"
                )
        else:
            action = mt5.TRADE_ACTION_DEAL
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise OrderExecutionError(
                    f"Tick di prezzo non disponibile per '{symbol}'."
                )
            price = float(tick.ask if side is Side.BUY else tick.bid)
            spread_pips = self._check_spread(symbol, plan, tick, price)
            self._check_volatility(symbol, plan, spread_pips)
            # Anche gli ordini market devono passare dalla stessa barriera
            # geometrica/rischio dei pending: il webhook non può bypassarla.
            order_mode = plan.get("mode", "daytrading")
            levels_ok, levels_reason = utils.validate_intraday_levels(
                symbol, plan["side"], float(plan["entry"]), float(plan["sl"]),
                float(plan["tp"]), price, order_mode,
                tp_levels=(float(plan["tp"]),),
            )
            if not levels_ok:
                raise OrderExecutionError(
                    f"Livelli market non validi per '{symbol}': {levels_reason}"
                )

        return {
            "action": action,
            "symbol": symbol,
            "volume": float(plan["volume"]),
            "type": self._order_type(side),
            "price": price,
            "sl": float(plan["sl"]),
            "tp": float(plan["tp"]),
            "deviation": self._deviation,
            "magic": int(plan["magic"]),
            "comment": plan["comment"],
            # Un pending swing può attendere il ritorno al POI per più
            # sessioni; gli altri pending restano validi solo per la giornata.
            "type_time": (
                getattr(mt5, "ORDER_TIME_GTC", getattr(mt5, "ORDER_TIME_DAY", 0))
                if plan.get("mode") == "swing"
                else getattr(mt5, "ORDER_TIME_DAY", mt5.ORDER_TIME_GTC)
            ),
            "type_filling": self._resolve_filling(symbol),
        }

    # -- Invio ordini -------------------------------------------------------

    def place_order(self, plan: OrderPlan, *, plan_key: str = "") -> OrderResult:
        """Invia un singolo ordine a partire da un ``OrderPlan``.

        Ritorna sempre un ``OrderResult``: un rifiuto del broker (retcode !=
        DONE) e' incapsulato con ``success=False`` invece di sollevare, cosi'
        l'invio dello split puo' riportare esiti parziali.

        Raises
        ------
        OrderExecutionError
            Solo su errori infrastrutturali (simbolo assente, tick mancante,
            ``order_send`` che ritorna ``None``).
        """
        if not self._initialized:
            raise MT5ConnectionError(
                "MT5Engine non inizializzato: chiamare initialize() prima."
            )

        mt5 = self._lib()
        self.ensure_symbol(plan["symbol"])
        request = self.build_request(plan)

        # Negozia il type_filling: rilancia con la modalita' successiva se il
        # broker risponde 10030 (Unsupported filling mode). Ogni rifiuto 10030
        # NON piazza alcun ordine, quindi non c'e' rischio di doppia esecuzione.
        invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        result = None
        for filling in self._candidate_fillings(plan["symbol"]):
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is None:
                raise OrderExecutionError(
                    f"order_send() ha ritornato None per '{plan['symbol']}' "
                    f"(magic {plan['magic']}): {mt5.last_error()}"
                )
            if result.retcode != invalid_fill:
                break
            logger.warning(
                "Filling mode %s non supportato su %s: provo la successiva.",
                filling, plan["symbol"],
            )

        success = result.retcode == mt5.TRADE_RETCODE_DONE
        order_result = OrderResult(
            plan_key=plan_key,
            magic=int(plan["magic"]),
            success=success,
            retcode=int(result.retcode),
            ticket=getattr(result, "order", None) or None,
            volume=float(plan["volume"]),
            price=float(request["price"]),
            comment=getattr(result, "comment", "") or "",
        )

        if success:
            logger.info(
                "Ordine OK | %s magic=%s ticket=%s vol=%s @ %s",
                plan["symbol"], plan["magic"], order_result.ticket,
                order_result.volume, order_result.price,
            )
        else:
            logger.error(
                "Ordine RIFIUTATO | %s magic=%s retcode=%s :: %s",
                plan["symbol"], plan["magic"], order_result.retcode,
                order_result.comment,
            )
        return order_result

    def send_single_order(self, order: OrderPlan) -> OrderResult:
        """Invia 1 solo ordine col 100% del lotto (magic 1000 = MAGIC_MAIN).

        ``order`` e' l'``OrderPlan`` ritornato da ``TradeValidator.build_order()``.
        Le chiusure parziali (30% TP1, 30% TP2) sono gestite dal
        PartialCloseTracker in position_monitor.py.
        MT5 chiude automaticamente il remainder al TP dell'ordine (target piu' lontano).
        """
        return self.place_order(order, plan_key="main")


# ---------------------------------------------------------------------------
# Factory di comodo
# ---------------------------------------------------------------------------

def build_engine_from_config(
    mt5_module: Optional[object] = None,
) -> MT5Engine:
    """Crea un ``MT5Engine`` usando i parametri di ``config`` (senza inizializzare)."""
    try:
        mode = ExecutionMode(config.EXECUTION_MODE)
    except ValueError:
        logger.warning(
            "EXECUTION_MODE '%s' non valido: uso 'market'.", config.EXECUTION_MODE
        )
        mode = ExecutionMode.MARKET
    return MT5Engine(
        mt5_module=mt5_module,
        deviation=config.ORDER_DEVIATION,
        execution_mode=mode,
    )


__all__ = [
    "MT5Engine",
    "OrderResult",
    "ExecutionMode",
    "MT5ConnectionError",
    "OrderExecutionError",
    "build_engine_from_config",
]
