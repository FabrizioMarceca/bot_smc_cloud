"""
diagnostica_mt5.py
==================
Script di diagnosi della connessione MT5. NON invia ordini: prova solo ad
agganciarsi al terminale e stampa cosa succede, per capire perche' il login
fallisce. Lanciare con:  python diagnostica_mt5.py
"""

from __future__ import annotations

from mt5_adapter import mt5

import config


def _stato_account() -> None:
    acc = mt5.account_info()
    if acc is None:
        print("  account_info() = None  -> il terminale NON ha un conto loggato.")
        return
    print(f"  Conto loggato: {acc.login} | server: {acc.server}")
    print(f"  Equity: {acc.equity} | Margine libero: {acc.margin_free}")
    print(f"  Trade consentito (terminale): {mt5.terminal_info().trade_allowed}")


def prova_attach() -> bool:
    """Modo 1: aggancio al terminale gia' aperto e loggato (senza credenziali)."""
    print("\n[MODO 1] Aggancio al terminale gia' aperto (mt5.initialize() senza credenziali)")
    ok = mt5.initialize()
    if not ok:
        print(f"  FALLITO: {mt5.last_error()}")
        print("  -> Apri MT5 e fai LOGIN a mano sul conto, poi riprova.")
        return False
    print("  OK: agganciato al terminale.")
    _stato_account()
    mt5.shutdown()
    return True


def prova_login_codice() -> bool:
    """Modo 2: login via codice con le credenziali del .env."""
    print("\n[MODO 2] Login via codice (login/password/server dal .env)")
    if not (config.MT5_LOGIN and config.MT5_PASSWORD and config.MT5_SERVER):
        print("  Saltato: credenziali incomplete nel .env.")
        return False
    print(f"  login={config.MT5_LOGIN} server={config.MT5_SERVER}")
    ok = mt5.initialize(
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER,
    )
    if not ok:
        print(f"  FALLITO: {mt5.last_error()}")
        print("  -> (-6 Authorization failed) = login/password/server errati o "
              "conto demo scaduto. Crea un nuovo demo in MT5 e aggiorna il .env.")
        return False
    print("  OK: login via codice riuscito.")
    _stato_account()
    mt5.shutdown()
    return True


if __name__ == "__main__":
    print("=" * 60)
    print(" DIAGNOSTICA CONNESSIONE MT5")
    print("=" * 60)
    print(f"MT5_ATTACH_ONLY = {config.MT5_ATTACH_ONLY}")
    if not prova_attach():
        prova_login_codice()
    print("\nFine diagnostica.")
