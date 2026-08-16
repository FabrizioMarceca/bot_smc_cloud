from mt5_adapter import mt5

def connect_mt5():
    # Chiamando initialize() senza parametri, Python si aggancia al MT5 già aperto
    if not mt5.initialize():
        print("Inizializzazione MT5 fallita. Errore:", mt5.last_error())
        return False
    
    print("Connessione a MetaTrader 5 stabilita con successo!")
    
    # Interroghiamo il terminale per leggere lo stato del conto
    account_info = mt5.account_info()
    if account_info is not None:
        print(f"Server connesso: {account_info.server}")
        print(f"Equity: {account_info.equity}")
        print(f"Margine Libero: {account_info.margin_free}")
    else:
        print("Impossibile recuperare i dati del conto. Assicurati di aver fatto il login sull'interfaccia di MT5.")
        
    return True

if __name__ == "__main__":
    if connect_mt5():
        mt5.shutdown()