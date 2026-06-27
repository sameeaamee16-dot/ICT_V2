# XAUUSD Institutional Signal Terminal

Real-time, closed-candle XAUUSD signal engine with ICT/SMC confluence, MT5 broker candles, automatic virtual trade execution, MySQL trade history, adaptive filtering, confidence calibration, and a browser terminal.

## Quick Start

```powershell
python -m pip install -r requirements.txt
$env:MYSQL_USER="root"
$env:MYSQL_PASSWORD="your_mysql_password"
python terminal_server.py
```

Open:

```text
http://127.0.0.1:8080
```

Or run:

```powershell
.\start_terminal.ps1
```

Before running, open MetaTrader 5, login to your broker, and open an XAUUSD or GOLD chart once. The bot analysis uses MT5 broker candles only.

XAUUSD is the default asset:

```powershell
$env:TRADING_SYMBOL="XAUUSD"
$env:TRADINGVIEW_SYMBOL="OANDA:XAUUSD"
$env:MT5_SYMBOL_CANDIDATES="XAUUSD,XAUUSDm,GOLD"
```

## Professional Controls

- XAUUSD-only symbol discovery and asset profile.
- Active 1m execution mode for more frequent virtual trades, with strict high-win-rate mode available through `HIGH_WINRATE_MODE=true`.
- Automatic virtual execution opens valid signals after all risk and quality filters pass.
- Manual pause switch blocks new auto entries from the terminal.
- Entry Progress visual shows how close the current market read is to a trade setup using live gate percentages.
- Minimum Activity fallback can attempt a bounded micro trade after 5 minutes without an entry, while still using spread, geometry, drawdown, duplicate, confidence, and RR guards.
- Duplicate setup protection blocks repeated same-direction entries from the same model.
- Manual UTC news blackout windows can pause new entries during CPI, FOMC, NFP, Fed speeches, or other high-impact macro events.
- Fixed micro lot sizing keeps every new trade at `0.01` lot by default.
- Bounded micro-scalp exits still use market analysis, but keep XAUUSD distances compact: SL is capped around 3-5 price points and TP around 10-15 price points, for example BUY 4000.00 -> TP near 4012.00 and SL near 3995.00.
- Trade Master tracks every setup agent and concept, then shows an upgrade percentage after losses so the strategy can tighten or promote itself from real closed-trade results.
- Confidence calibration groups closed trades by confidence bucket so confidence can be judged against actual history.
- Adaptive trainer starts learning after 3 closed trades so strategy changes show up much faster during live testing.
- Backtests model spread, slippage, and commission instead of using a perfect-cost environment.
- Signal events and rejected decisions are written to MySQL `bot_events` when MySQL is available.

Example news pause:

```powershell
$env:NEWS_BLACKOUT_UTC="2026-06-12T12:15:00Z/2026-06-12T13:15:00Z"
```

Minimum activity timing:

```powershell
$env:MINIMUM_ACTIVITY_ENABLED="true"
$env:MINIMUM_ACTIVITY_MINUTES="5"
$env:HIGH_WINRATE_MODE="false"
```

Optional economic calendar CSV blackout:

```powershell
$env:ECONOMIC_NEWS_CSV="A:\ICT Trading\news.csv"
$env:NEWS_BLACKOUT_BEFORE_MIN="20"
$env:NEWS_BLACKOUT_AFTER_MIN="20"
```

CSV format:

```text
time,title,impact
2026-06-12T12:30:00Z,US CPI,high
```

## Important

This is a professional analytics and virtual execution terminal, not financial advice and not a guaranteed profit machine. Signals are generated only from closed candles and are never backdated.
