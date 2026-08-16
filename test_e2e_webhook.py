"""
test_e2e_webhook.py
===================
Test end-to-end SIMULATO del percorso completo, con MetaTrader 5 e Telegram
mockati (nessun terminale ne' rete reale richiesti):

    webhook POST  ->  TradeValidator.validate()  ->  build_orders()  ->
    MT5Engine.place_order()  ->  TelegramNotifier.notify_execution()

Copre anche:
  - segnale valido (200, ordine eseguito, notifica inviata)
  - segnale con R:R insufficiente (400 rejected)
  - token errato (401)
  - break-even sul runner al raggiungimento di 1:1

Eseguire con:  python test_e2e_webhook.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from mt5_engine import MT5Engine, ExecutionMode
from telegram_notifier import TelegramNotifier
from trade_manager import (
    BreakEvenManager,
    make_mock_symbol_info_provider,
    MAGIC_MAIN,
)
from webhook_server import create_app

SECRET = "test-secret-token"


# ---------------------------------------------------------------------------
# Mock di MetaTrader5
# ---------------------------------------------------------------------------

class FakeMT5:
    """Sostituto minimale del modulo MetaTrader5 per i test."""

    # Costanti (valori arbitrari ma coerenti internamente)
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        self.sent_requests: list[dict] = []
        self._ticket_seq = 5000
        self.positions: list[SimpleNamespace] = []

    # -- sessione --
    def initialize(self, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self):
        return (0, "no error")

    def account_info(self):
        return SimpleNamespace(server="Demo-Server", balance=10000.0,
                               equity=10000.0, margin_free=9500.0)

    # -- simboli / prezzi --
    def symbol_info(self, symbol: str):
        return SimpleNamespace(
            visible=True,
            filling_mode=self.SYMBOL_FILLING_FOK | self.SYMBOL_FILLING_IOC,
            digits=5 if symbol != "XAUUSD" else 2,
        )

    def symbol_info_tick(self, symbol: str):
        # spread simbolico attorno all'entry di test
        return SimpleNamespace(ask=1.08510, bid=1.08500)

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    # -- invio ordini --
    def order_send(self, request: dict):
        self.sent_requests.append(request)
        self._ticket_seq += 1
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=self._ticket_seq,
            comment="Request executed",
        )

    # -- posizioni aperte (per il break-even) --
    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]


# ---------------------------------------------------------------------------
# Mock della sessione HTTP di Telegram
# ---------------------------------------------------------------------------

class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, text="ok")


# ---------------------------------------------------------------------------
# Utility di test
# ---------------------------------------------------------------------------

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label} {('-> ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# Setup app + client
# ---------------------------------------------------------------------------

def build_client():
    fake_mt5 = FakeMT5()
    engine = MT5Engine(mt5_module=fake_mt5, execution_mode=ExecutionMode.MARKET)
    engine.initialize()

    http = FakeHTTP()
    notifier = TelegramNotifier(token="T", chat_id="C", session=http)

    provider = make_mock_symbol_info_provider()
    app = create_app(
        engine=engine,
        notifier=notifier,
        symbol_info_provider=provider,
        prop_mode=False,
        secret_token=SECRET,
    )
    return app.test_client(), fake_mt5, http


# ---------------------------------------------------------------------------
# Scenari
# ---------------------------------------------------------------------------

def test_valid_signal() -> None:
    print("\n[1] Segnale valido (BUY EURUSD, R:R 2.0) -> esecuzione + notifica")
    client, fake_mt5, http = build_client()
    payload = {
        "token": SECRET,
        "symbol": "EURUSD",
        "side": "buy",
        "entry": 1.0850,
        "sl": 1.0835,
        "tp2": 1.0880,   # R:R = 2.0, minimo daytrading (15 pip SL)
        "mode": "daytrading",
        "setup_type": "pro_trend",
        "balance": 10000,
    }
    resp = client.post("/webhook", json=payload)
    data = resp.get_json()

    check("HTTP 200", resp.status_code == 200, f"status={resp.status_code}")
    check("status success", data.get("status") == "success", str(data))
    check("un ordine inviato a MT5", len(fake_mt5.sent_requests) == 1,
          f"count={len(fake_mt5.sent_requests)}")
    check("magic daytrading presente",
          {r["magic"] for r in fake_mt5.sent_requests} == {1002})
    check("action = DEAL (market)",
          all(r["action"] == FakeMT5.TRADE_ACTION_DEAL for r in fake_mt5.sent_requests))
    check("type_filling valido negoziato (IOC/FOK/RETURN)",
          all(r["type_filling"] in (FakeMT5.ORDER_FILLING_IOC,
                                    FakeMT5.ORDER_FILLING_FOK,
                                    FakeMT5.ORDER_FILLING_RETURN)
              for r in fake_mt5.sent_requests))
    check("deviation propagata",
          all(r["deviation"] == 20 for r in fake_mt5.sent_requests))
    check("ticket assegnato nella risposta",
          bool(data.get("order", {}).get("ticket")), str(data))
    check("notifica Telegram inviata", len(http.calls) == 1, f"calls={len(http.calls)}")
    if http.calls:
        text = http.calls[0]["json"]["text"]
        check("notifica contiene entry/SL/TP", all(k in text for k in ("Entry", "SL", "TP1", "TP2")))


def test_invalid_rr() -> None:
    print("\n[2] R:R insufficiente (TP2 troppo vicino) -> 400 rejected")
    client, fake_mt5, http = build_client()
    payload = {
        "token": SECRET,
        "symbol": "EURUSD",
        "side": "buy",
        "entry": 1.0850,
        "sl": 1.0835,
        "tp1": 1.0855,
        "tp2": 1.0860,   # R:R = 0.67 -> sotto il minimo 2.0
        "mode": "daytrading",
        "setup_type": "pro_trend",
        "balance": 10000,
    }
    resp = client.post("/webhook", json=payload)
    data = resp.get_json()
    check("HTTP 400", resp.status_code == 400, f"status={resp.status_code}")
    check("status rejected", data.get("status") == "rejected", str(data))
    check("nessun ordine inviato", len(fake_mt5.sent_requests) == 0)
    check("nessuna notifica ordine per segnale rifiutato", len(http.calls) == 0)


def test_bad_token() -> None:
    print("\n[3] Token errato -> 401 unauthorized")
    client, fake_mt5, _ = build_client()
    payload = {
        "token": "WRONG",
        "symbol": "EURUSD", "side": "buy", "entry": 1.0850, "sl": 1.0830,
        "mode": "daytrading", "setup_type": "pro_trend", "balance": 10000,
    }
    resp = client.post("/webhook", json=payload)
    check("HTTP 401", resp.status_code == 401, f"status={resp.status_code}")
    check("nessun ordine inviato", len(fake_mt5.sent_requests) == 0)


def test_directional_incoherence() -> None:
    print("\n[4] Incoerenza direzionale (BUY con SL sopra l'entry) -> 400")
    client, _, _ = build_client()
    payload = {
        "token": SECRET,
        "symbol": "EURUSD", "side": "buy", "entry": 1.0830, "sl": 1.0850,
        "mode": "daytrading", "setup_type": "pro_trend", "balance": 10000,
    }
    resp = client.post("/webhook", json=payload)
    check("HTTP 400", resp.status_code == 400, f"status={resp.status_code}")


def test_break_even_runner() -> None:
    print("\n[5] Break-even sul runner (magic MAIN) al raggiungimento di 1:1")
    fake_mt5 = FakeMT5()
    # Runner BUY: entry 1.0850, SL 1.0830 (rischio 20 pip), prezzo 1.0870 (=+20 pip => 1:1)
    fake_mt5.positions = [
        SimpleNamespace(
            symbol="EURUSD", magic=MAGIC_MAIN, ticket=9001,
            type=FakeMT5.ORDER_TYPE_BUY,
            price_open=1.0850, sl=1.0830, tp=1.0900, price_current=1.0870,
        ),
        # Half-exit (magic 1001) NON deve essere toccato dal break-even
        SimpleNamespace(
            symbol="EURUSD", magic=1001, ticket=9000,
            type=FakeMT5.ORDER_TYPE_BUY,
            price_open=1.0850, sl=1.0830, tp=1.0870, price_current=1.0870,
        ),
    ]
    be = BreakEvenManager(mt5_module=fake_mt5)
    moved = be.secure_runners("EURUSD")
    check("solo il runner MAIN spostato a BE", moved == [9001], f"moved={moved}")
    sltp = [r for r in fake_mt5.sent_requests if r["action"] == FakeMT5.TRADE_ACTION_SLTP]
    check("modifica SLTP inviata", len(sltp) == 1, f"count={len(sltp)}")
    if sltp:
        check("SL portato a entry (break-even)", sltp[0]["sl"] == 1.0850, str(sltp[0]))


def main() -> int:
    print("=" * 62)
    print(" TEST END-TO-END SIMULATO — webhook -> validazione -> ordini -> notifica")
    print("=" * 62)
    test_valid_signal()
    test_invalid_rr()
    test_bad_token()
    test_directional_incoherence()
    test_break_even_runner()
    print("\n" + "=" * 62)
    print(f" RISULTATO: {PASSED} passati, {FAILED} falliti")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
