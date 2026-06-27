from __future__ import annotations

from data_feed import MT5Feed


def main() -> None:
    feed = MT5Feed()
    print("MT5 connected successfully")
    print(f"Using symbol: {feed.symbol}")
    df = feed.get_closed_candles("1m", 500)
    print(df.tail())
    tick = feed.get_tick()
    print("BID:", tick["bid"])
    print("ASK:", tick["ask"])


if __name__ == "__main__":
    main()

