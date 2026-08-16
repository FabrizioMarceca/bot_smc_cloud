"""Regression tests for MT5 health checks and reconnect-safe ingress gating."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from mt5_engine import ExecutionMode, MT5Engine
from trade_manager import make_mock_symbol_info_provider
from webhook_server import create_app


class FakeHealthyMT5:
    TIMEFRAME_M15 = 15
    TIMEFRAME_M5 = 5
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2

    def __init__(self) -> None:
        self.account_available = True
        self.tick_available = True
        self.symbol_available = True
        self.positions_available = True
        self.orders_available = True
        self.initialize_calls = 0
        self.shutdown_calls = 0

    def initialize(self, **kwargs) -> bool:
        self.initialize_calls += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def account_info(self):
        if not self.account_available:
            return None
        return SimpleNamespace(balance=10000.0, equity=10000.0)

    def positions_get(self, *args, **kwargs):
        return [] if self.positions_available else None

    def orders_get(self, *args, **kwargs):
        return [] if self.orders_available else None

    def symbol_info(self, symbol: str):
        if not self.symbol_available:
            return None
        return SimpleNamespace(visible=True, filling_mode=3)

    def symbol_info_tick(self, symbol: str):
        if not self.tick_available:
            return None
        return SimpleNamespace(bid=1.1000, ask=1.1001)


class ReconnectTests(unittest.TestCase):
    def test_health_check_accepts_live_account_empty_positions_and_orders(self) -> None:
        fake = FakeHealthyMT5()
        engine = MT5Engine(mt5_module=fake, execution_mode=ExecutionMode.MARKET)
        engine.initialize()
        self.assertTrue(engine.health_check(("EURUSD",)))

    def test_health_check_rejects_missing_account(self) -> None:
        fake = FakeHealthyMT5()
        fake.account_available = False
        engine = MT5Engine(mt5_module=fake)
        engine.initialize()
        self.assertFalse(engine.health_check(("EURUSD",)))

    def test_health_check_rejects_missing_symbol_or_tick(self) -> None:
        for field in ("symbol_available", "tick_available"):
            fake = FakeHealthyMT5()
            setattr(fake, field, False)
            engine = MT5Engine(mt5_module=fake)
            engine.initialize()
            self.assertFalse(engine.health_check(("EURUSD",)), field)

    def test_health_check_rejects_unreadable_positions_or_pending_orders(self) -> None:
        for field in ("positions_available", "orders_available"):
            fake = FakeHealthyMT5()
            setattr(fake, field, False)
            engine = MT5Engine(mt5_module=fake)
            engine.initialize()
            self.assertFalse(engine.health_check(("EURUSD",)), field)

    def test_webhook_returns_503_while_master_is_reconnecting(self) -> None:
        class ReconnectingMaster:
            is_mt5_ready = False

        app = create_app(
            engine=None,
            master_bot=ReconnectingMaster(),
            notifier=None,
            symbol_info_provider=make_mock_symbol_info_provider(),
            secret_token="",
        )
        response = app.test_client().post(
            "/webhook",
            json={"symbol": "EURUSD", "side": "buy"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "mt5_disconnected")


if __name__ == "__main__":
    unittest.main()
