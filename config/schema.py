"""Atlas config schema validation.

Provides:
    SCHEMA               — list of (key_path, type_spec, required, range_spec, default, desc) tuples
    _get_nested()        — navigate a nested dict by dot-path
    validate_config()    — collect ALL validation errors for a config dict
    validate_config_file() — load JSON from file and validate
    print_validation_report() — pretty-print a validation report to stdout

Schema tuple layout
-------------------
    (key_path, type_spec, required, range_spec, default, description)

    key_path   — dot-separated path, e.g. "risk.max_open_positions"
    type_spec  — Python type, or tuple of types (any match is accepted)
    required   — bool; if True and absent, an error is raised
    range_spec — None           → no restriction
                 (lo, hi)       → numeric range; either bound may be None (unbounded)
                 [v1, v2, ...]  → enum; value must be one of the listed strings
    default    — informational only (not used to fill missing values)
    description — human-readable description
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Sentinel for values that are absent from the config
# ---------------------------------------------------------------------------
_MISSING = object()


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------
SCHEMA: List[Tuple] = [
    # ---- top-level ----------------------------------------------------------
    ("version",     str,             True,  None,                               None,        "Config version string (e.g. v3.0)"),
    ("market",      str,             True,  ["sp500", "asx"],                   None,        "Target market identifier"),
    ("description", str,             False, None,                               None,        "Human-readable config description"),

    # ---- universe -----------------------------------------------------------
    ("universe.benchmark_ticker",          str,          False, None,                          "SPY",       "Benchmark ticker symbol"),
    ("universe.method",                    str,          False, ["top_liquid", "manual"],       "top_liquid","Universe selection method"),
    ("universe.top_n",                     int,          False, (1, 2000),                      200,         "Number of top liquid stocks to include"),
    ("universe.min_price",                 (int, float), False, (0, None),                      5.0,         "Minimum stock price filter"),
    ("universe.min_market_cap",            (int, float), False, (0, None),                      2_000_000_000, "Minimum market cap in USD"),
    ("universe.min_median_daily_value",    (int, float), False, (0, None),                      5_000_000,   "Minimum median daily traded value in USD"),

    # ---- risk ---------------------------------------------------------------
    ("risk.starting_equity",           (int, float), True,  (0, None),  None,   "Starting equity in local currency"),
    ("risk.max_risk_per_trade_pct",    (int, float), True,  (0.0, 1.0), None,   "Max fraction of equity at risk per trade"),
    ("risk.max_open_positions",        int,          True,  (1, 50),    None,   "Maximum concurrent open positions"),
    ("risk.max_sector_concentration",  int,          False, (1, 50),    2,      "Max positions allowed per sector"),
    ("risk.max_daily_drawdown_pct",    (int, float), False, (0.0, 1.0), 0.02,   "Max daily drawdown as fraction of equity"),
    ("risk.require_stop_loss",         bool,         False, None,       True,   "Every trade must have a stop-loss price"),
    ("risk.require_planned_exit",      bool,         False, None,       True,   "Every trade must have a planned exit target"),
    ("risk.min_confidence",            (int, float), False, (0.0, 1.0), 0.65,   "Minimum signal confidence threshold"),

    # risk.trailing_stop sub-section
    ("risk.trailing_stop.enabled",        bool,         False, None,     False, "Enable trailing stop"),
    ("risk.trailing_stop.activation_pct", (int, float), False, (0, 1),   0.02,  "Price gain required to activate the trailing stop"),
    ("risk.trailing_stop.atr_multiplier", (int, float), False, (0, 20),  1.5,   "ATR multiplier controlling trailing-stop distance"),

    # ---- trading ------------------------------------------------------------
    ("trading.mode",             str,  True,  ["live", "paper", "passive", "backtest"], None,            "Trading execution mode"),
    ("trading.broker",           str,  True,  ["alpaca", "moomoo", "none"],             None,            "Broker to use for order execution"),
    ("trading.live_enabled",     bool, False, None,                                     False,           "Whether live order submission is enabled"),
    ("trading.approval_required",bool, False, None,                                     False,           "Whether human approval is required before execution"),
    ("trading.holding_period_min",int, False, (1, 365),                                 2,               "Minimum days to hold a position"),
    ("trading.holding_period_max",int, False, (1, 730),                                 20,              "Maximum days to hold a position"),
    ("trading.data_frequency",   str,  False, ["daily", "1H", "15Min"],                 "daily",         "Market data frequency"),
    ("trading.order_type",       str,  False, ["market_on_open", "market", "limit"],    "market_on_open","Default order type"),

    # trading.live_safety sub-section
    ("trading.live_safety.max_order_value",  (int, float), False, (0, None), 5000, "Maximum value of a single order in local currency"),
    ("trading.live_safety.max_daily_orders", int,          False, (0, 1000), 10,   "Maximum number of orders per trading day"),

    # ---- fees ---------------------------------------------------------------
    ("fees.commission_per_trade", (int, float), False, (0, None),  0,       "Fixed commission charged per trade"),
    ("fees.commission_pct",       (int, float), False, (0.0, 1.0), 0,       "Commission as a fraction of trade value"),
    ("fees.slippage_pct",         (int, float), False, (0.0, 0.1), 0.0005,  "Expected slippage as a fraction of trade value"),
    ("fees.min_position_value",   (int, float), False, (0, None),  100.0,   "Minimum position value in local currency"),
    ("fees.flat_fee_threshold",   (int, float), False, (0, None),  0,       "Trade value below which a flat fee applies"),

    # ---- data ---------------------------------------------------------------
    ("data.source",           str,  True,  ["yfinance", "alpaca"],  None,            "Primary market data source"),
    ("data.history_years",    int,  True,  (1, 30),                 7,               "Years of price history to fetch"),
    ("data.cache_dir",        str,  False, None,                    "data/cache",    "Directory for cached data files"),
    ("data.raw_dir",          str,  False, None,                    "data/raw",      "Directory for raw downloaded data"),
    ("data.processed_dir",    str,  False, None,                    "data/processed","Directory for processed data"),
    ("data.fallback_enabled", bool, False, None,                    True,            "Whether to fall back to secondary data source"),

    # ---- backtest -----------------------------------------------------------
    ("backtest.train_window_days", int, False, (30, 2000), 252, "In-sample training window in days for walk-forward"),
    ("backtest.test_window_days",  int, False, (10, 500),  63,  "Out-of-sample test window in days for walk-forward"),
    ("backtest.step_days",         int, False, (1, 365),   21,  "Walk-forward step size in days"),
    ("backtest.min_history_days",  int, False, (10, 1000), 60,  "Minimum price history required before backtesting"),

    # ---- allocation ---------------------------------------------------------
    ("allocation.enabled",          bool, False, None,                       False,      "Whether strategy allocation pools are active"),
    ("allocation.mode",             str,  False, ["soft_pool", "hard_pool"], "soft_pool","Allocation pool enforcement mode"),
    ("allocation.overflow_enabled", bool, False, None,                       True,       "Whether positions can overflow their pool cap"),

    # ---- portfolio_optimizer ------------------------------------------------
    ("portfolio_optimizer.method",            str,          False, ["mean_variance", "equal_weight", "risk_parity"], "mean_variance", "Strategy weight optimisation method"),
    ("portfolio_optimizer.min_weight",        (int, float), False, (0.0, 1.0), 0.05, "Minimum allocation weight per strategy"),
    ("portfolio_optimizer.max_weight",        (int, float), False, (0.0, 1.0), 0.40, "Maximum allocation weight per strategy"),
    ("portfolio_optimizer.cluster_threshold", (int, float), False, (0.0, 1.0), 0.7,  "Correlation threshold for strategy clustering"),

    # ---- dynamic_sizing -----------------------------------------------------
    ("dynamic_sizing.enabled",       bool,         False, None,        False,  "Whether dynamic position sizing is active"),
    ("dynamic_sizing.base_risk_pct", (int, float), False, (0.0, 1.0),  0.0035, "Baseline risk fraction when no scaling applies"),
    ("dynamic_sizing.min_risk_pct",  (int, float), False, (0.0, 1.0),  0.002,  "Minimum risk fraction after downscaling"),
    ("dynamic_sizing.max_risk_pct",  (int, float), False, (0.0, 1.0),  0.005,  "Maximum risk fraction after upscaling"),

    # ---- annealing ----------------------------------------------------------
    ("annealing.max_changes_per_cycle",      int,          False, (1, 100),   2,    "Max config changes accepted per annealing cycle"),
    ("annealing.min_oos_sharpe_improvement", (int, float), False, (0.0, 10.0), 0.05, "Minimum OOS Sharpe improvement to accept a change"),
    ("annealing.max_drawdown_increase_pct",  (int, float), False, (0.0, 1.0), 0.01, "Max drawdown increase fraction allowed per change"),

    # ---- volatility_gate ----------------------------------------------------
    ("volatility_gate.enabled", bool, False, None, False, "Whether pre-market volatility gate is active"),

    # ---- event_calendar -----------------------------------------------------
    ("event_calendar.enabled",      bool, False, None, False, "Whether economic-event calendar checking is enabled"),
    ("event_calendar.warn_in_plan", bool, False, None, True,  "Whether to warn about upcoming events in the trading plan"),
    ("event_calendar.block_entries",bool, False, None, False, "Whether to block new entries around high-impact events"),

    # ---- macro_regime -------------------------------------------------------
    ("macro_regime.enabled", bool, False, None,                          False,    "Whether macro regime filter is active"),
    ("macro_regime.mode",    str,  False, ["sizing", "gate", "boost"],   "sizing", "How macro regime score influences trading"),

    # ---- intraday -----------------------------------------------------------
    ("intraday.enabled", bool, False, None, False, "Whether intraday entry refinement is enabled"),

    # ---- fee_aware_filter ---------------------------------------------------
    ("fee_aware_filter.enabled", bool, False, None, False, "Whether fee-aware signal filtering is active"),

    # ---- regime_filter ------------------------------------------------------
    ("regime_filter.enabled", bool, False, None, False, "Whether market regime filter is active"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _type_name(type_spec) -> str:
    """Return a human-readable name for a type specification."""
    if isinstance(type_spec, tuple):
        return " | ".join(t.__name__ for t in type_spec)
    return type_spec.__name__


def _check_single_type(value: Any, t: type) -> bool:
    """Check *value* against a single type with bool/int disambiguation.

    Python's ``bool`` is a subclass of ``int``, so without special handling
    ``isinstance(True, int)`` returns True.  We explicitly reject booleans
    when the expected type is ``int`` or ``float``.
    """
    if t is bool:
        return isinstance(value, bool)
    if t is int:
        # reject bool values — they look like ints in Python
        return isinstance(value, int) and not isinstance(value, bool)
    if t is float:
        # accept int OR float, but never bool
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, t)


def _check_type(value: Any, type_spec) -> bool:
    """Return True if *value* satisfies *type_spec* (single type or tuple)."""
    if isinstance(type_spec, tuple):
        return any(_check_single_type(value, t) for t in type_spec)
    return _check_single_type(value, type_spec)


def _get_nested(config: dict, dotpath: str) -> Any:
    """Navigate a nested dict using a dot-separated key path.

    Returns the value at the path, or *_MISSING* if any intermediate key
    or the final key is absent.

    Examples::

        _get_nested({"risk": {"max_open_positions": 10}}, "risk.max_open_positions")
        # → 10

        _get_nested({}, "risk.max_open_positions")
        # → _MISSING
    """
    parts = dotpath.split(".")
    current: Any = config
    for part in parts:
        if not isinstance(current, dict):
            return _MISSING
        current = current.get(part, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------

def validate_config(config: dict) -> List[str]:
    """Validate *config* dict against SCHEMA.

    ALL errors are collected before returning — the function never
    short-circuits on the first error.

    Returns a (possibly empty) list of human-readable error strings.
    Error prefixes:
        [MISSING] — required key is absent
        [TYPE]    — value has the wrong Python type
        [RANGE]   — numeric value is outside the allowed range
        [ENUM]    — string value is not one of the allowed choices
    """
    errors: List[str] = []

    for key_path, type_spec, required, range_spec, _default, desc in SCHEMA:
        value = _get_nested(config, key_path)

        # ── presence check ────────────────────────────────────────────────
        if value is _MISSING:
            if required:
                errors.append(
                    f"[MISSING] {key_path}: required field is absent — {desc}"
                )
            continue  # nothing else to check for an absent optional field

        # ── type check ───────────────────────────────────────────────────
        if not _check_type(value, type_spec):
            errors.append(
                f"[TYPE] {key_path}: expected {_type_name(type_spec)}, "
                f"got {type(value).__name__} (value={value!r})"
            )
            continue  # range/enum check is meaningless with a wrong type

        # ── range / enum check ───────────────────────────────────────────
        if range_spec is not None:
            if isinstance(range_spec, list):
                # enum
                if value not in range_spec:
                    errors.append(
                        f"[ENUM] {key_path}: {value!r} is not one of {range_spec}"
                    )
            elif isinstance(range_spec, tuple) and len(range_spec) == 2:
                lo, hi = range_spec
                if lo is not None and value < lo:
                    errors.append(
                        f"[RANGE] {key_path}: {value!r} is below minimum {lo}"
                    )
                if hi is not None and value > hi:
                    errors.append(
                        f"[RANGE] {key_path}: {value!r} is above maximum {hi}"
                    )

    return errors


# ---------------------------------------------------------------------------
# File-based entry point
# ---------------------------------------------------------------------------

def validate_config_file(path: Union[str, Path]) -> List[str]:
    """Load a JSON config from *path* and run :func:`validate_config`.

    Returns a list of error strings (empty means PASS).
    File/parse errors are returned as a single-element list.
    """
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        return [f"[FILE] {path}: file not found"]
    except json.JSONDecodeError as exc:
        return [f"[JSON] {path}: invalid JSON — {exc}"]

    if not isinstance(config, dict):
        return [
            f"[STRUCTURE] {path}: top-level must be a JSON object, "
            f"got {type(config).__name__}"
        ]

    return validate_config(config)


# ---------------------------------------------------------------------------
# Human-readable reporting
# ---------------------------------------------------------------------------

def print_validation_report(config_or_path: Union[dict, str, Path]) -> None:
    """Print a formatted validation report to stdout.

    Accepts either a pre-loaded config dict or a path (str / Path) to a
    JSON file.
    """
    if isinstance(config_or_path, dict):
        errors = validate_config(config_or_path)
        label = "<dict>"
    else:
        path = Path(config_or_path)
        errors = validate_config_file(path)
        label = str(path)

    sep = "=" * 62
    print(f"\n{sep}")
    print(f"Config Validation Report: {label}")
    print(sep)
    if not errors:
        print("✅  PASS — no errors found")
    else:
        print(f"❌  FAIL — {len(errors)} error(s) found:")
        for i, err in enumerate(errors, 1):
            print(f"  {i:3d}. {err}")
    print()


# ---------------------------------------------------------------------------
# CLI convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m config.schema <path/to/config.json>", file=sys.stderr)
        sys.exit(1)

    errs = validate_config_file(sys.argv[1])
    print_validation_report(sys.argv[1])
    sys.exit(0 if not errs else 1)
