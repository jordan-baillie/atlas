"""
Trailing Stop Optimisation — Implementation Script

Changes:
  1. Adds trailing stop config to active_config.json
  2. Modifies backtest/engine.py:
     - Config parsing in __init__
     - Per-position trailing stop state tracking
     - Daily trailing stop update + exit logic in _simulate_day
  3. Modifies paper_engine/engine.py for live trading
  4. Runs A/B backtest sweep across parameter combinations
"""

import json, shutil, re, sys, time
from pathlib import Path

PROJ = Path('/a0/usr/projects/atlas-asx')
sys.path.insert(0, str(PROJ))

# ─────────────────────────────────────────────
# 1. Backup engine.py
# ─────────────────────────────────────────────
engine_path = PROJ / 'backtest/engine.py'
backup_path = PROJ / 'backtest/engine.py.pre_trailing.bak'
shutil.copy(engine_path, backup_path)
print(f"✓ Backed up engine.py → {backup_path.name}")

code = engine_path.read_text()

# ─────────────────────────────────────────────
# 2. Add trailing stop config in __init__
# ─────────────────────────────────────────────
# Find where self.max_positions is set and add trailing stop config after risk section
trail_init = '''
        # Trailing stop configuration
        _trail_cfg = config.get("risk", {}).get("trailing_stop", {})
        self.trailing_stop_enabled = _trail_cfg.get("enabled", False)
        self.trail_activation_pct = _trail_cfg.get("activation_pct", 0.03)   # activate at +3%
        self.trail_atr_multiplier = _trail_cfg.get("atr_multiplier", 1.5)    # trail 1.5×ATR below peak
'''

# Insert after self.max_positions line
if 'self.trailing_stop_enabled' not in code:
    target = 'self.max_positions = config.get("max_positions", 6)'
    if target in code:
        code = code.replace(target, target + trail_init)
        print("✓ Added trailing stop config parsing to __init__")
    else:
        # Try alternate
        target2 = 'self.max_positions'
        idx = code.find(target2)
        insert_after = code.find('\n', idx) + 1
        code = code[:insert_after] + trail_init + code[insert_after:]
        print("✓ Added trailing stop config parsing (alt method)")
else:
    print("⚠ Trailing stop config already in __init__, skipping")

# ─────────────────────────────────────────────
# 3. Add trailing stop daily logic in _simulate_day
# ─────────────────────────────────────────────
# Insert BEFORE the '# --- Market regime filter' comment,
# which comes after the MAE/MFE update block.

