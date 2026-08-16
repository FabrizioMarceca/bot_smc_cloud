"""
diagnostica_ordine.py
=====================
Diagnostica del filling mode: interroga il broker per capire QUALE modalita' di
riempimento accetta per un ordine a mercato, SENZA piazzare ordini veri (usa
mt5.order_check(), che valida soltanto).

Lancia con MT5 aperto e loggato:  python diagnostica_ordine.py
"""

from __future__ import annotations

from mt5_adapter import mt5

import config

SYMBOL = config.SYMBOLS[0] if config.SYMBOLS else "XAUUSD"

EXE_MODE = {0: "REQUEST", 1: "INSTANT", 2: "MARKET", 3: "EXCHANGE"}
FILLINGS = {
    "FOK": mt5.ORDER_FILLING_FOK,
    "IOC": mt5.ORDER_FILLING_IOC,
    "RETURN": mt5.ORDER_FILLING_RETURN,
}


def main() -> None:
    if not mt5.initialize():
        print("initialize() fallita:", mt5.last_error())
        print("-> apri MT5 e fai login a mano, poi riprova.")
        return

    info = mt5.symbol_info(SYMBOL)
    if info is None:
        print(f"Simbolo {SYMBOL} non trovato. Aggiungilo al Market Watch.")
        mt5.shutdown()
        return
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)
        info = mt5.symbol_info(SYMBOL)

    tick = mt5.symbol_info_tick(SYMBOL)
    print("=" * 60)
    print(f" DIAGNOSTICA FILLING — {SYMBOL}")
    print("=" * 60)
    print(f"filling_mode (bitmask): {info.filling_mode}  "
          f"(1=FOK, 2=IOC, 3=FOK+IOC)")
    print(f"trade_exemode         : {info.trade_exemode} "
          f"({EXE_MODE.get(info.trade_exemode, '?')})")
    print(f"volume_min            : {info.volume_min}")
    print(f"trade_stops_level     : {info.trade_stops_level} (points)")
    print(f"ask/bid               : {tick.ask} / {tick.bid}")
    print(f"point                 : {info.point}")
    print("-" * 60)

    base = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": info.volume_min,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": round(tick.ask - 100 * info.point, info.digits),
        "tp": round(tick.ask + 250 * info.point, info.digits),
        "deviation": 20,
        "magic": 999999,
        "comment": "diag",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    print("Test order_check() per ogni filling (NON piazza ordini):")
    ok_modes = []
    for name, filling in FILLINGS.items():
        req = {**base, "type_filling": filling}
        res = mt5.order_check(req)
        code = res.retcode if res else "None"
        comment = res.comment if res else ""
        flag = ""
        # retcode 0 o 10009 (DONE) => la request e' valida
        if res and res.retcode in (0, mt5.TRADE_RETCODE_DONE):
            flag = "  <-- ACCETTATO"
            ok_modes.append(name)
        print(f"  {name:7s} retcode={code} :: {comment}{flag}")

    print("-" * 60)
    if ok_modes:
        print(f"Filling accettati dal broker: {', '.join(ok_modes)}")
    else:
        print("Nessun filling accettato: incolla l'output qui sopra per l'analisi.")
    mt5.shutdown()


if __name__ == "__main__":
    main()
