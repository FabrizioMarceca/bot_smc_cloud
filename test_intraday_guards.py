"""Regression tests for daytrading price-level guards.

These tests are broker-free: they exercise the pure validation layer used
immediately before an autonomous order is sent to MT5.
"""

from __future__ import annotations

import unittest

from utils import pip_size, validate_intraday_levels


class IntradayGuardTests(unittest.TestCase):
    def test_jpy_pip_size(self) -> None:
        self.assertEqual(pip_size("USDJPY"), 0.01)

    def test_rejects_stale_usdjpy_pending_setup(self) -> None:
        # Regression case from the terminal: market ~157.710, entry 163.538.
        ok, reason = validate_intraday_levels(
            "USDJPY", "sell", 163.538, 163.758, 157.260,
            157.710, "daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("distante", reason)

    def test_accepts_realistic_daytrading_sell(self) -> None:
        # 25 pip SL and 50 pip final target: exactly 1:2.
        ok, reason = validate_intraday_levels(
            "USDJPY", "sell", 157.738, 157.988, 157.238,
            157.710, "daytrading",
        )
        self.assertTrue(ok, reason)

    def test_accepts_long_daytrading_target_without_a_tp_cap(self) -> None:
        # Il TP non ha un tetto in pip: il vincolo daytrading è il RR finale.
        ok, reason = validate_intraday_levels(
            "USDJPY", "sell", 157.738, 157.988, 150.000,
            157.710, "daytrading",
        )
        self.assertTrue(ok, reason)

    def test_allows_near_tp1_but_requires_final_tp_at_two_r(self) -> None:
        ok, reason = validate_intraday_levels(
            "USDJPY", "sell", 157.738, 157.988, 157.238,
            157.710, "daytrading",
            tp_levels=(157.700, 157.238),
        )
        self.assertTrue(ok, reason)

    def test_rejects_final_rr_below_two_r(self) -> None:
        ok, reason = validate_intraday_levels(
            "USDJPY", "sell", 157.738, 157.988, 157.488,
            157.710, "daytrading",
            tp_levels=(157.700, 157.488),
        )
        self.assertFalse(ok)
        self.assertIn("R:R", reason)

    def test_rejects_wrong_side_levels(self) -> None:
        ok, reason = validate_intraday_levels(
            "USDJPY", "sell", 157.738, 157.700, 157.498,
            157.710, "daytrading",
        )
        self.assertFalse(ok)
        self.assertIn("SL", reason)


if __name__ == "__main__":
    unittest.main()
