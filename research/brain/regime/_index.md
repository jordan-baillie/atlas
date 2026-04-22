# Market Regime

> 3 entries.

| Name | Summary |
|------|---------|
| [equity_scaling](equity_scaling.md) | How edge scales with starting capital (Task #89, 2026-03-12). |
| [per_regime_performance](per_regime_performance.md) | Bull/neutral/bear strategy breakdown (Task #84, 2026-03-12). |
| [regime_backtest_harness](run_gate_backtest) | Gate #208 evaluation harness — un-archived 2026-04-22. |

## Canonical regime-aware backtest harness
- Location: regime/run_gate_backtest.py (un-archived 2026-04-22)
- Purpose: Gate #208 evaluation harness — regime_backtest.py vs SP500 baseline
- Last validated: 2026-04-02 (Gate 4/4 PASSED: Sharpe 1.0184, DD -7.86%)
- Usage: python3 regime/run_gate_backtest.py
