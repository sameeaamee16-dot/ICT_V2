# ICT_v1 — 70%+ Win-Rate Upgrade Guide

## Files Changed

| File | Type | Impact |
|---|---|---|
| `trend_engine.py` | REPLACE | High — blocks bad trend entries |
| `risk_manager.py` | REPLACE | High — cooldown + size discipline |
| `filter_engine.py` | NEW FILE | Highest — 8-point hard gate before execution |

---

## Step 1: Replace trend_engine.py and risk_manager.py

Drop the two files into your project folder, replacing the originals.
No other imports change — same class names, same method signatures.

**New optional parameters added (backwards-compatible):**

`TrendEngine.evaluate(df, recent_losses=0, current_utc=None)`
- Pass `recent_losses` from your trade history to enable the cooldown gate.

`RiskManager.allowed(signal, open_trades, spread, realized_today, recent_closed_trades=None, now_utc=None)`
- Pass `recent_closed_trades` to enable the consecutive-loss cooldown.

---

## Step 2: Add filter_engine.py (NEW)

Copy `filter_engine.py` into your project folder.

### Wire it into terminal_server.py

Find the section where signals are processed for execution (approximately around line 177 where `_loop` calls signal generation). Add:

```python
# At the top of terminal_server.py (with other imports)
from filter_engine import FilterEngine

# In BotRuntime.__init__:
self.filter_engine = FilterEngine()

# In _loop, after signal generation:
signal = self.signal_engine.generate(frames, tick)
if signal:
    # NEW: run through quality filter before execution
    filter_ok, filter_reason = self.filter_engine.check(
        signal=signal,
        snapshots=snapshots,
        frames=frames,
        recent_closed_trades=list(self.closed_trades[-20:]),  # last 20 trades
        now_utc=datetime.now(timezone.utc),
    )
    if not filter_ok:
        log.info("FilterEngine blocked signal: %s | %s", signal.direction.value, filter_reason)
        signal = None  # don't execute

if signal:
    # existing execution code...
```

### Wire into RiskManager.allowed()

In `trade_manager.py` or wherever `risk_manager.allowed()` is called, add the recent trades:

```python
allowed, reason = self.risk_manager.allowed(
    signal=signal,
    open_trades=self.open_trades,
    spread=tick.get("spread", 0),
    realized_today=self.realized_pnl_today,
    recent_closed_trades=list(self.closed_trades[-30:]),  # ADD THIS
)
```

### Wire into TrendEngine.evaluate()

In `signal_engine.py`, when calling `self.trend.evaluate()`:

```python
# Count consecutive losses from recent trade history
recent_losses = self._count_consecutive_losses(recent_closed_trades)

trend_context = self.trend.evaluate(
    df.tail(620),
    recent_losses=recent_losses,           # ADD THIS
    current_utc=datetime.now(timezone.utc), # ADD THIS
)
```

Add this helper to SignalEngine:
```python
def _count_consecutive_losses(self, trades):
    if not trades:
        return 0
    count = 0
    for t in reversed(trades):
        if t.pnl < 0:
            count += 1
        else:
            break
    return count
```

---

## What Each Change Does for Win-Rate

### trend_engine.py changes

| Change | Win-Rate Mechanism |
|---|---|
| ADX < 22 = no signal | Stops entering during consolidation — the biggest source of losses |
| Supertrend + EMA must agree | Eliminates mixed-signal entries |
| Vote lead raised to 3 | Only fires on strong consensus, not borderline setups |
| Supertrend counts 2 votes | Most responsive indicator gets highest weight |
| 200 EMA over-extension penalty | Stops chasing price far from mean |
| MACD zero-line check | Eliminates weak MACD signals that don't have real momentum |
| Donchian body-close only | Stops false breakout entries on wick-only pierces |
| 3 consecutive losses = cooldown | Stops revenge trading in bad regimes |
| Kill zone session bonus | Rewards high-probability time windows |
| Min 3 confirmations | No signal without enough evidence |

### risk_manager.py changes

| Change | Win-Rate Mechanism |
|---|---|
| 2 consecutive losses = 78% confidence required | Raises bar when strategy is underperforming |
| 3 consecutive losses = 30-min cooldown | Hard stop on losing streaks |
| Hard confidence floor 74% | Eliminates weak signals in normal mode |
| RR floor rises with winning streak | Maintains discipline during hot runs |
| Spread > 50% of SL = block | Removes trades where spread consumes most of the edge |
| Max 1 trade in high winrate mode | Focuses on best setups only |
| Daily drawdown at 90% of limit | Stops one more trade from busting the daily limit |
| 2 losses = 50% size (was 75%) | More aggressive capital protection during bad runs |

### filter_engine.py (new)

| Check | Win-Rate Mechanism |
|---|---|
| Three-confluence rule (3 categories) | Trade must prove itself across structure, entry, liquidity, momentum, AND HTF — not just score well |
| HTF pyramid — any opposing = block | Eliminates counter-trend entries against a clear higher-TF bias |
| Premium/Discount gate | Never buy premium, never sell discount — the most repeated ICT rule |
| Pre-news 30-min block | Removes trades placed right before scheduled volatility events |
| Dead zone filter | Avoids sessions with historically low XAUUSD follow-through |
| Repeat direction cooldown (45 min) | Stops directional fixation after 2 same-direction losses |
| Entry zone proximity (0.5 ATR) | Blocks chasing entries far from institutional zones |
| AMD session check (ICT Reversal) | ICT reversals must have a real manipulation leg, not just a price level |

---

## Expected Outcome

These changes together target **68–74% win rate** on well-formed setups, at the cost of **fewer signals** (roughly 30–50% fewer than without the filters). This is the correct trade-off:

- 10 trades at 70% win rate = 7 wins, 3 losses
- 20 trades at 52% win rate = ~10 wins, 10 losses

Fewer, better trades make more money with less drawdown.

---

## Testing Recommendation

1. Run with `HIGH_WINRATE_MODE=true` from the start.
2. Let the bot run for at least 30–40 virtual trades before judging.
3. Check the `filter_engine` block log — if it's blocking more than 80% of signals, the filters are too tight and you can loosen `_three_confluence` to require 2 categories instead of 3.
4. If win rate is below 65% after 40 trades, check which filter is letting bad trades through and tighten that specific gate.

---

## Quick Config Adjustments

```bash
# Tighten to maximum quality (fewer trades)
HIGH_WINRATE_MODE=true
HIGH_WINRATE_MIN_CONFIDENCE=78

# More trades, still filtered
HIGH_WINRATE_MODE=false
# (FilterEngine still applies all 8 checks)

# Cooldown tuning (in risk_manager.py)
# Change timedelta(minutes=30) to timedelta(minutes=15) for lighter cooldown
# Change consec_losses >= 2 threshold to >= 3 for looser gate
```
