"""
trade_manager.py
================
Money Management ed esecuzione ordini per un bot MT5 alimentato da webhook
TradingView. Il modulo NON esegue analisi grafica: riceve segnali gia'
validati dalla strategia (TPS / SMC) e si occupa di:

    1. Validare il payload del webhook (coerenza direzionale + R:R minimo).
    2. Calcolare la Lot Size in base al rischio percentuale (config.RISK_PERCENT).
    3. Produrre 1 solo ordine col 100% del lotto, usando il magic della
       modalità (1002 daytrading, 1003 swing; 1000 resta solo legacy).
       Le chiusure parziali (30% TP1, 30% TP2, 40% TP3) sono gestite dal
       PartialCloseTracker in position_monitor.py.

Le regole numeriche derivano dai parametri in config.py (RISK_PERCENT).

Integrazione con MetaTrader5: la classe accetta un provider di symbol_info
iniettabile. In produzione si passa un adapter che chiama mt5.symbol_info();
in test si usa il mock incluso in fondo al file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Protocol, TypedDict


# ---------------------------------------------------------------------------
# Costanti di regola (da trading_rules.json)
# ---------------------------------------------------------------------------

# money_management.risk_per_trade_pct
# I valori reali vengono da config.RISK_PERCENT; questi sono fallback
# usati solo se config non e' disponibile (es. test standalone).
RISK_PCT_DEFAULT: float = 1.0  # fallback: 1% risk per trade
RISK_PCT_PROP_DEFAULT: float = 0.5  # fallback prop firm
RISK_PCT_MIN: float = 0.1   # minimo sicurezza
RISK_PCT_MAX: float = 20.0  # massimo sicurezza (accomoda 10% del conto reale)

# Import pigro di config (con fallback se non disponibile)
_logger = logging.getLogger(__name__)
try:
    import config as _config  # type: ignore[import]
    _RISK_PCT_FROM_CONFIG: float = float(getattr(_config, 'RISK_PERCENT', RISK_PCT_DEFAULT))
except Exception:
    _config = None  # type: ignore[assignment]
    _RISK_PCT_FROM_CONFIG: float = RISK_PCT_DEFAULT
    _logger.warning("config.RISK_PERCENT non disponibile — uso fallback %.1f%%", RISK_PCT_DEFAULT)

# risk_reward secondo le modalità attive.
# Daytrading: minimo 1:2 (materiale base); swing: minimo 1:4.
RR_MIN_PRO_TREND: float = 2.0
RR_MIN_COUNTER_TREND: float = 2.0
RR_MIN_DAYTRADING: float = 2.0
RR_MIN_SWING: float = 4.0
# Retrocompatibilita' (usato da test e moduli legacy)
RR_MIN_ABSOLUTE: float = RR_MIN_COUNTER_TREND

# take_profit (RR usati per derivare i target quando non arrivano dal webhook)
TP1_RR: float = 1.0
TP2_RR: float = 2.5
TP3_RR: float = 5.0   # target esteso (runner 40%)

# partial_close — percentuali di chiusura parziale ai vari TP
# TP1: chiude 30%, TP2: chiude 30% del rimanente, TP3: chiude il 40% restante
PARTIAL_CLOSE_TP1: float = 0.3
PARTIAL_CLOSE_TP2: float = 0.3
PARTIAL_CLOSE_TP3: float = 1.0   # 100% del rimanente = chiude tutto

# Magic number legacy per la posizione principale (non più split 30/30).
MAGIC_MAIN: int = 1000  # non associato automaticamente al daytrading
# Retrocompatibilita' per moduli legacy
MAGIC_TP1: int = 1001
MAGIC_TP2: int = 1002

# break_even.trigger_rr : sposta lo SL a entry quando il runner raggiunge 1:1
BREAK_EVEN_TRIGGER_RR: float = 1.0

# oro: unita' di misura lotto diversa dal forex (money_management.lot_size)
from utils import XAU_SYMBOLS, pip_size


# ---------------------------------------------------------------------------
# Eccezioni custom
# ---------------------------------------------------------------------------

class TradeManagerError(Exception):
    """Classe base per tutti gli errori del modulo."""


class InvalidSignalError(TradeManagerError):
    """Sollevata quando il segnale non supera la validazione.

    Casi tipici: campi mancanti, side/setup_type non ammessi, coerenza
    direzionale violata (SL dalla parte sbagliata dell'entry) o R:R
    insufficiente (min 1:3 daytrading, 1:4 swing).
    """


class LotSizingError(TradeManagerError):
    """Sollevata quando il calcolo del volume non e' possibile.

    Casi tipici: distanza SL nulla, symbol_info incompleto/assente, lotto
    risultante inferiore al minimo del broker una volta splittato.
    """


# ---------------------------------------------------------------------------
# Tipi di dominio
# ---------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SetupType(str, Enum):
    PRO_TREND = "pro_trend"
    COUNTER_TREND = "counter_trend"


class SymbolInfo(TypedDict):
    """Sottoinsieme di mt5.symbol_info() usato dal money management."""
    trade_tick_size: float
    trade_tick_value: float
    volume_min: float
    volume_max: float
    volume_step: float
    digits: int


class OrderPlan(TypedDict):
    """Singolo ordine pronto per mt5.order_send()."""
    symbol: str
    side: str
    volume: float
    entry: float
    sl: float
    tp: float
    magic: int
    comment: str
    mode: str


class SymbolInfoProvider(Protocol):
    """Contratto per il provider di symbol_info (MT5 reale o mock)."""
    def __call__(self, symbol: str) -> Optional[SymbolInfo]: ...


# ---------------------------------------------------------------------------
# Payload validato
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatedSignal:
    """Segnale normalizzato e verificato, pronto per il sizing."""
    symbol: str
    side: Side
    setup_type: SetupType
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: Optional[float]   # None = TP3 non fattibile, chiudi tutto a TP2
    balance: float
    risk_pct: float
    mode: Optional[str] = None
    risk_distance: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_distance", abs(self.entry - self.sl))

    @property
    def has_tp2(self) -> bool:
        """True se TP2 e' diverso da TP1 (cioe' esiste un secondo target)."""
        return abs(self.tp2 - self.tp1) > 0.0001

    @property
    def has_tp3(self) -> bool:
        """True se TP3 e' definito e diverso da TP2."""
        return self.tp3 is not None and abs(self.tp3 - self.tp2) > 0.0001

    @property
    def farthest_tp(self) -> float:
        """Il TP piu' lontano nella direzione del trade (per MT5).
        BUY  → prezzo piu' alto  (max profit)
        SELL → prezzo piu' basso (max profit)
        MT5 chiudera' automaticamente il remainder quando il prezzo lo raggiunge."""
        candidates = [self.tp1]
        if self.has_tp2:
            candidates.append(self.tp2)
        if self.has_tp3 and self.tp3 is not None:
            candidates.append(self.tp3)
        return max(candidates) if self.side == Side.BUY else min(candidates)


