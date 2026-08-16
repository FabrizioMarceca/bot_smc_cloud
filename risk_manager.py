import logging

from mt5_adapter import mt5
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

def calculate_lot_size(symbol, entry, sl, risk_pct, balance=None):
    """
    Calcola il lotto in base al rischio specificato.
    - Se balance è None, usa il valore del conto live.
    - Gestisce la precisione decimale e i limiti del broker.
    """
    # 1. Recupero dati
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)
    
    if symbol_info is None:
        logger.error("Impossibile trovare info per %s", symbol)
        return 0.01

    # 2. Setup Balance
    if balance is None:
        if account_info is None: return 0.01
        balance = Decimal(str(account_info.balance))
    else:
        balance = Decimal(str(balance))

    # 3. Parametri Broker
    step = Decimal(str(symbol_info.volume_step))
    min_lot = Decimal(str(symbol_info.volume_min))
    max_lot = Decimal(str(symbol_info.volume_max))
    tick_value = Decimal(str(symbol_info.trade_tick_value))
    tick_size = Decimal(str(symbol_info.trade_tick_size))
    
    # 4. Calcolo Rischio
    money_at_risk = balance * (Decimal(str(risk_pct)) / Decimal('100'))
    price_distance = Decimal(str(abs(entry - sl)))
    
    if price_distance == 0 or tick_size == 0 or tick_value == 0:
        return float(min_lot)

    # Formula corretta per XAUUSD e altri asset
    # loss_per_lot = (distanza_punti / tick_size) * tick_value
    loss_per_lot = (price_distance / tick_size) * tick_value
    
    if loss_per_lot <= 0:
        return float(min_lot)
        
    lotto_raw = money_at_risk / loss_per_lot
    
    # 5. Arrotondamento e Vincoli Broker
    lotto_rounded = (lotto_raw / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step
    
    # Usa il limite massimo del broker (niente cap artificiale)
    final_lotto = max(min_lot, min(max_lot, lotto_rounded))
    
    return float(final_lotto)