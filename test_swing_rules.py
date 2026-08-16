"""Regression tests for the swing strategy rules."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import config
from mt5_engine import ExecutionMode, MT5Engine, OrderExecutionError
from smc_signals import (
    generate_sweep_entry,
    validate_swing_context,
    validate_swing_ltf_confirmation,
    validate_daytrading_ltf_confirmation,
    validate_daytrading_counter_trend,
    validate_liquidity_environment,
    validate_pre_entry_liquidity,
    validate_volatility_filter,
    detect_dxy_conflict,
    classify_reversal,
)
from trade_manager import InvalidSignalError, TradeValidator, make_mock_symbol_info_provider
from utils import validate_intraday_levels
from webhook_server import create_app, infer_trading_mode


class SwingRulesTests(unittest.TestCase):
    def _clean_sweep(self, *, direction: str = "buy", price: float = 1.1000) -> dict:
        if direction == "buy":
            candle = {"high": price + 0.0005, "low": price - 0.0010, "close": price + 0.0002}
            sweep_type = "SSL"
        else:
            candle = {"high": price + 0.0010, "low": price - 0.0005, "close": price - 0.0002}
            sweep_type = "BSL"
        return {
            "swept": True, "type": sweep_type, "price": price,
            "bars_ago": 1, "sweep_candle": candle,
        }

    def test_pre_entry_rejects_unverified_clean_flag_without_candle(self) -> None:
        sweep = self._clean_sweep()
        sweep.pop("sweep_candle")
        sweep["clean"] = True
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=pd.DataFrame(),
            sweep_check=sweep,
            reversal={"type": "institutional", "displacement": True},
            entry=1.1005, sl=1.0985, direction="buy", min_rr=2.0,
            mode="daytrading", symbol="EURUSD",
        )
        self.assertFalse(ok)
        self.assertIn("candela sweep", reason)

    def test_pre_entry_rejects_sweep_without_reclaim(self) -> None:
        sweep = self._clean_sweep()
        sweep["sweep_candle"] = {"high": 1.1002, "low": 1.0999, "close": 1.09995}
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=pd.DataFrame(),
            sweep_check=sweep, reversal={"type": "institutional", "displacement": True},
            entry=1.1005, sl=1.0985, direction="buy", min_rr=2.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("reclaim", reason)

    def test_pre_entry_rejects_residual_same_side_liquidity_after_sweep(self) -> None:
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None,
            liq_zones=pd.DataFrame([
                {"type": "SSL", "price_level": 1.0998},
            ]),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1005, sl=1.0985, direction="buy", min_rr=2.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("residua", reason)

    def test_pre_entry_rejects_single_front_level(self) -> None:
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None,
            liq_zones=pd.DataFrame([{"type": "BSL", "price_level": 1.1010}]),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("davanti", reason)

    def test_pre_entry_rejects_unmitigated_retail_level_inside_risk(self) -> None:
        swings = pd.DataFrame([{
            "type": "low", "price_level": 1.0992,
            "time": pd.Timestamp("2026-01-01"),
        }])
        df = pd.DataFrame([{
            "time": pd.Timestamp("2026-01-02"), "high": 1.0995,
            "low": 1.0993, "close": 1.0994,
        }])
        ok, reason = validate_pre_entry_liquidity(
            df=df, swings=swings, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("non mitigato", reason)

    def test_pre_entry_rejects_opposite_liquidity_below_required_rr(self) -> None:
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None,
            liq_zones=pd.DataFrame([{"type": "BSL", "price_level": 1.1030}]),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=4.0, mode="swing",
        )
        self.assertFalse(ok)
        self.assertIn("opposta", reason)

    def test_daytrading_ignores_h4_target_distance_but_swing_rejects_it(self) -> None:
        zones = pd.DataFrame([{"type": "BSL", "price_level": 1.1030}])
        sweep = self._clean_sweep()
        reversal = {"type": "institutional", "displacement": True}
        day_ok, day_reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=zones,
            sweep_check=sweep, reversal=reversal,
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0,
            mode="daytrading", target_levels=None,
        )
        self.assertTrue(day_ok, day_reason)
        swing_ok, swing_reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=zones,
            sweep_check=sweep, reversal=reversal,
            entry=1.1000, sl=1.0985, direction="buy", min_rr=4.0,
            mode="swing", target_levels=None,
        )
        self.assertFalse(swing_ok)
        self.assertIn("opposta", swing_reason)

    def test_pre_entry_rejects_already_extended_entry(self) -> None:
        sweep = self._clean_sweep(price=1.1000)
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=pd.DataFrame(),
            sweep_check=sweep,
            reversal={"type": "institutional", "displacement": True},
            entry=1.1060, sl=1.1040, direction="buy", min_rr=2.0, mode="swing",
        )
        self.assertFalse(ok)
        self.assertIn("esteso", reason)

    def test_pre_entry_accepts_clean_open_path(self) -> None:
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1005, sl=1.0985, direction="buy", min_rr=2.0, mode="daytrading",
        )
        self.assertTrue(ok, reason)

    def test_pre_entry_swing_requires_opposite_liquidity(self) -> None:
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=None, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1005, sl=1.0985, direction="buy", min_rr=4.0, mode="swing",
        )
        self.assertFalse(ok)
        self.assertIn("nessuna liquidità opposta", reason)

    def test_pre_entry_rejects_structural_level_in_front_of_entry(self) -> None:
        swings = pd.DataFrame([{"type": "high", "price_level": 1.1010}])
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=swings, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0,
            mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("davanti", reason)

    def test_pre_entry_accepts_truthy_mitigated_flag(self) -> None:
        swings = pd.DataFrame([{
            "type": "low", "price_level": 1.0992,
            "time": pd.Timestamp("2026-01-01"), "mitigated": 1,
            "mitigation_verified": True,
        }])
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=swings, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0,
            mode="daytrading",
        )
        self.assertTrue(ok, reason)

    def test_pre_entry_rejects_unverified_mitigated_flag(self) -> None:
        swings = pd.DataFrame([{
            "type": "low", "price_level": 1.0992,
            "time": pd.Timestamp("2026-01-01"), "mitigated": 1,
        }])
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=swings, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0,
            mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("non mitigato", reason)

    def test_pre_entry_rejects_nan_mitigated_flag(self) -> None:
        swings = pd.DataFrame([{
            "type": "low", "price_level": 1.0992,
            "time": pd.Timestamp("2026-01-01"),
            "mitigated": float("nan"), "mitigation_verified": True,
        }])
        ok, reason = validate_pre_entry_liquidity(
            df=None, swings=swings, liq_zones=pd.DataFrame(),
            sweep_check=self._clean_sweep(),
            reversal={"type": "institutional", "displacement": True},
            entry=1.1000, sl=1.0985, direction="buy", min_rr=2.0,
            mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("non mitigato", reason)

    def test_daytrading_counter_trend_requires_two_r(self) -> None:
        # EURUSD daytrading: rischio 15 pip, reward finale 30 pip = 1:2.
        ok, reason = validate_intraday_levels(
            "EURUSD", "buy", 1.1000, 1.0985, 1.1030,
            1.1000, "daytrading",
        )
        self.assertTrue(ok, reason)

    def test_daytrading_rejects_final_rr_below_two_r(self) -> None:
        ok, reason = validate_intraday_levels(
            "EURUSD", "buy", 1.1000, 1.0985, 1.1010,
            1.1000, "daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("1:2", reason)

    def test_daytrading_webhook_without_mode_is_rejected(self) -> None:
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0985,
            "tp1": 1.1005, "tp2": 1.1030,
            "setup_type": "counter_trend", "balance": 10000,
        }
        with self.assertRaisesRegex(InvalidSignalError, r"mode.*obbligatorio"):
            TradeValidator(payload, make_mock_symbol_info_provider()).validate()

    def test_daytrading_webhook_counter_trend_requires_two_r(self) -> None:
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0985,
            "tp1": 1.1005, "tp2": 1.1030,
            "setup_type": "counter_trend", "mode": "daytrading",
            "balance": 10000,
        }
        signal = TradeValidator(payload, make_mock_symbol_info_provider()).validate()
        self.assertEqual(signal.mode, "daytrading")
        self.assertGreaterEqual(
            abs(signal.farthest_tp - signal.entry) / signal.risk_distance,
            2.0 - 1e-9,
        )

    def test_daytrading_ranges_match_requested_elastic_bands(self) -> None:
        self.assertEqual(config.get_sl_min_pips("EURUSD", "daytrading"), 15)
        self.assertEqual(config.get_sl_max_pips("EURUSD", "daytrading"), 25)
        self.assertEqual(config.get_sl_min_pips("GBPUSD", "daytrading"), 15)
        self.assertEqual(config.get_sl_max_pips("GBPUSD", "daytrading"), 25)
        self.assertEqual(config.get_sl_min_pips("GBPJPY", "daytrading"), 25)
        self.assertEqual(config.get_sl_max_pips("GBPJPY", "daytrading"), 40)
        self.assertEqual(config.get_sl_min_pips("USDJPY", "daytrading"), 25)
        self.assertEqual(config.get_sl_max_pips("USDJPY", "daytrading"), 40)
        self.assertEqual(config.get_sl_min_pips("XAUUSD", "daytrading"), 50)
        self.assertEqual(config.get_sl_max_pips("XAUUSD", "daytrading"), 100)

    def test_swing_sl_tolerance_is_25_percent_and_daytrading_is_not_extended(self) -> None:
        # I massimi nominali swing restano quelli della strategia, ma il
        # validatore ammette il 25% extra solo nello swing.
        self.assertEqual(config.get_sl_nominal_max_pips("EURUSD", "swing"), 30)
        self.assertEqual(config.get_sl_max_pips("EURUSD", "swing"), 37.5)
        self.assertEqual(config.get_sl_max_pips("EURUSD", "daytrading"), 25.0)
        self.assertEqual(config.get_sl_max_pips("XAUUSD", "swing"), 200.0)
        self.assertEqual(config.get_sl_max_pips("XAUUSD", "daytrading"), 100.0)

        # 35 pip su EURUSD swing: oltre il range nominale, ma dentro 37.5.
        ok, reason = validate_intraday_levels(
            "EURUSD", "buy", 1.1000, 1.0965, 1.1140,
            1.1000, "swing",
        )
        self.assertTrue(ok, reason)

        # 38 pip supera il massimo swing esteso e viene rifiutato.
        ok, reason = validate_intraday_levels(
            "EURUSD", "buy", 1.1000, 1.0962, 1.1152,
            1.1000, "swing",
        )
        self.assertFalse(ok)
        self.assertIn("massimo", reason)

        # XAUUSD resta rigido a 100-200 pip nello swing: 201 pip è rifiutato.
        ok, reason = validate_intraday_levels(
            "XAUUSD", "buy", 4000.0, 3980.0, 4080.0,
            4000.0, "swing",
        )
        self.assertTrue(ok, reason)
        ok, reason = validate_intraday_levels(
            "XAUUSD", "buy", 4000.0, 3979.9, 4080.4,
            4000.0, "swing",
        )
        self.assertFalse(ok)
        self.assertIn("massimo", reason)

    def test_structural_swing_sl_uses_extended_ceiling(self) -> None:
        # La struttura richiede più del massimo nominale EURUSD swing (30),
        # ma il limite operativo esteso del 25% consente 37,5 pip.
        from structure_analyzer import find_h4_structural_sl

        swings = pd.DataFrame([{"type": "low", "price_level": 1.0960}])
        sl = find_h4_structural_sl(
            swings, 1.1000, "buy", 15, config.get_sl_max_pips("EURUSD", "swing"), 0.0001,
        )
        self.assertAlmostEqual((1.1000 - sl) / 0.0001, 37.5, places=6)

    def test_daytrading_sweep_uses_two_r_even_counter_trend(self) -> None:
        signal = generate_sweep_entry(
            {"swept": True, "type": "SSL", "price": 1.1000, "bars_ago": 1},
            {"type": "institutional", "confidence": 90},
            "bearish", "buy", 10, 0.0001,
            min_rr=2.0, mode="daytrading",
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["rr"], 2.0)

    def test_mode_magic_is_distinct_for_daytrading_and_swing(self) -> None:
        day_payload = {
            "symbol": "EURUSD", "side": "buy", "entry": 1.1000,
            "sl": 1.0985, "tp2": 1.1030, "setup_type": "pro_trend",
            "mode": "daytrading", "balance": 10000,
        }
        swing_payload = {
            "symbol": "EURUSD", "side": "buy", "entry": 1.1000,
            "sl": 1.0980, "tp2": 1.1080, "setup_type": "pro_trend",
            "mode": "swing", "balance": 10000,
        }
        provider = make_mock_symbol_info_provider()
        day_order = TradeValidator(day_payload, provider).build_order(
            TradeValidator(day_payload, provider).validate()
        )
        swing_validator = TradeValidator(swing_payload, provider)
        swing_order = swing_validator.build_order(swing_validator.validate())
        self.assertEqual(day_order["magic"], config.get_mode_magic("daytrading"))
        self.assertEqual(swing_order["magic"], config.get_mode_magic("swing"))
        self.assertNotEqual(day_order["magic"], swing_order["magic"])

    def test_mode_timeframes_keep_h1_in_swing_not_daytrading(self) -> None:
        self.assertEqual(config.get_mode_timeframes("daytrading"), ("H4", "M15", "M1"))
        self.assertEqual(config.get_mode_timeframe_pipeline("daytrading"), ("D1", "H4", "M15", "M5", "M1"))
        self.assertEqual(config.get_mode_timeframes("swing"), ("H4", "H1", "M15"))

    def test_timeframe_does_not_infer_webhook_mode(self) -> None:
        for timeframe in ("H1", "1H", "60", "60M", "M15", "5"):
            self.assertIsNone(infer_trading_mode(timeframe), timeframe)
        self.assertIsNone(infer_trading_mode("M15"))
        self.assertIsNone(infer_trading_mode("5"))

    def test_gbpusd_h1_trade_is_valid_swing_and_not_daytrading(self) -> None:
        # Schermata di riferimento: sell H1, entry 1.34611, SL 1.34865
        # (25.4 pip), TP 1.32814 (~179.7 pip, circa 1:7).
        swing_ok, swing_reason = validate_intraday_levels(
            "GBPUSD", "sell", 1.34611, 1.34865, 1.32814,
            1.34604, "swing",
        )
        self.assertTrue(swing_ok, swing_reason)

        day_ok, day_reason = validate_intraday_levels(
            "GBPUSD", "sell", 1.34611, 1.34865, 1.32814,
            1.34604, "daytrading",
        )
        self.assertFalse(day_ok)
        self.assertIn("SL", day_reason)

    def test_swing_accepts_target_beyond_120_pips_at_four_r(self) -> None:
        # EURUSD: rischio 20 pip, reward 200 pip = 1:10 (>120 pip).
        ok, reason = validate_intraday_levels(
            "EURUSD", "buy", 1.1000, 1.0980, 1.1200,
            1.1000, "swing",
        )
        self.assertTrue(ok, reason)

    def test_swing_rejects_final_rr_below_four(self) -> None:
        # Rischio 20 pip, reward 60 pip = 1:3: vietato nello swing.
        ok, reason = validate_intraday_levels(
            "EURUSD", "buy", 1.1000, 1.0980, 1.1060,
            1.1000, "swing",
        )
        self.assertFalse(ok)
        self.assertIn("1:4", reason)

    def test_swing_webhook_rejects_tp1_one_to_three_even_when_tp2_is_four_r(self) -> None:
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0980,
            "tp1": 1.1030, "tp2": 1.1080,
            "setup_type": "pro_trend", "mode": "swing",
            "balance": 10000,
        }
        validator = TradeValidator(payload, make_mock_symbol_info_provider())
        with self.assertRaisesRegex(InvalidSignalError, r"TP1.*1:4"):
            validator.validate()

    def test_swing_webhook_rejects_counter_trend_one_to_three(self) -> None:
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0980,
            "tp1": 1.1010, "tp2": 1.1060,
            "setup_type": "counter_trend", "mode": "swing",
            "balance": 10000,
        }
        validator = TradeValidator(payload, make_mock_symbol_info_provider())
        with self.assertRaisesRegex(InvalidSignalError, r"1:4"):
            validator.validate()

    def test_swing_webhook_defaults_targets_to_four_r_and_above(self) -> None:
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0980,
            "setup_type": "counter_trend", "mode": "swing",
            "balance": 10000,
        }
        signal = TradeValidator(payload, make_mock_symbol_info_provider()).validate()
        self.assertGreaterEqual(
            abs(signal.farthest_tp - signal.entry) / signal.risk_distance,
            4.0,
        )

    def test_swing_requires_htf_sweep_and_institutional_reversal(self) -> None:
        base = {
            "htf_ready": True,
            "htf_trend": "bullish",
            "sweep_check": {"swept": False, "bars_ago": 0},
            "reversal": {"type": "institutional"},
        }
        ok, reason = validate_swing_context(**base)
        self.assertFalse(ok)
        self.assertIn("sweep", reason)

        base["sweep_check"] = {"swept": True, "type": "SSL", "bars_ago": 1}
        base["reversal"] = {"type": "retail_pullback"}
        ok, reason = validate_swing_context(**base)
        self.assertFalse(ok)
        self.assertIn("istituzionale", reason)

        base["reversal"] = {"type": "institutional", "displacement": True}
        base["htf_trend"] = "bearish"
        ok, reason = validate_swing_context(**base)
        self.assertFalse(ok)
        self.assertIn("coerente", reason)

    def test_daytrading_ltf_requires_mss_or_tc_on_m5_and_m1(self) -> None:
        ok, reason = validate_daytrading_ltf_confirmation(
            direction="buy", m5_ready=True, m5_events=["MSS_bullish"],
            m1_ready=True, m1_events=["TC_bullish"],
        )
        self.assertTrue(ok, reason)
        ok, reason = validate_daytrading_ltf_confirmation(
            direction="buy", m5_ready=True, m5_events=["MSS_bullish"],
            m1_ready=True, m1_events=["SB_bearish"],
        )
        self.assertFalse(ok)
        self.assertIn("M1", reason)

    def test_daytrading_counter_trend_requires_sweep_and_mss_tc(self) -> None:
        ok, reason = validate_daytrading_counter_trend(
            direction="buy", trend="bearish",
            sweep_check={"swept": True, "type": "SSL", "bars_ago": 1},
            structure_events=["MSS_bullish"],
        )
        self.assertTrue(ok, reason)
        ok, reason = validate_daytrading_counter_trend(
            direction="buy", trend="bearish",
            sweep_check={"swept": False}, structure_events=["MSS_bullish"],
        )
        self.assertFalse(ok)
        self.assertIn("sweep", reason)

    def test_swing_requires_ltf_poi_and_confirmation(self) -> None:
        ok, reason = validate_swing_ltf_confirmation(
            ltf_ready=False, ltf_obs_count=0, confirmed_count=0,
        )
        self.assertFalse(ok)
        self.assertIn("LTF", reason)

        ok, reason = validate_swing_ltf_confirmation(
            ltf_ready=True, ltf_obs_count=2, confirmed_count=1,
            structure_confirmed_count=1,
        )
        self.assertTrue(ok, reason)

        ok, reason = validate_swing_ltf_confirmation(
            ltf_ready=True, ltf_obs_count=2, confirmed_count=1,
            structure_confirmed_count=0,
        )
        self.assertFalse(ok)
        self.assertIn("TC", reason)

    def test_swing_mss_alone_is_not_a_complete_confirmation(self) -> None:
        ok, reason = validate_swing_ltf_confirmation(
            ltf_ready=True, ltf_obs_count=1, confirmed_count=1,
            structure_confirmed_count=0,
        )
        self.assertFalse(ok)
        self.assertIn("MSS+SB", reason)

    def test_liquidity_environment_rejects_opposite_zone_too_close_in_swing(self) -> None:
        # BUY swing: entry 1.1000, SL 1.0985 (15 pip risk). BSL a 1.1020
        # = 1.33R: fuori dal corridoio (1R) ma sotto i 4R richiesti.
        liq = pd.DataFrame([{"type": "BSL", "price_level": 1.1020}])
        ok, reason = validate_liquidity_environment(
            liq, 1.1000, 1.0985, "buy", min_rr=4.0, mode="swing",
        )
        self.assertFalse(ok)
        self.assertIn("opposta", reason)

    def test_liquidity_environment_accepts_opposite_zone_far_enough_in_swing(self) -> None:
        liq = pd.DataFrame([{"type": "BSL", "price_level": 1.1060}])
        ok, reason = validate_liquidity_environment(
            liq, 1.1000, 1.0985, "buy", min_rr=4.0, mode="swing",
        )
        self.assertTrue(ok, reason)

    def test_liquidity_environment_daytrading_ignores_h4_zone_beyond_corridor(self) -> None:
        # Il target daytrade usa M5/M15: una BSL H4 a 1.33R (fuori dal
        # corridoio di 1R) non è un motivo di rifiuto per il daytrading.
        liq = pd.DataFrame([{"type": "BSL", "price_level": 1.1020}])
        ok, reason = validate_liquidity_environment(
            liq, 1.1000, 1.0985, "buy", min_rr=2.0, mode="daytrading",
        )
        self.assertTrue(ok, reason)

    def test_liquidity_environment_rejects_zone_right_in_front_of_entry(self) -> None:
        # SSL a 1.1006 (0.4R) davanti a un BUY: può essere spazzata contro
        # la posizione prima del target.
        liq = pd.DataFrame([{"type": "SSL", "price_level": 1.1006}])
        ok, reason = validate_liquidity_environment(
            liq, 1.1000, 1.0985, "buy", min_rr=2.0,
        )
        self.assertFalse(ok)
        self.assertIn("davanti", reason)

    def test_liquidity_environment_rejects_sell_corridor_zone(self) -> None:
        # SELL: SSL a 1.0992 (0.53R) davanti all'entry viene rifiutata.
        liq = pd.DataFrame([{"type": "SSL", "price_level": 1.0992}])
        ok, reason = validate_liquidity_environment(
            liq, 1.1000, 1.1015, "sell", min_rr=2.0,
        )
        self.assertFalse(ok)
        self.assertIn("davanti", reason)

    def test_volatility_rejects_spread_above_symbol_max(self) -> None:
        # EURUSD: spread 2.5 pip > massimo 2 pip -> rifiutato.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=2.5, avg_range_pips=8.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("massimo", reason)

    def test_volatility_rejects_spread_too_large_vs_sl(self) -> None:
        # XAUUSD (max spread 30 pip): SL 20 pip, spread 4 pip = 20%
        # dello SL -> rifiutato (max 15%).
        ok, reason = validate_volatility_filter(
            "XAUUSD", "buy", 4000.0, 3998.0,
            spread_pips=4.0, avg_range_pips=10.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("dello SL", reason)

    def test_volatility_accepts_normal_spread_and_range(self) -> None:
        # Spread 2 pip su SL 15 pip (13%) e candela M15 media 10 pip: ok.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=2.0, avg_range_pips=10.0, mode="daytrading",
        )
        self.assertTrue(ok, reason)

    def test_volatility_rejects_sl_too_small_vs_average_candle(self) -> None:
        # Candela media 40 pip, SL 15 pip = 0.375x (< 0.5x): stop da rumore.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=1.0, avg_range_pips=40.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("candela media", reason)

    def test_volatility_rejects_sl_too_large_vs_average_candle(self) -> None:
        # Candela media 2 pip, SL 15 pip = 7.5x (> 5x): movimento esteso.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=1.0, avg_range_pips=2.0, mode="daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("candela media", reason)

    def test_volatility_rejects_tp_unreachable_in_low_volatility(self) -> None:
        # TP 70 pip con range M15=10 e M5=5: il target richiede >6x il
        # movimento medio daytrading e non è realistico per la sessione.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=1.0, avg_range_pips=10.0, mode="daytrading",
            tp_price=1.1070, fast_avg_range_pips=5.0,
        )
        self.assertFalse(ok)
        self.assertIn("volatilità insufficiente", reason)

    def test_volatility_accepts_tp_with_sufficient_slow_and_fast_ranges(self) -> None:
        # TP 200 pip nello swing: 3.3x H1=60 e 6.7x M15=30, entro 12x.
        ok, reason = validate_volatility_filter(
            "XAUUSD", "buy", 4000.0, 3985.0,
            spread_pips=2.0, avg_range_pips=60.0, mode="swing",
            tp_price=4020.0, fast_avg_range_pips=30.0,
        )
        self.assertTrue(ok, reason)

    def test_volatility_rejects_current_fast_candle_spike(self) -> None:
        # La candela M5 corrente è uno spike 4x rispetto alla media: entry
        # instabile, anche se spread e SL sono nominalmente accettabili.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=1.0, avg_range_pips=10.0, mode="daytrading",
            tp_price=1.1030, fast_avg_range_pips=5.0,
            current_range_pips=20.0,
        )
        self.assertFalse(ok)
        self.assertIn("candela corrente anomala", reason)

    def test_volatility_still_checks_tp_when_spread_is_unavailable(self) -> None:
        # Tick assente (spread=0) non deve disattivare il filtro storico.
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=0.0, avg_range_pips=10.0, mode="daytrading",
            tp_price=1.1070, fast_avg_range_pips=5.0,
        )
        self.assertFalse(ok)
        self.assertIn("volatilità insufficiente", reason)

    def test_candidate_fillings_always_include_return_fallback(self) -> None:
        # Errore 5/8 (retcode 10030 su GBPUSD): il broker rifiutava FOK e IOC
        # e il loop non provava mai RETURN -> ordine perso. Il fallback RETURN
        # deve essere SEMPRE nell'elenco, anche quando il simbolo non dichiara
        # alcun filling_mode o ne dichiara solo alcuni.
        class FakeMt5:
            ORDER_FILLING_FOK = 0
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_RETURN = 2
            SYMBOL_FILLING_FOK = 1
            SYMBOL_FILLING_IOC = 2

            @staticmethod
            def symbol_info(symbol: str):
                return None  # nessun filling_mode dichiarato

        engine = MT5Engine(mt5_module=FakeMt5(), execution_mode=ExecutionMode.MARKET)
        cands = engine._candidate_fillings("EURUSD")
        self.assertEqual(cands[-1], FakeMt5.ORDER_FILLING_RETURN)
        self.assertEqual(set(cands), {0, 1, 2})

        # Simbolo che dichiara solo FOK: preferred = [FOK], poi fallback IOC/RETURN.
        class FakeMt5Fok:
            ORDER_FILLING_FOK = 0
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_RETURN = 2
            SYMBOL_FILLING_FOK = 1
            SYMBOL_FILLING_IOC = 2

            @staticmethod
            def symbol_info(symbol: str):
                return SimpleNamespace(filling_mode=1)

        engine2 = MT5Engine(mt5_module=FakeMt5Fok(), execution_mode=ExecutionMode.MARKET)
        cands2 = engine2._candidate_fillings("EURUSD")
        self.assertEqual(cands2[0], FakeMt5Fok.ORDER_FILLING_FOK)
        self.assertIn(FakeMt5Fok.ORDER_FILLING_RETURN, cands2)

    def test_place_order_retries_invalid_filling_until_return(self) -> None:
        # Simula il broker che rifiuta FOK e IOC con 10030 e accetta solo
        # RETURN: l'ordine deve essere eseguito al terzo tentativo.
        class FakeMt5Retry:
            ORDER_FILLING_FOK = 0
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_RETURN = 2
            SYMBOL_FILLING_FOK = 1
            SYMBOL_FILLING_IOC = 2
            TRADE_RETCODE_INVALID_FILL = 10030
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_DONE_PARTIAL = 10010
            TRADE_ACTION_DEAL = 1
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            ORDER_TIME_DAY = 0
            ORDER_TIME_GTC = 1

            def __init__(self) -> None:
                self.sent_fillings: list[int] = []
                self.sent_requests: list[dict] = []

            @staticmethod
            def symbol_info(symbol: str):
                return SimpleNamespace(
                    visible=True, filling_mode=3, digits=5,
                    trade_stops_level=0, point=0.00001, spread=2,
                )

            @staticmethod
            def symbol_info_tick(symbol: str):
                return SimpleNamespace(ask=1.10010, bid=1.10000)

            @staticmethod
            def symbol_select(symbol: str, enable: bool):
                return True

            @staticmethod
            def initialize(*args, **kwargs):
                return True

            @staticmethod
            def shutdown():
                return None

            @staticmethod
            def account_info():
                return SimpleNamespace(
                    balance=10000.0, equity=10000.0, margin_free=9000.0,
                    server="demo", profit=0.0,
                )

            @staticmethod
            def last_error():
                return (0, "ok")

            def order_send(self, request: dict):
                self.sent_requests.append(dict(request))
                filling = request["type_filling"]
                self.sent_fillings.append(filling)
                if filling == self.ORDER_FILLING_RETURN:
                    return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=777, comment="")
                return SimpleNamespace(retcode=self.TRADE_RETCODE_INVALID_FILL, order=0, comment="unsupported")

        fake = FakeMt5Retry()
        engine = MT5Engine(mt5_module=fake, execution_mode=ExecutionMode.MARKET)
        engine.initialize()
        plan = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0985, "tp": 1.1060,
            "magic": 1002, "comment": "test", "mode": "daytrading",
            "volume": 0.1,
        }
        result = engine.place_order(plan, plan_key="main")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.ticket, 777)
        self.assertEqual(fake.sent_fillings[-1], FakeMt5Retry.ORDER_FILLING_RETURN)

    def test_partial_close_accepts_done_partial_as_success(self) -> None:
        # Regression: se il broker risponde DONE_PARTIAL (eseguita solo in
        # parte), la chiusura NON deve essere considerata fallita: altrimenti
        # il loop BE ritenta e chiude piu' volume del previsto.
        from position_monitor import _do_partial_close, reset_tracker

        class FakeMt5Partial:
            ORDER_FILLING_FOK = 0
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_RETURN = 2
            SYMBOL_FILLING_FOK = 1
            SYMBOL_FILLING_IOC = 2
            TRADE_RETCODE_INVALID_FILL = 10030
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_DONE_PARTIAL = 10010
            TRADE_ACTION_DEAL = 1
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            POSITION_TYPE_BUY = 0

            @staticmethod
            def symbol_info(symbol: str):
                return SimpleNamespace(
                    visible=True, filling_mode=3, digits=5,
                    trade_stops_level=0, point=0.00001, spread=2,
                    volume_min=0.01, volume_step=0.01,
                )

            @staticmethod
            def positions_get(ticket=None, symbol=None):
                return [SimpleNamespace(ticket=ticket, volume=0.30)]

            @staticmethod
            def order_send(request: dict):
                return SimpleNamespace(
                    retcode=10010, order=999, comment="eseguito in parte",
                )

        reset_tracker()
        with patch("position_monitor.mt5", FakeMt5Partial()):
            detail = _do_partial_close(
                "EURUSD", 42, 0.30, 0.3, 1.1050, "BUY", "TP1",
            )
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["ticket"], 42)
        self.assertEqual(detail["pct"], 30)

    def test_webhook_market_order_rejects_excessive_spread(self) -> None:
        # Percorso webhook: build_request rifiuta uno spread sopra il massimo
        # del simbolo prima dell'invio. EURUSD max 2 pip, fake spread 25 pip.
        class FakeMt5:
            ORDER_FILLING_IOC = 2
            ORDER_FILLING_FOK = 1
            ORDER_FILLING_RETURN = 3
            TRADE_RETCODE_INVALID_FILL = 10030
            TRADE_ACTION_DEAL = 1
            TRADE_ACTION_PENDING = 5
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            ORDER_TIME_DAY = 0
            ORDER_TIME_GTC = 1

            @staticmethod
            def symbol_info(symbol: str):
                return None  # filling fallback

            @staticmethod
            def symbol_info_tick(symbol: str):
                return SimpleNamespace(ask=1.10025, bid=1.10000)  # 25 pip

        plan = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.1000, "sl": 1.0985, "tp": 1.1060,
            "magic": 1002, "comment": "test", "mode": "daytrading",
        }
        engine = MT5Engine(mt5_module=FakeMt5(), execution_mode=ExecutionMode.MARKET)
        with self.assertRaises(OrderExecutionError) as ctx:
            engine.build_request(plan)
        self.assertIn("Spread", str(ctx.exception))

    def test_volatility_swing_uses_h1_scale_not_m15(self) -> None:
        # XAUUSD swing: SL 150 pip contro candela H1 media 60 pip
        # (2.5x, dentro 0.5-5.0x): setup valido non ucciso dal filtro.
        ok, reason = validate_volatility_filter(
            "XAUUSD", "buy", 4000.0, 3985.0,
            spread_pips=5.0, avg_range_pips=60.0, mode="swing",
        )
        self.assertTrue(ok, reason)

    def test_pip_size_supports_broker_suffix_for_gold(self) -> None:
        from utils import pip_size
        self.assertEqual(pip_size("XAUUSD.pro"), 0.10)
        self.assertEqual(pip_size("EURUSDm"), 0.0001)

    def test_volatility_rejects_non_finite_or_negative_market_data(self) -> None:
        for kwargs, expected in (
            ({"spread_pips": float("nan"), "avg_range_pips": 10.0}, "non finiti"),
            ({"spread_pips": -0.1, "avg_range_pips": 10.0}, "negativi"),
            ({"spread_pips": 1.0, "avg_range_pips": float("inf")}, "non finiti"),
        ):
            ok, reason = validate_volatility_filter(
                "EURUSD", "buy", 1.1000, 1.0985,
                mode="daytrading", fast_avg_range_pips=5.0,
                **kwargs,
            )
            self.assertFalse(ok)
            self.assertIn(expected, reason)

    def test_volatility_requires_fast_range_when_pipeline_demands_it(self) -> None:
        ok, reason = validate_volatility_filter(
            "EURUSD", "buy", 1.1000, 1.0985,
            spread_pips=1.0, avg_range_pips=10.0, mode="daytrading",
            tp_price=1.1030, fast_avg_range_pips=0.0,
            require_fast_range=True,
        )
        self.assertFalse(ok)
        self.assertIn("volatilità veloce non disponibile", reason)

    def test_liquidity_environment_skips_when_no_opposite_zones(self) -> None:
        liq = pd.DataFrame([{"type": "SSL", "price_level": 1.0950}])
        ok, reason = validate_liquidity_environment(
            liq, 1.1000, 1.0985, "buy", min_rr=4.0, mode="swing",
        )
        self.assertTrue(ok, reason)
        self.assertIn("nessuna liquidità opposta", reason)

    def test_swing_reversal_without_displacement_is_rejected(self) -> None:
        reversal = {"type": "institutional", "displacement": False}
        self.assertEqual(reversal.get("displacement"), False)
        ok, reason = validate_swing_context(
            htf_ready=True, htf_trend="bullish",
            sweep_check={"swept": True, "type": "SSL", "bars_ago": 1},
            reversal=reversal,
        )
        self.assertFalse(ok)
        self.assertIn("displacement", reason)

    def test_swing_sweep_uses_small_entry_offset_and_four_r(self) -> None:
        signal = generate_sweep_entry(
            {"swept": True, "type": "SSL", "price": 1.1000, "bars_ago": 1},
            {"type": "institutional", "confidence": 90, "displacement": True},
            "bearish", "buy", 20, 0.0001,
            min_rr=3.0, mode="swing",
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["entry_offset_pips"], 2.0)
        self.assertEqual(signal["rr"], 4.0)

    # ------------------------------------------------------------------
    # PUNTO 5: blocco dei segnali con conflitto DXY
    # ------------------------------------------------------------------

    def test_dxy_conflict_inverse_for_eur_gbp_xau(self) -> None:
        # EURUSD/GBPUSD/XAUUSD: correlazione inversa col dollaro.
        ok, reason = detect_dxy_conflict("EURUSD", "buy", "bullish")
        self.assertTrue(ok)
        self.assertIn("DXY", reason)
        ok, reason = detect_dxy_conflict("EURUSD", "sell", "bullish")
        self.assertFalse(ok)
        ok, reason = detect_dxy_conflict("EURUSD", "sell", "bearish")
        self.assertTrue(ok)
        ok, reason = detect_dxy_conflict("GBPUSD", "buy", "bullish")
        self.assertTrue(ok)
        ok, reason = detect_dxy_conflict("XAUUSD", "buy", "bullish")
        self.assertTrue(ok)

    def test_dxy_conflict_direct_for_usdjpy(self) -> None:
        # USDJPY: correlazione diretta (segue la forza del dollaro).
        ok, reason = detect_dxy_conflict("USDJPY", "buy", "bearish")
        self.assertTrue(ok)
        ok, reason = detect_dxy_conflict("USDJPY", "sell", "bullish")
        self.assertTrue(ok)
        ok, reason = detect_dxy_conflict("USDJPY", "buy", "bullish")
        self.assertFalse(ok)
        ok, reason = detect_dxy_conflict("USDJPY", "sell", "bearish")
        self.assertFalse(ok)

    def test_dxy_conflict_cross_and_unavailable_are_neutral(self) -> None:
        # GBPJPY è un cross: il DXY non è un filtro direzionale affidabile.
        ok, reason = detect_dxy_conflict("GBPJPY", "buy", "bullish")
        self.assertFalse(ok)
        self.assertIn("non applicabile", reason)
        # DXY non disponibile: fail-open, nessun blocco.
        ok, reason = detect_dxy_conflict("EURUSD", "buy", None)
        self.assertFalse(ok)
        self.assertEqual(reason, "")
        # Simbolo fuori mappa: fail-open.
        ok, reason = detect_dxy_conflict("BTCUSD", "buy", "bullish")
        self.assertFalse(ok)

    def test_dxy_conflict_supports_usd_pairs_and_broker_suffixes(self) -> None:
        # Le coppie inverse e dirette devono rispettare il prefisso standard
        # anche quando il broker aggiunge un suffisso al simbolo.
        ok, reason = detect_dxy_conflict("AUDUSDm", "buy", "bullish")
        self.assertTrue(ok)
        self.assertIn("contro DXY", reason)
        ok, reason = detect_dxy_conflict("NZDUSD.pro", "sell", "bearish")
        self.assertTrue(ok)
        ok, reason = detect_dxy_conflict("USDCAD.r", "sell", "bullish")
        self.assertTrue(ok)
        ok, reason = detect_dxy_conflict("USDCHF", "buy", "bullish")
        self.assertFalse(ok)
        ok, reason = detect_dxy_conflict("EURJPY", "sell", "bearish")
        self.assertFalse(ok)
        self.assertIn("non applicabile", reason)
        ok, reason = detect_dxy_conflict("CADJPYm", "buy", "bullish")
        self.assertFalse(ok)
        self.assertIn("non applicabile", reason)
        # Un nome che contiene una coppia valida ma una base diversa non deve
        # essere classificato erroneamente come EURUSD.
        ok, reason = detect_dxy_conflict("EURUSDJPY", "buy", "bullish")
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_dxy_conflict_rejects_invalid_direction_or_trend(self) -> None:
        ok, reason = detect_dxy_conflict("EURUSD", "hold", "bullish")
        self.assertFalse(ok)
        self.assertEqual(reason, "")
        ok, reason = detect_dxy_conflict("EURUSD", "buy", "sideways")
        self.assertFalse(ok)
        self.assertEqual(reason, "")

    def test_webhook_dxy_conflict_blocks_signal(self) -> None:
        # Percorso webhook (punto 5): un BUY EURUSD con DXY bullish viene
        # rifiutato prima della validazione/ordine.
        class _FakeEngine:
            def account_balance(self):
                return 10000.0

        app = create_app(
            engine=_FakeEngine(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            prop_mode=False,
            secret_token="",
        )
        client = app.test_client()
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.0850, "sl": 1.0835, "tp2": 1.0880,
            "mode": "daytrading", "setup_type": "pro_trend", "balance": 10000,
        }
        with patch(
            "webhook_server._get_dxy_bias_raw",
            return_value={"trend": "bullish", "current_price": 105.0, "bias": "bullish_usd"},
        ):
            resp = client.post("/webhook", json=payload)
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(data.get("status"), "dxy_conflict")
        self.assertIn("DXY", str(data.get("error", "")))

    def test_webhook_dxy_unavailable_is_fail_open(self) -> None:
        # DXY non disponibile: il segnale NON viene bloccato dal filtro DXY
        # (fallisce solo la validazione R:R successiva, non per conflitto).
        class _FakeEngine:
            def account_balance(self):
                return 10000.0

        app = create_app(
            engine=_FakeEngine(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            prop_mode=False,
            secret_token="",
        )
        client = app.test_client()
        payload = {
            "symbol": "EURUSD", "side": "buy",
            "entry": 1.0850, "sl": 1.0835,
            "tp1": 1.0855, "tp2": 1.0860,  # R:R troppo basso
            "mode": "daytrading", "setup_type": "pro_trend", "balance": 10000,
        }
        with patch("webhook_server._get_dxy_bias_raw", return_value=None):
            resp = client.post("/webhook", json=payload)
        data = resp.get_json()
        self.assertEqual(resp.status_code, 400)
        self.assertNotEqual(data.get("status"), "dxy_conflict")
        self.assertEqual(data.get("status"), "rejected")


    def test_landing_route_serves_page(self) -> None:
        # GET / deve servire la landing page (status 200, contenuto HTML).
        class _FakeEngine:
            def account_balance(self):
                return 10000.0

        app = create_app(
            engine=_FakeEngine(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            prop_mode=False,
            secret_token="",
        )
        client = app.test_client()
        with patch("os.path.exists", return_value=True), \
                patch("builtins.open", unittest.mock.mock_open(read_data="<html>LANDING</html>")):
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("LANDING", resp.get_data(as_text=True))

    def test_landing_route_falls_back_to_dashboard(self) -> None:
        # Se landing.html non esiste, GET / deve reindirizzare a /dashboard.
        class _FakeEngine:
            def account_balance(self):
                return 10000.0

        app = create_app(
            engine=_FakeEngine(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            prop_mode=False,
            secret_token="",
        )
        client = app.test_client()
        with patch("os.path.exists", return_value=False):
            resp = client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("Location"), "/dashboard")


    def test_api_prices_returns_symbols_with_fail_open(self) -> None:
        # /api/prices deve restituire i prezzi dei simboli configurati quando
        # MT5 è connesso e una lista vuota (fail-open) quando non lo è.
        class _FakeEngine:
            is_initialized = True

        class _FakeMt5:
            TIMEFRAME_D1 = 16408

            @staticmethod
            def symbol_info_tick(symbol: str):
                if symbol == "XAUUSD":
                    return SimpleNamespace(bid=4000.0, ask=4000.5)
                return None  # simbolo non quotato: omesso

            @staticmethod
            def history_rates_get(symbol, timeframe, dt_from, dt_to):
                return [SimpleNamespace(close=3990.0), SimpleNamespace(close=4005.0)]

        app = create_app(
            engine=_FakeEngine(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            prop_mode=False,
            secret_token="",
        )
        client = app.test_client()
        with patch("mt5_adapter.mt5", _FakeMt5()):
            resp = client.get("/api/prices")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("mt5_connected"))
        self.assertEqual(len(data.get("prices", [])), 1)
        self.assertEqual(data["prices"][0]["symbol"], "XAUUSD")
        self.assertGreater(data["prices"][0]["change_pct"], 0.3)

        # MT5 non inizializzato: fail-open con lista vuota, nessun errore 500.
        class _FakeEngineOff:
            is_initialized = False

        app2 = create_app(
            engine=_FakeEngineOff(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            prop_mode=False,
            secret_token="",
        )
        resp2 = app2.test_client().get("/api/prices")
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(resp2.get_json().get("mt5_connected"))
        self.assertEqual(resp2.get_json().get("prices"), [])


if __name__ == "__main__":
    unittest.main()
