from __future__ import annotations

import MetaTrader5 as mt5


def main() -> None:
    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}")
        return
    symbols = mt5.symbols_get()
    if symbols is None:
        print("No symbols received from MT5.")
        mt5.shutdown()
        return
    terms = ("BTC", "BITCOIN", "XBT", "XAU", "GOLD")
    found = [s.name for s in symbols if any(term in s.name.upper() for term in terms)]
    if not found:
        print("No BTC/XAU symbols found. Make sure you are logged in and Market Watch is loaded.")
    else:
        print("Broker BTC/XAU symbols:")
        for name in found:
            print(name)
    mt5.shutdown()


if __name__ == "__main__":
    main()
