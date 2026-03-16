# Cookbook: Common Failures & Fixes

Reference for the 10 most common strategy implementation failures.

---

### 1. `stop_price >= entry_price`

**Error:** `ValueError: Stop price (185.00) must be below entry price (185.00) for long positions`

**Cause:** Stop calculated from current price using ATR, but ATR is huge or price is very low.

**Fix:**
```python
stop_price = entry_price - self.atr_stop_mult * current_atr
if stop_price <= 0 or stop_price >= entry_price:
    self._logger.debug(f'{ticker}: invalid stop {stop_price:.2f}, skipping')
    continue
```

---

### 2. Zero signals generated

**Symptom:** `quick_check()` returns `{"alive": False, "signal_count": 0, "reason": "No signals generated"}`

**Common causes and fixes:**

| Cause | Fix |
|---|---|
| Too many position guards hit | Pass `existing_positions=[]` in screening mode |
| `min_rows` guard too strict | Lower to `max(period, 50) + 5` for screening |
| Signal condition is logically impossible | Print intermediate values; check threshold direction |
| Data has NaN in indicators | Add `if pd.isna(value): continue` |

```python
# Debug: log intermediate values
self._logger.debug(f'{ticker}: rsi={current_rsi:.1f} zscore={current_zscore:.2f}')
```

---

### 3. Strategy not found in registry

**Error:** `strategy_name='donchian_breakout' not found in STRATEGY_REGISTRY or sandbox`

**Cause:** File exists but has the wrong class name or is in the wrong directory.

**Fix:** Sandbox strategies must be in `research/strategies/`, not `strategies/`.
```bash
ls research/strategies/donchian_breakout.py   # ✅ correct location
ls strategies/donchian_breakout.py             # ❌ only for promoted strategies
```

---

### 4. `param_sweep` validation fails

**Error:** `param_sweep requires params_override.sweep_param (str) (found 'sweep_params' — use singular)`

**Fix:** Use exact keys `"sweep_param"` (str) and `"sweep_values"` (list):
```python
# ❌ wrong
params_override = {"sweep_params": ["rsi_period"], "values": [5, 7, 14]}

# ✅ correct
params_override = {"sweep_param": "rsi_period", "sweep_values": [5, 7, 14]}
```

---

### 5. Trades collapse (< 10)

**Symptom:** `DISCARD: Trades collapsed: 3 < 21 (70% of 30)`

**Cause:** A filter is too strict, or the entry condition is rarely true.

**Fix:** Loosen the filter condition or check if the strategy has poor signal frequency on the market:
```python
# Typical min trade guard
if e_trades < max(10, int(b_trades * 0.7)):
    # Discard — filter too aggressive
```

---

### 6. `full_optimization` missing `param_grid`

**Error:** `full_optimization with strategy_name requires params_override.param_grid`

**Fix:**
```python
# ❌ wrong
params_override = {"optimize_params": {...}}

# ✅ correct
params_override = {"param_grid": {"rsi_period": [5, 7, 14], "atr_stop": [1.5, 2.0, 2.5]}}
```

---

### 7. NaN in indicators causes silent skip

**Symptom:** Strategy is alive but generates fewer signals than expected.

**Fix:** Always guard against NaN after indicator computation:
```python
atr_val = atr.iloc[-1]
if pd.isna(atr_val) or atr_val <= 0:
    self._logger.debug(f'{ticker}: invalid ATR, skipping')
    continue
```

---

### 8. Drawdown explosion from no stop loss

**Symptom:** `DISCARD: Drawdown exploded: 45.2% > 30.0%`

**Fix:** All strategies must set `stop_price` below entry. Minimum ATR-based stop:
```python
stop_price = entry_price - 2.0 * current_atr   # 2 ATR below entry
```

---

### 9. Look-ahead bias in exit check

**Symptom:** Unrealistically high Sharpe (> 3.0) in backtest, collapses in OOS.

**Cause:** Exit check uses `df.iloc[-1]` (today) instead of `df.iloc[-2]` (yesterday's confirmed close).

**Fix:** Use yesterday's data for exit decisions on today's open:
```python
# ✅ Use yesterday's close to decide today's exit
prev_close = close.iloc[-2]     # confirmed yesterday
current_close = close.iloc[-1]  # today — use for price only, not decisions
```

---

### 10. Strategy generates signals but never exits

**Symptom:** All positions hit `time_exit` with negative P&L, no strategy-specific exits trigger.

**Cause:** Exit condition check is wrong — e.g. comparing wrong column, off-by-one on mean.

**Fix:** Add a debug exit log:
```python
self._logger.debug(
    f'{ticker}: close={current_price:.2f} mean20={mean_20:.2f} stop={stop_price:.2f}'
)
```
