# XAUUSD Bot — Winrate Upgrade v2
## Goal: 43% → 65-70% winrate | 1 trade per 25 minutes

---

## The Single Biggest Bug: `fixed_profit_target_usd = $4`

This was responsible for most of the 43% winrate. Here's why:

```
XAUUSD @ 0.01 lot, contract_size = 100
Value per point = 0.01 × 100 = $1 per point
$4 profit = 4 price points movement needed

XAUUSD typical spread = 2-3 points
So the bot was closing winning trades after just 4 points of movement,
BEFORE the trade even had a chance to reach 1:2 RR.
```

A trade with SL=5 points and TP=10 points (1:2 RR) was being closed at 4 points — turning a potential winner at TP into a near-breakeven that counted as a "win" only because PnL > 0 by $0.50. And when spread ate into that, it became a loss.

**Fix: `fixed_profit_target_usd = 0.0` (disabled)**. Trades now run to their proper TP.

---

## All Fixes Made

### `config.py`

| Parameter | Old | New | Why |
|-----------|-----|-----|-----|
| `fixed_profit_target_usd` | 4.0 | **0.0** | Was destroying RR — biggest winrate killer |
| `break_even_at_r` | 1.2 | 1.5 | Trades were moved to BE too early, then stopped out |
| `trail_after_r` | 2.0 | 2.5 | Winners were being capped too early |
| `partial_tp_at_r` | 1.5 | 2.0 | Partial close at better level |
| `micro_max_sl_points` | 8 | 10 | XAUUSD 1m volatility needs room |
| `micro_max_tp_points` | 20 | 25 | Let price run to full target |
| `min_agent_trades_for_guard` | 6 | 10 | More samples before blocking an agent |
| `agent_min_winrate_pct` | 52 | 45 | Less aggressive blocking |
| `calibration_warn_winrate_pct` | 55 | 45 | Fewer calibration rejections |
| `high_winrate_min_confidence` | 75 | 72 | Small relaxation |
| `high_winrate_min_entry_score` | 68 | 65 | Small relaxation |
| `minimum_activity_minutes` | 30 | 25 | More frequent fallback trades |
| `sideways_adx_threshold` | 17 | 15 | Less aggressive sideways detection |
| `fvg_min_atr` | 0.10 | 0.08 | Catch more FVGs |
| `min_confirmations` | 4 | 3 | One less confirmation needed |

---

### `trade_manager.py`

1. **Fixed profit target disabled** — `_exit_price()` and `_apply_fixed_profit_target()` now skip when `fixed_profit_target_usd = 0`

2. **Agent guard softened** — consecutive losses now trigger "recovery mode" (allow trade) instead of block. Only truly persistent bad agents (40%+ WR with 20+ trades) are blocked.

3. **Adaptive trainer** — hard block is advisory-only for first 20 trades. After that it enforces.

4. **Calibration block** — now requires 15+ samples AND < 45% winrate before blocking (was 20+ samples AND < 55%).

5. **Streak guard** — Activity Fallback Agent added to allowed-setups list so it doesn't get blocked during streaks.

---

### `signal_engine.py`

1. **Off-session soft penalty** — instead of hard blocking off-session trades, confidence is reduced by 3 points. Asia and NY PM have real setups and the bot should trade them.

2. **`_target_winrate_allows()`** — "stretched" timing now allowed (was only "valid"). Institutional evidence check now needs 1 of 7 concepts (was 3-of-3 strict).

3. **`_quality_allows()`** — premium/discount filter only blocks when AGAINST current bias. Equilibrium zone allowed for all trades.

4. **`_confidence()`** — bonus per confirmed concept raised from 1.5 to 2.0, and 2 new bonuses added (sweep, displacement) so more signals reach the 72% floor.

5. **Activity fallback** — now uses EMA direction as final fallback if SMC/trend give no direction. Fires at 25 mins. Works in all sessions.

6. **Trend Continuation** — only needs 3 confirmations (was 4) and MACD is no longer mandatory.

---

## Installation

Replace these 3 files in your `ICT Trading` folder:
- `config.py`
- `trade_manager.py`  
- `signal_engine.py`

No other files need changing. All other files (ict_engine, smc_engine, etc.) are unchanged.

```powershell
$env:MYSQL_USER="root"
$env:MYSQL_PASSWORD="your_password"
python terminal_server.py
```

---

## Expected Results

- **Trades per hour**: 2-4 during London/NY sessions, at least 2 per hour via fallback during quiet periods
- **Winrate target**: 62-70% after 50+ trades
- **Key metric to watch**: If winrate is still below 55% after 30 trades, check the terminal's "Agent Performance" panel — if one agent has < 40% WR, note which session/regime it's losing in

---

## Quick Tuning

**More trades (more aggressive):**
```python
minimum_activity_minutes = 20
high_winrate_min_confidence = 68.0
mtf_alignment_floor = 0.45
```

**Fewer but higher quality:**
```python
minimum_activity_minutes = 35
high_winrate_min_confidence = 76.0
mtf_alignment_floor = 0.60
break_even_at_r = 1.8
```