# ---------------------------------------------------------------------------
# Validator + sizing + split
# ---------------------------------------------------------------------------

class TradeValidator:
    """Valida un payload webhook, calcola la lot size e genera gli ordini MT5.

    Parameters
    ----------
    payload:
        Dizionario grezzo del webhook TradingView. Campi richiesti:
        ``symbol``, ``side``, ``entry``, ``sl``, ``setup_type``, ``balance``.
        Campi opzionali: ``tp1``, ``tp2``, ``risk_pct_override``.
    symbol_info_provider:
        Callable ``symbol -> SymbolInfo | None``. In produzione avvolge
        ``mt5.symbol_info()``; in test si passa un mock.
    prop_mode:
        Se True applica i rischi ridotti da prop firm (0.5% / 0.25%).
    """

    _REQUIRED_FIELDS: tuple[str, ...] = (
        "symbol", "side", "entry", "sl", "setup_type", "balance",
    )

    def __init__(
        self,
        payload: dict,
        symbol_info_provider: SymbolInfoProvider,
        *,
        prop_mode: bool = False,
    ) -> None:
        self._payload = payload
        self._get_symbol_info = symbol_info_provider
        self._prop_mode = prop_mode
        self._signal: Optional[ValidatedSignal] = None

    # -- API pubblica -------------------------------------------------------

    def validate(self) -> ValidatedSignal:
        """Valida il payload e ritorna il segnale normalizzato.

        Deriva i TP dalle regole se non presenti nel payload, verifica la
        coerenza direzionale e impone il minimo R:R della modalità (1:3
        daytrading, 1:4 swing) sul target finale.

        Raises
        ------
        InvalidSignalError
            Se un controllo fallisce.
        """
        self._check_required_fields()

        symbol = str(self._payload["symbol"]).upper()
        side = self._parse_side(self._payload["side"])
        setup_type = self._parse_setup_type(self._payload["setup_type"])
        mode = self._parse_mode(self._payload.get("mode"))
        if mode is None:
            raise InvalidSignalError(
                "Campo 'mode' obbligatorio: specificare 'daytrading' oppure 'swing'."
            )
        effective_mode = mode
        entry = self._parse_positive_float("entry", self._payload["entry"])
        sl = self._parse_positive_float("sl", self._payload["sl"])
        balance = self._parse_positive_float("balance", self._payload["balance"])

        risk_distance = abs(entry - sl)
        if risk_distance <= 0.0:
            raise InvalidSignalError(
                f"Distanza SL nulla: entry ({entry}) coincide con sl ({sl})."
            )

        self._check_directional_coherence(side, entry, sl)
        self._check_sl_range(symbol, entry, sl, mode)

        tp1, tp2, tp3 = self._resolve_take_profits(side, entry, risk_distance, effective_mode)
        self._check_tp_order(side, entry, tp1, tp2, tp3)
        final_tp = max([tp1, tp2] + ([tp3] if tp3 is not None else [])) if side is Side.BUY else min([tp1, tp2] + ([tp3] if tp3 is not None else []))
        self._check_risk_reward(entry, final_tp, risk_distance, setup_type, effective_mode)
        if mode == "swing":
            self._check_swing_target_rr(entry, risk_distance, tp1, tp2, tp3)

        risk_pct = self._resolve_risk_pct(setup_type)

        signal = ValidatedSignal(
            symbol=symbol,
            side=side,
            setup_type=setup_type,
            entry=entry,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            balance=balance,
            risk_pct=risk_pct,
            mode=mode,
        )
        self._signal = signal
        return signal

    def calculate_lot_size(self, signal: Optional[ValidatedSignal] = None) -> float:
        """Calcola il volume TOTALE (pre-split) per il segnale.

        Formula (money_management.lot_size):
            value_per_point = tick_value / tick_size
            raw_lot = (balance * risk_pct/100) / (sl_distance * value_per_point)
        arrotondata al volume_step e limitata a [volume_min, volume_max].

        Raises
        ------
        LotSizingError
            Se symbol_info e' assente/incompleto o la distanza SL e' nulla.
        """
        signal = self._require_signal(signal)
        info = self._get_symbol_info(signal.symbol)
        if info is None:
            raise LotSizingError(
                f"symbol_info non disponibile per '{signal.symbol}'."
            )
        self._validate_symbol_info(info, signal.symbol)

        tick_size = info["trade_tick_size"]
        tick_value = info["trade_tick_value"]
        value_per_point = tick_value / tick_size  # tick_size gia' > 0

        risk_amount = signal.balance * (signal.risk_pct / 100.0)
        denominator = signal.risk_distance * value_per_point
        if denominator <= 0.0:
            raise LotSizingError(
                "Denominatore non valido nel calcolo lotti "
                f"(sl_distance={signal.risk_distance}, "
                f"value_per_point={value_per_point})."
            )

        raw_lot = risk_amount / denominator
        return self._round_to_broker_limits(raw_lot, info)

    def build_order(self, signal: Optional[ValidatedSignal] = None) -> OrderPlan:
        """Ritorna UN SOLO ordine col 100% del lotto, pronto per MT5.

        Il magic viene derivato dalla modalità: 1002 daytrading, 1003 swing.

        Strategia SMC con chiusure parziali:
        - Si apre 1 posizione col 100% del lotto calcolato sul rischio.
        - Il TP sull'ordine e' il target piu' lontano (tp3 > tp2 > tp1).
        - Le chiusure parziali (30% a TP1, 30% a TP2) sono gestite dal
          PositionMonitor nel loop break-even.
        - MT5 chiude automaticamente il remainder quando il prezzo
          raggiunge il TP impostato sull'ordine.
        - Se tp2 non e' fattibile (=tp1), il TP e' tp1 e MT5 chiude tutto.
        - Se tp3 non e' fattibile, il TP e' tp2 e MT5 chiude il remainder.

        Raises
        ------
        LotSizingError
            Se il lotto totale risulta sotto il minimo del broker.
        """
        signal = self._require_signal(signal)
        info = self._get_symbol_info(signal.symbol)
        if info is None:
            raise LotSizingError(
                f"symbol_info non disponibile per '{signal.symbol}'."
            )
        self._validate_symbol_info(info, signal.symbol)

        total_lot = self.calculate_lot_size(signal)

        if total_lot < info["volume_min"]:
            raise LotSizingError(
                f"Lotto totale {total_lot} sotto il minimo del broker "
                f"({info['volume_min']}) per '{signal.symbol}'. "
                f"Aumentare il rischio o il balance."
            )

        digits = info["digits"]
        farthest_tp = signal.farthest_tp
        tp_label = ("TP3" if signal.has_tp3 else "TP2" if signal.has_tp2 else "TP1")

        mode = signal.mode or "daytrading"
        mode_magic = (
            _config.get_mode_magic(mode)
            if _config is not None and hasattr(_config, "get_mode_magic")
            else MAGIC_MAIN
        )
        order: OrderPlan = {
            "symbol": signal.symbol,
            "side": signal.side.value,
            "volume": total_lot,
            "entry": round(signal.entry, digits),
            "sl": round(signal.sl, digits),
            "tp": round(farthest_tp, digits),
            "magic": mode_magic,
            "comment": f"SMC {mode} {tp_label} runner",
            "mode": mode,
        }
        return order

    # -- Helpers di validazione --------------------------------------------

    def _check_required_fields(self) -> None:
        missing = [f for f in self._REQUIRED_FIELDS if self._payload.get(f) is None]
        if missing:
            raise InvalidSignalError(
                f"Campi obbligatori mancanti nel payload: {missing}."
            )

    @staticmethod
    def _parse_side(raw: object) -> Side:
        try:
            return Side(str(raw).strip().lower())
        except ValueError:
            raise InvalidSignalError(
                f"side '{raw}' non valido. Ammessi: 'buy', 'sell'."
            )

    @staticmethod
    def _parse_setup_type(raw: object) -> SetupType:
        try:
            return SetupType(str(raw).strip().lower())
        except ValueError:
            raise InvalidSignalError(
                f"setup_type '{raw}' non valido. "
                "Ammessi: 'pro_trend', 'counter_trend'."
            )

    @staticmethod
    def _parse_mode(raw: object) -> Optional[str]:
        if raw is None or str(raw).strip() == "":
            return None
        mode = str(raw).strip().lower()
        if mode not in {"daytrading", "swing"}:
            raise InvalidSignalError(
                f"mode '{raw}' non valido. Ammessi: daytrading, swing."
            )
        return mode

    @staticmethod
    def _parse_positive_float(name: str, raw: object) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise InvalidSignalError(f"Campo '{name}' non numerico: {raw!r}.")
        if value <= 0.0:
            raise InvalidSignalError(f"Campo '{name}' deve essere > 0 (ricevuto {value}).")
        return value

    @staticmethod
    def _check_directional_coherence(side: Side, entry: float, sl: float) -> None:
        # signal_validation.coerenza_direzionale
        if side is Side.BUY and not (sl < entry):
            raise InvalidSignalError(
                f"BUY incoerente: lo SL ({sl}) deve stare SOTTO l'entry ({entry})."
            )
        if side is Side.SELL and not (sl > entry):
            raise InvalidSignalError(
                f"SELL incoerente: lo SL ({sl}) deve stare SOPRA l'entry ({entry})."
            )

    def _resolve_take_profits(
        self, side: Side, entry: float, risk_distance: float,
        mode: str = "daytrading",
    ) -> tuple[float, float, Optional[float]]:
        """Usa i TP del payload se presenti e coerenti, altrimenti li deriva.

        Returns: (tp1, tp2, tp3) dove tp3 puo' essere None.
        """
        direction = 1.0 if side is Side.BUY else -1.0
        default_tp1_rr = RR_MIN_SWING if mode == "swing" else TP1_RR
        default_tp2_rr = 5.0 if mode == "swing" else max(2.0, TP2_RR)
        default_tp1 = entry + direction * risk_distance * default_tp1_rr
        default_tp2 = entry + direction * risk_distance * default_tp2_rr
        default_tp3_rr = 7.0 if mode == "swing" else TP3_RR
        default_tp3 = entry + direction * risk_distance * default_tp3_rr

        tp1 = self._optional_tp("tp1", default_tp1)
        tp2 = self._optional_tp("tp2", default_tp2)
        tp3 = self._optional_tp_or_none("tp3", default_tp3)

        # Il target deve stare nella direzione del trade rispetto all'entry.
        for label, tp in (("tp1", tp1), ("tp2", tp2)):
            if direction * (tp - entry) <= 0.0:
                raise InvalidSignalError(
                    f"{label} ({tp}) non e' nella direzione del trade {side.value}."
                )
        # tp3 e' opzionale: se presente, validalo
        if tp3 is not None and direction * (tp3 - entry) <= 0.0:
            raise InvalidSignalError(
                f"tp3 ({tp3}) non e' nella direzione del trade {side.value}."
            )
        return tp1, tp2, tp3

    def _optional_tp(self, name: str, fallback: float) -> float:
        raw = self._payload.get(name)
        if raw is None:
            return fallback
        return self._parse_positive_float(name, raw)

    def _optional_tp_or_none(self, name: str, fallback: float) -> Optional[float]:
        """Come _optional_tp ma ritorna None se il campo non e' presente
        (invece del fallback)."""
        raw = self._payload.get(name)
        if raw is None:
            return None  # tp3 non fornito = non fattibile
        return self._parse_positive_float(name, raw)

    @staticmethod
    def _check_sl_range(symbol: str, entry: float, sl: float, mode: Optional[str]) -> None:
        if mode is None or _config is None:
            return
        pip = pip_size(symbol)
        sl_pips = abs(entry - sl) / pip
        min_sl = _config.get_sl_min_pips(symbol, mode)
        max_sl = _config.get_sl_max_pips(symbol, mode)
        if sl_pips < min_sl - 1e-6:
            raise InvalidSignalError(
                f"SL distante {sl_pips:.1f} pip; minimo {min_sl} pip per {mode}."
            )
        if sl_pips > max_sl + 1e-6:
            raise InvalidSignalError(
                f"SL distante {sl_pips:.1f} pip; massimo {max_sl} pip per {mode}."
            )

    @staticmethod
    def _check_tp_order(side: Side, entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> None:
        targets = [tp1, tp2] + ([tp3] if tp3 is not None else [])
        direction = 1 if side is Side.BUY else -1
        for index in range(1, len(targets)):
            if direction * (targets[index] - targets[index - 1]) < 0:
                raise InvalidSignalError(
                    f"TP{index + 1} non è più lontano di TP{index}."
                )

    @staticmethod
    def _check_swing_target_rr(
        entry: float, risk_distance: float,
        tp1: float, tp2: float, tp3: Optional[float],
    ) -> None:
        """Nello swing anche TP1 deve essere almeno 4R."""
        targets = [tp1, tp2] + ([tp3] if tp3 is not None else [])
        for index, target in enumerate(targets, start=1):
            rr = abs(target - entry) / risk_distance
            if rr < RR_MIN_SWING:
                raise InvalidSignalError(
                    f"R:R TP{index} {rr:.2f} inferiore al minimo swing (1:4)."
                )

    @staticmethod
    def _check_risk_reward(
        entry: float, tp_final: float, risk_distance: float,
        setup_type: Optional[SetupType] = None, mode: Optional[str] = None,
    ) -> None:
        # R:R minimo: lo swing vieta 1:2 e 1:3 (minimo 1:4).
        min_rr = RR_MIN_COUNTER_TREND if setup_type is SetupType.COUNTER_TREND else RR_MIN_PRO_TREND
        if mode == "swing":
            min_rr = RR_MIN_SWING
        elif mode == "daytrading":
            min_rr = RR_MIN_DAYTRADING

        rr = abs(tp_final - entry) / risk_distance
        if rr < min_rr - 1e-9:
            min_label = f"1:{min_rr:g}"
            raise InvalidSignalError(
                f"R:R {rr:.2f} inferiore al minimo consentito "
                f"({min_label}). Segnale scartato."
            )

    def _resolve_risk_pct(self, setup_type: SetupType) -> float:
        override = self._payload.get("risk_pct_override")
        if override is not None:
            value = self._parse_positive_float("risk_pct_override", override)
            if not (RISK_PCT_MIN <= value <= RISK_PCT_MAX):
                raise InvalidSignalError(
                    f"risk_pct_override {value} fuori dal range consentito "
                    f"[{RISK_PCT_MIN}, {RISK_PCT_MAX}]."
                )
            return value

        # Usa config.RISK_PERCENT con bounds validation
        base_risk = _RISK_PCT_FROM_CONFIG
        if not (RISK_PCT_MIN <= base_risk <= RISK_PCT_MAX):
            _logger.warning(
                "config.RISK_PERCENT=%.1f fuori range [%.1f, %.1f] — uso fallback %.1f%%",
                base_risk, RISK_PCT_MIN, RISK_PCT_MAX, RISK_PCT_DEFAULT,
            )
            base_risk = RISK_PCT_DEFAULT

        if self._prop_mode:
            base_risk = RISK_PCT_PROP_DEFAULT

        if setup_type is SetupType.PRO_TREND:
            return base_risk
        else:
            return base_risk * 0.5  # counter-trend: meta' rischio

    # -- Helpers di sizing --------------------------------------------------

    @staticmethod
    def _validate_symbol_info(info: SymbolInfo, symbol: str) -> None:
        required = ("trade_tick_size", "trade_tick_value",
                    "volume_min", "volume_max", "volume_step", "digits")
        for key in required:
            if info.get(key) is None:
                raise LotSizingError(f"symbol_info['{key}'] mancante per '{symbol}'.")
        if info["trade_tick_size"] <= 0.0:
            raise LotSizingError(f"trade_tick_size non valido per '{symbol}'.")
        if info["volume_step"] <= 0.0:
            raise LotSizingError(f"volume_step non valido per '{symbol}'.")

    @staticmethod
    def _round_to_step(volume: float, info: SymbolInfo) -> float:
        step = info["volume_step"]
        steps = round(volume / step)
        rounded = steps * step
        # Normalizza i decimali coerentemente con lo step (evita 0.30000000004).
        decimals = TradeValidator._step_decimals(step)
        return round(rounded, decimals)

    def _round_to_broker_limits(self, raw_lot: float, info: SymbolInfo) -> float:
        rounded = self._round_to_step(raw_lot, info)
        if rounded < info["volume_min"]:
            return info["volume_min"]
        if rounded > info["volume_max"]:
            return info["volume_max"]
        return rounded

    @staticmethod
    def _step_decimals(step: float) -> int:
        text = f"{step:.10f}".rstrip("0")
        if "." not in text:
            return 0
        return len(text.split(".", 1)[1])

    def _require_signal(self, signal: Optional[ValidatedSignal]) -> ValidatedSignal:
        resolved = signal or self._signal
        if resolved is None:
            raise TradeManagerError(
                "Nessun segnale validato: chiamare validate() prima del sizing."
            )
        return resolved


# ---------------------------------------------------------------------------
# Gestione posizioni aperte: break-even sul runner
# ---------------------------------------------------------------------------

class BreakEvenManager:
    """Sposta lo Stop Loss a break-even sul runner quando raggiunge 1:1.

    Lavora su posizioni GIA' aperte (tutti i magic SMC di default). NON
    inizializza ne' chiude la connessione MT5: la sessione e' gestita dal
    loop chiamante (run_master), cosi' il metodo puo' girare ogni pochi
    secondi senza aprire e chiudere il terminale a ogni ciclo.

    Il modulo MetaTrader5 viene importato pigramente: la classe resta
    importabile e testabile anche su una macchina senza terminale MT5.
    """

    def __init__(
        self,
        *,
        magic_runner: int = MAGIC_MAIN,
        trigger_rr: float = BREAK_EVEN_TRIGGER_RR,
        mt5_module: Optional[object] = None,
        extra_magics: frozenset[int] | None = None,
    ) -> None:
        # Supporta sia magic singolo (retro-compatibile) che multipli
        if extra_magics:
            self._magic_runners: frozenset[int] = extra_magics | {magic_runner}
        else:
            self._magic_runners = frozenset({magic_runner})
        self._trigger_rr = trigger_rr
        self._mt5 = mt5_module  # iniettabile per i test; None => import a runtime

    def _mt5_lib(self) -> object:
        if self._mt5 is None:
            from mt5_adapter import mt5  # import locale
            self._mt5 = mt5
        return self._mt5

    def secure_runners(self, symbol: str) -> list[int]:
        """Controlla le posizioni del simbolo e mette a BE i runner idonei.

        Ritorna la lista dei ticket effettivamente spostati a break-even.
        Non solleva eccezioni sui singoli fallimenti di order_send: le
        segnala tramite il valore di ritorno (ticket assenti dalla lista).
        """
        mt5 = self._mt5_lib()
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return []

        moved: list[int] = []
        for pos in positions:
            if pos.magic not in self._magic_runners:
                continue
            if not self._needs_break_even(pos, mt5):
                continue
            if self._modify_sl_to_entry(symbol, pos, mt5):
                moved.append(pos.ticket)
        return moved

    def _needs_break_even(self, pos: object, mt5: object) -> bool:
        """True se il runner ha raggiunto il trigger 1:1 e lo SL non e' gia' a BE."""
        open_price: float = pos.price_open
        current_sl: float = pos.sl
        current_price: float = pos.price_current

        if pos.type == mt5.ORDER_TYPE_BUY:
            # SL gia' a BE o oltre => niente da fare
            if current_sl >= open_price:
                return False
            initial_risk = open_price - current_sl
            if initial_risk <= 0.0:
                return False
            profit_distance = current_price - open_price
            return profit_distance >= initial_risk * self._trigger_rr

        if pos.type == mt5.ORDER_TYPE_SELL:
            if current_sl <= open_price:
                return False
            initial_risk = current_sl - open_price
            if initial_risk <= 0.0:
                return False
            profit_distance = open_price - current_price
            return profit_distance >= initial_risk * self._trigger_rr

        return False

    def _modify_sl_to_entry(self, symbol: str, pos: object, mt5: object) -> bool:
        """Invia la modifica SLTP portando lo SL al prezzo d'apertura."""
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": pos.ticket,
            "sl": float(pos.price_open),
            "tp": float(pos.tp),
        }
        result = mt5.order_send(request)
        return bool(result) and result.retcode == mt5.TRADE_RETCODE_DONE