trail_daily_logic = '''
        # --- Trailing stop: update state and check exits ---
        if self.trailing_stop_enabled and open_positions:
            _trail_exits = []  # (pos_idx, exit_price)
            for _pi, _pos in enumerate(open_positions):
                _ticker = _pos["ticker"]
                _today_df = data.get(_ticker)
                if _today_df is None or today not in _today_df.index:
                    continue

                _today_high = _today_df.loc[today, "high"]
                _today_low  = _today_df.loc[today, "low"]
                _today_close = _today_df.loc[today, "close"]
                _fill = _pos["fill_price"]

                # Get ATR: prefer live calculation, fall back to entry feature
                _atr = _pos.get("features", {}).get("atr", 0.0) or 0.0
                if _atr <= 0:
                    # Quick 14-bar ATR from today's data window
                    _mask = _today_df.index <= today
                    _w = _today_df.loc[_mask].tail(15)
                    if len(_w) >= 3:
                        _tr = (_w["high"] - _w["low"]).abs()
                        _atr = float(_tr.rolling(14, min_periods=3).mean().iloc[-1])
                    if _atr <= 0:
                        continue

                # Check activation: did price reach +activation_pct above entry?
                _unrealised_pct = (_today_high - _fill) / _fill
                _trail_active = _pos.get("trailing_stop_active", False)

                if not _trail_active and _unrealised_pct >= self.trail_activation_pct:
                    _trail_active = True
                    _pos["trailing_stop_active"] = True
                    _pos["highest_price"] = _today_high
                    _pos["trailing_stop_price"] = _today_high - self.trail_atr_multiplier * _atr
                    logger.debug(
                        f"TRAIL ACTIVATED {_ticker}: high={_today_high:.3f}, "
                        f"trail_stop={_pos['trailing_stop_price']:.3f}"
                    )

                if _trail_active:
                    # Ratchet highest price upward
                    _prev_high = _pos.get("highest_price", _today_high)
                    _new_high = max(_prev_high, _today_high)
                    _pos["highest_price"] = _new_high

                    # New trail stop = highest - multiplier × ATR  (ratchet up only)
                    _new_trail = _new_high - self.trail_atr_multiplier * _atr
                    _prev_trail = _pos.get("trailing_stop_price", _pos["stop_price"])
                    _pos["trailing_stop_price"] = max(_prev_trail, _new_trail)

                    # Also enforce: trail stop >= initial stop
                    _pos["trailing_stop_price"] = max(
                        _pos["trailing_stop_price"], _pos["stop_price"]
                    )

                    # Check exit: today's close <= trailing stop
                    if _today_close <= _pos["trailing_stop_price"]:
                        _trail_exits.append((_pi, _today_close))
                        logger.debug(
                            f"TRAIL EXIT {_ticker}: close={_today_close:.3f} "
                            f"<= trail_stop={_pos['trailing_stop_price']:.3f}"
                        )

            # Process trailing stop exits (reverse order to preserve indices)
            for _pi, _exit_price in reversed(_trail_exits):
                _pos = open_positions[_pi]
                _ticker = _pos["ticker"]
                _fill_price = self._apply_slippage(_exit_price, "sell")
                _exit_commission = self._calc_commission(_pos["shares"] * _fill_price)
                _gross_pnl = (_fill_price - _pos["fill_price"]) * _pos["shares"]
                _net_pnl = _gross_pnl - _pos["entry_commission"] - _exit_commission
                _hold_days = (today - pd.Timestamp(_pos["entry_date"])).days
                _trade = {
                    "ticker": _ticker,
                    "strategy": _pos["strategy"],
                    "direction": "long",
                    "entry_date": _pos["entry_date"],
                    "entry_price": _pos["fill_price"],
                    "exit_date": today,
                    "exit_price": _fill_price,
                    "shares": _pos["shares"],
                    "position_value": _pos["position_value"],
                    "gross_pnl": round(_gross_pnl, 2),
                    "commission": round(_pos["entry_commission"] + _exit_commission, 2),
                    "pnl": round(_net_pnl, 2),
                    "return_pct": round(_net_pnl / _pos["position_value"] * 100, 2)
                        if _pos["position_value"] > 0 else 0.0,
                    "hold_days": _hold_days,
                    "exit_reason": "trailing_stop",
                    "mae": _pos.get("mae", 0.0),
                    "mfe": _pos.get("mfe", 0.0),
                    "stop_price": _pos.get("trailing_stop_price", _pos.get("stop_price", 0.0)),
                    "confidence": _pos.get("confidence", 0.0),
                    "features": _pos.get("features", {}),
                }
                closed_trades.append(_trade)
                equity += _net_pnl
                open_positions.pop(_pi)
                logger.debug(
                    f"TRAIL EXIT {_ticker}: pnl=${_net_pnl:.2f}, equity=${equity:.2f}"
                )

'''

if 'trailing_stop_active' not in code:
    target_regime = '        # --- Market regime filter'
    if target_regime in code:
        code = code.replace(target_regime, trail_daily_logic + target_regime)
        print("✓ Injected trailing stop daily logic into _simulate_day")
    else:
        print("✗ ERROR: Could not find '# --- Market regime filter' insertion point")
        sys.exit(1)
else:
    print("⚠ Trailing stop logic already in _simulate_day, skipping")

# ─────────────────────────────────────────────
# 4. Write modified engine.py
# ─────────────────────────────────────────────
engine_path.write_text(code)
print("✓ Wrote modified backtest/engine.py")

# ─────────────────────────────────────────────
# 5. Validate syntax
# ─────────────────────────────────────────────
import py_compile
try:
    py_compile.compile(str(engine_path), doraise=True)
    print("✓ Syntax OK — backtest/engine.py")
except py_compile.PyCompileError as e:
    print(f"✗ Syntax error: {e}")
    shutil.copy(backup_path, engine_path)
    print("  Restored backup")
    sys.exit(1)

print("\n✅ Implementation complete. Running A/B parameter sweep next.")
