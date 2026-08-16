"""
diagnose_signals.py
===================
Diagnostica completa: mostra tutti gli OB, le zone PD, equilibrium,
entry/sl/tp calcolati e il MOTIVO esatto per cui non ci sono segnali.
Esegue solo lettura, non piazza ordini.
"""
from __future__ import annotations

import sys
import io

# Forza output UTF-8 su Windows (evita UnicodeEncodeError cp1252)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from mt5_adapter import mt5
import structure_analyzer as sa
import config

SYMBOL = "XAUUSD"

def main() -> None:
    if not mt5.initialize():
        print("ERRORE: MT5 non connesso.")
        return

    tick = mt5.symbol_info_tick(SYMBOL)
    prezzo = float(tick.bid) if tick else 0.0
    print(f"\n{'='*70}")
    print(f"  DIAGNOSTICA SMC - {SYMBOL} | prezzo attuale: {prezzo:.2f}")
    print(f"{'='*70}")

    last_trend = "sideways"

    for tf_name, tf, pw in [("H4", mt5.TIMEFRAME_H4, 3), ("M15", mt5.TIMEFRAME_M15, 4)]:
        print(f"\n{'-'*70}")
        print(f"  TIMEFRAME {tf_name}")
        print(f"{'-'*70}")

        df = sa.get_market_data(SYMBOL, tf, bars=200)
        if df is None or df.empty:
            print("  Nessun dato.")
            continue

        df = sa.identify_swings(df, window=pw)
        swings = sa.filter_alternating_swings(df)
        if swings.empty:
            print("  Nessuno swing.")
            continue

        swings = sa.label_structure(swings)
        swings = sa.classify_strong_weak(swings)
        swings = sa.detect_structure_breaks(swings)
        trend = sa.get_trend_direction(swings)
        last_trend = trend

        # Equilibrium
        labeled = swings[swings["label"] != ""]
        highs = labeled[labeled["type"] == "high"]["price_level"]
        lows = labeled[labeled["type"] == "low"]["price_level"]
        range_high = highs.iloc[-1] if not highs.empty else 0
        range_low = lows.iloc[-1] if not lows.empty else 0
        hh_data = labeled[labeled["label"] == "HH"]
        ll_data = labeled[labeled["label"] == "LL"]
        if not hh_data.empty:
            range_high = float(hh_data["price_level"].iloc[-1])
        if not ll_data.empty:
            range_low = float(ll_data["price_level"].iloc[-1])
        equilibrium = (range_high + range_low) / 2

        print(f"  Trend: {trend}")
        print(f"  Range: {range_low:.2f} -> {range_high:.2f}")
        print(f"  Equilibrium (50%): {equilibrium:.2f}")
        print(f"  Prezzo attuale: {prezzo:.2f} -> zona: {sa.get_fibonacci_zone(prezzo, range_high, range_low)}")
        print(f"  Swings totali: {len(swings)}")

        # Struttura: stampa ultimi swing
        print(f"\n  Ultimi 8 swing:")
        for _, s in swings.tail(8).iterrows():
            ev = s.get("structure_event", "") or ""
            st = s.get("strength", "") or ""
            print(f"    {s['time']}  {s['label']:>3}  {s['type']:>5}  "
                  f"lvl={s['price_level']:.2f}  strength={st:<6}  event={ev}")

        # Structure events
        events = [e for e in swings["structure_event"].tolist() if e]
        print(f"\n  Structure events trovati: {events if events else 'NESSUNO'}")

        # Order Blocks
        obs_raw = sa.identify_order_blocks(df, swings)
        print(f"\n  Order Blocks grezzi (prima filtri): {len(obs_raw)}")
        for _, ob in obs_raw.iterrows():
            print(f"    {ob['tipo_zona']:>22}  top={ob['top_ob']:.2f}  bottom={ob['bottom_ob']:.2f}  "
                  f"swing={ob['label_swing']}")

        obs_mit = sa.filter_mitigated_obs(df, obs_raw)
        print(f"\n  OB dopo filtro mitigazione: {len(obs_mit)}")

        obs_pd = sa.apply_pd_matrix(swings, obs_mit)
        print(f"  OB dopo PD matrix: {len(obs_pd)}")
        for _, ob in obs_pd.iterrows():
            print(f"    {ob['tipo_zona']:>22}  top={ob['top_ob']:.2f}  bottom={ob['bottom_ob']:.2f}  "
                  f"zone={ob.get('pd_zone','?')}  eq={ob.get('equilibrium',0):.2f}")

        obs_final = sa.detect_liquidity_sweeps(df, obs_pd)
        print(f"  OB dopo liquidity sweep: {len(obs_final)}")
        for _, ob in obs_final.iterrows():
            print(f"    {ob['tipo_zona']:>22}  top={ob['top_ob']:.2f}  bottom={ob['bottom_ob']:.2f}  "
                  f"zone={ob.get('pd_zone','?')}  sweep={ob.get('liquidity_sweep','none')}")

        # Liquidity zones
        liq = sa.find_liquidity_zones(df, swings)
        print(f"\n  Liquidity zones: {len(liq)}")
        for _, lz in liq.iterrows():
            print(f"    {lz['type']:>4}  lvl={lz['price_level']:.2f}  count={lz['candle_count']}  "
                  f"strong={lz['is_strong']}")

        # Simula generate_signals e mostra PERCHE' non c'e' segnale
        print(f"\n  --- ANALISI SEGNALI (simulazione) ---")
        if obs_final.empty:
            print("  Nessun OB valido -> impossibile generare segnali.")
            continue

        liq_map: dict[str, list[float]] = {"BSL": [], "SSL": []}
        if not liq.empty:
            for _, lz in liq.iterrows():
                liq_map[lz["type"]].append(float(lz["price_level"]))

        for _, ob in obs_final.iterrows():
            tipo = str(ob["tipo_zona"])
            pd_zone = str(ob.get("pd_zone", ""))
            sweep = str(ob.get("liquidity_sweep", "none"))

            print(f"\n  OB: {tipo} | zone={pd_zone} | sweep={sweep}")
            print(f"    top={ob['top_ob']:.2f}  bottom={ob['bottom_ob']:.2f}")

            if "Demand" in tipo and pd_zone == "Discount":
                direction = "buy"
                entry = float(ob["top_ob"])
                sl = float(ob["bottom_ob"])
                tps = sorted([lz for lz in liq_map.get("BSL", []) if lz > entry])
                tp1 = tps[0] if tps else entry + (entry - sl) * 3.0
                risk = entry - sl
                rr = (tp1 - entry) / risk if risk > 0 else 0
                is_pro = (trend == "bullish")
                tp2 = entry + risk * 5.0 if risk > 0 else entry
                min_rr = config.MIN_RR if is_pro else 2.0
                print(f"    -> Setup BUY | pro-trend={is_pro} (trend={trend})")
                print(f"    -> Entry={entry:.2f}  SL={sl:.2f}  TP1={tp1:.2f}  TP2={tp2:.2f}")
                print(f"    -> R:R = {rr:.2f}  (minimo richiesto: {min_rr})")
                print(f"    -> Distanza entry da prezzo: {abs(entry - prezzo):.2f}")

                rejected = False
                if rr < min_rr:
                    print(f"    [X] SCARTATO: R:R {rr:.2f} < {min_rr}")
                    rejected = True
                if direction == "buy" and trend == "bearish":
                    if "TC_bullish" not in events and "MSS_bullish" not in events:
                        print(f"    [X] SCARTATO: contro-trend (buy vs bearish) senza TC_bullish/MSS_bullish")
                        rejected = True
                if not rejected:
                    print(f"    [OK] Condizioni OK - segnale verrebbe generato!")

            elif "Supply" in tipo and pd_zone == "Premium":
                direction = "sell"
                entry = float(ob["bottom_ob"])
                sl = float(ob["top_ob"])
                tps = sorted([lz for lz in liq_map.get("SSL", []) if lz < entry], reverse=True)
                tp1 = tps[0] if tps else entry - (sl - entry) * 3.0
                risk = sl - entry
                rr = (entry - tp1) / risk if risk > 0 else 0
                is_pro = (trend == "bearish")
                tp2 = entry - risk * 5.0 if risk > 0 else entry
                min_rr = config.MIN_RR if is_pro else 2.0
                print(f"    -> Setup SELL | pro-trend={is_pro} (trend={trend})")
                print(f"    -> Entry={entry:.2f}  SL={sl:.2f}  TP1={tp1:.2f}  TP2={tp2:.2f}")
                print(f"    -> R:R = {rr:.2f}  (minimo richiesto: {min_rr})")
                print(f"    -> Distanza entry da prezzo: {abs(entry - prezzo):.2f}")

                rejected = False
                if rr < min_rr:
                    print(f"    [X] SCARTATO: R:R {rr:.2f} < {min_rr}")
                    rejected = True
                if direction == "sell" and trend == "bullish":
                    if "TC_bearish" not in events and "MSS_bearish" not in events:
                        print(f"    [X] SCARTATO: contro-trend (sell vs bullish) senza TC_bearish/MSS_bearish")
                        rejected = True
                if not rejected:
                    print(f"    [OK] Condizioni OK - segnale verrebbe generato!")
            else:
                print(f"    [X] SCARTATO: tipo/zone non compatibili ({tipo} in {pd_zone})")

    # Riepilogo finale
    print(f"\n{'='*70}")
    print(f"  RIEPILOGO: cosa deve succedere per un segnale?")
    print(f"{'='*70}")
    print(f"  1. Un OB Demand deve essere in Discount (sotto equilibrium) -> BUY")
    print(f"  2. Un OB Supply deve essere in Premium (sopra equilibrium) -> SELL")
    print(f"  3. R:R deve essere >= {config.MIN_RR} (pro-trend) o >= 2.0 (counter-trend)")
    print(f"  4. Se contro-trend: serve TC o MSS nello stesso verso")
    print(f"  Attualmente trend={last_trend} -> il bot cerca BUY in Discount (pro-trend)")
    print(f"{'='*70}\n")

    mt5.shutdown()

if __name__ == "__main__":
    main()