# ---------------------------------------------------------------------------
# Provider MT5 reale e mock
# ---------------------------------------------------------------------------

def mt5_symbol_info_provider(symbol: str) -> Optional[SymbolInfo]:
    """Adapter di produzione: legge i dati dal terminale MT5.

    Richiede un terminale gia' inizializzato (mt5.initialize()) e il simbolo
    visibile nel Market Watch. Ritorna None se il simbolo non e' disponibile.
    """
    from mt5_adapter import mt5  # import locale: il modulo resta testabile senza MT5

    raw = mt5.symbol_info(symbol)
    if raw is None:
        return None
    return {
        "trade_tick_size": raw.trade_tick_size,
        "trade_tick_value": raw.trade_tick_value,
        "volume_min": raw.volume_min,
        "volume_max": raw.volume_max,
        "volume_step": raw.volume_step,
        "digits": raw.digits,
    }


def make_mock_symbol_info_provider(
    overrides: Optional[dict[str, SymbolInfo]] = None,
) -> Callable[[str], Optional[SymbolInfo]]:
    """Factory di un provider mock per test/integrazione senza terminale MT5."""
    defaults: dict[str, SymbolInfo] = {
        "EURUSD": {
            "trade_tick_size": 0.00001, "trade_tick_value": 1.0,
            "volume_min": 0.01, "volume_max": 100.0,
            "volume_step": 0.01, "digits": 5,
        },
        "GBPUSD": {
            "trade_tick_size": 0.00001, "trade_tick_value": 1.0,
            "volume_min": 0.01, "volume_max": 100.0,
            "volume_step": 0.01, "digits": 5,
        },
        "XAUUSD": {
            "trade_tick_size": 0.01, "trade_tick_value": 1.0,
            "volume_min": 0.01, "volume_max": 50.0,
            "volume_step": 0.01, "digits": 2,
        },
    }
    table = {**defaults, **(overrides or {})}

    def _provider(symbol: str) -> Optional[SymbolInfo]:
        return table.get(symbol.upper())

    return _provider


# ---------------------------------------------------------------------------
# Esempio d'uso (rimosso in produzione)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    payload = {
        "symbol": "EURUSD",
        "side": "buy",
        "entry": 1.0850,
        "sl": 1.0830,
        "setup_type": "pro_trend",
        "balance": 10000,
    }

    provider = make_mock_symbol_info_provider()
    validator = TradeValidator(payload, provider)

    signal = validator.validate()
    total_lot = validator.calculate_lot_size(signal)
    order = validator.build_order(signal)

    print(f"Segnale valido su {signal.symbol} {signal.side.value} "
          f"| rischio {signal.risk_pct}% | R:R finale "
          f"{abs(signal.tp2 - signal.entry) / signal.risk_distance:.2f}")
    print(f"SL={signal.sl}  TP1={signal.tp1}  TP2={signal.tp2}")
    print(f"Lotto totale: {total_lot}")
    print(f"  [MAIN] magic={order['magic']} vol={order['volume']} "
          f"tp={order['tp']} :: {order['comment']}")