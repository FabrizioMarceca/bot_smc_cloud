"""
test_webhook.py
===============
Test rapido del webhook server: verifica che il formato JSON sia corretto
e che l'endpoint risponda, senza bisogno di MT5.

Uso:
    python test_webhook.py

Se run_master.py e' in esecuzione, testa l'endpoint reale.
Altrimenti verifica solo la validazione del payload.
"""

import json
import sys

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Test 1: Verifica che il payload JSON sia formattato correttamente
print("=" * 60)
print("TEST 1: Validazione formato JSON payload")
print("=" * 60)

test_payload = {
    "symbol": "XAUUSD",
    "side": "buy",
    "entry": 4050.00,
    "sl": 4030.00,
    "setup_type": "pro_trend",
    "balance": 2884.13,
    "mode": "daytrading",
}

json_str = json.dumps(test_payload)
print("  Payload JSON:")
print(f"  {json_str}")
print("  [OK] Formato JSON valido")

# Test 2: Prova a inviare al webhook locale (se attivo)
print()
print("=" * 60)
print("TEST 2: Invio webhook a localhost:5000")
print("=" * 60)

if HAS_REQUESTS:
    try:
        resp = requests.post(
            "http://localhost:5000/webhook",
            json=test_payload,
            timeout=5,
        )
        print(f"  Status: {resp.status_code}")
        print(f"  Response: {resp.text}")
        if resp.status_code == 200:
            print("  [OK] Webhook server risponde correttamente!")
        else:
            print(f"  [WARN] Server risponde ma con status {resp.status_code}")
    except requests.exceptions.ConnectionError:
        print("  [WARN] Server webhook NON in esecuzione su localhost:5000")
        print("     Avvia run_master.py prima di testare il webhook.")
    except Exception as e:
        print(f"  [ERROR] Errore: {e}")
else:
    print("  [WARN] Modulo 'requests' non installato. Salto test.")

# Test 3: Verifica health endpoint
print()
print("=" * 60)
print("TEST 3: Health check")
print("=" * 60)

if HAS_REQUESTS:
    try:
        resp = requests.get("http://localhost:5000/health", timeout=3)
        print(f"  Status: {resp.status_code}")
        print(f"  Response: {resp.json()}")
    except requests.exceptions.ConnectionError:
        print("  [WARN] Health endpoint non raggiungibile (server spento)")
    except Exception as e:
        print(f"  [ERROR] Errore: {e}")
else:
    print("  [WARN] Modulo 'requests' non installato. Salto test.")

print()
print("=" * 60)
print("RIEPILOGO")
print("=" * 60)
print()
print("Per testare il flusso completo TradingView -> MT5:")
print()
print("1. Avvia run_master.py:  python run_master.py")
print("2. Avvia ngrok:          ngrok http 5000")
print("3. Copia l'URL HTTPS da ngrok (es. https://abc123.ngrok-free.app)")
print("4. Su TradingView crea un Alert con Webhook URL = quell'URL + /webhook")
print("5. Come Message metti il JSON qui sopra")
print("6. Clicca 'Create' e poi 'Play' per testare subito")
print()
print("Il bot ricevera' il segnale con mode=daytrading e piazzera' l'ordine su MT5.")
