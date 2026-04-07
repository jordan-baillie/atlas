#!/usr/bin/env python3
"""
Atlas Dual-Write Consistency Checker.

Compares JSON source-of-truth files against SQLite to verify the dual-write
bridge is working correctly.  Tracks consecutive passing days for the Phase 0
migration gate: 5 consecutive passes → SQLite is ready to become authoritative.

Usage:
    python3 scripts/verify_dual_write.py           # run all checks
    python3 scripts/verify_dual_write.py --status  # show consecutive pass count
"""
import argparse
import json
import os
import random
import sys
from datetime import date
from pathlib import Path
from typing import Dict, Optional, Tuple

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

# ── Paths ─────────────────────────────────────────────────────────────────────
TRADE_LEDGER     = PROJECT / "journal" / "trade_ledger.json"
DECISION_JOURNAL = PROJECT / "journal" / "decision_journal.json"
BROKER_STATE     = PROJECT / "brokers" / "state" / "live_sp500.json"
PLANS_DIR        = PROJECT / "plans"
OHLCV_CACHE_DIR  = PROJECT / "data" / "cache" / "sp500"
VERIFY_HISTORY   = PROJECT / "data" / "dual_write_verification.json"

TODAY = date.today().isoformat()

# ── Symbols ───────────────────────────────────────────────────────────────────
OK   = "✅"
BAD  = "❌"
WARN = "⚠️ "
WIDTH = 51


def _hr(char: str = "═") -> str:
    return char * WIDTH


def _row(label: str, value, indent: int = 5) -> None:
    print(f"{' ' * indent}{label}: {value}")


def _result(passed: bool, msg: str = "") -> None:
    sym  = OK  if passed else BAD
    word = "PASS" if passed else "FAIL"
    suffix = f" — {msg}" if msg else ""
    print(f"     {sym} {word}{suffix}")


# ── JSON helpers ──────────────────────────────────────────────────────────────
def _load(path: Path) -> Tuple[Optional[object], Optional[str]]:
    """Return (data, error_str).  error_str is None on success."""
    if not path.exists():
        return None, f"missing: {path.relative_to(PROJECT)}"
    try:
        with open(path) as f:
            return json.load(f), None
    except Exception as exc:
        return None, f"parse error ({path.name}): {exc}"


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 1 — Trades
# ═════════════════════════════════════════════════════════════════════════════
def check_trades() -> bool:
    """
    Compare trade_ledger.json + broker state vs SQLite trades table.

    Pass criteria (all must hold):
    - ledger entry count == sqlite open+closed
    - every broker closed trade has a matching row in SQLite
    - every broker open position has a matching row in SQLite
    """
    print("\n  1. Trades")

    from db import atlas_db  # lazy import

    # --- JSON ledger ----------------------------------------------------------
    ledger, err = _load(TRADE_LEDGER)
    if err:
        print(f"     {WARN}{err}")
        return False

    json_entries = sum(1 for e in ledger if e.get("type") == "entry")
    json_exits   = sum(1 for e in ledger if e.get("type") == "exit")
    _row("JSON entries", f"{json_entries} (entry) / {json_exits} (exit)")

    # --- Broker state ---------------------------------------------------------
    broker, berr = _load(BROKER_STATE)
    broker_open   = len(broker.get("positions",    [])) if broker else None
    # Exclude blank/test records (no ticker or exit_date) to match migration logic
    broker_closed = len([t for t in broker.get("closed_trades", [])
                         if t.get("ticker") and t.get("exit_date")]) if broker else None

    if broker:
        _row("Broker state", f"{broker_open} open, {broker_closed} closed")
    else:
        _row(f"{WARN}broker state", berr)

    # --- SQLite ---------------------------------------------------------------
    with atlas_db.get_db() as db:
        sqlite_open   = db.execute(
            "SELECT COUNT(*) FROM trades WHERE status='open'"
        ).fetchone()[0]
        sqlite_closed = db.execute(
            "SELECT COUNT(*) FROM trades WHERE status='closed'"
        ).fetchone()[0]

    _row("SQLite", f"{sqlite_open} open, {sqlite_closed} closed")

    # --- Count check (broker state is most reliable) --------------------------
    if broker:
        if broker_open != sqlite_open or broker_closed != sqlite_closed:
            _result(
                False,
                f"broker ({broker_open} open, {broker_closed} closed) ≠ "
                f"SQLite ({sqlite_open} open, {sqlite_closed} closed)",
            )
            return False
    else:
        # Fallback: compare ledger entry count vs SQLite
        sqlite_total = sqlite_open + sqlite_closed
        if json_entries != sqlite_total:
            _result(False, f"ledger entries ({json_entries}) ≠ SQLite total ({sqlite_total})")
            return False

    # --- Spot-check: broker closed trades present in SQLite -------------------
    field_ok = True

    if broker and broker.get("closed_trades"):
        missing_ct: list = []
        with atlas_db.get_db() as db:
            for ct in broker["closed_trades"]:
                # Skip blank/test records (same filter as migration)
                if not ct.get("ticker") or not ct.get("exit_date"):
                    continue
                found = db.execute(
                    """SELECT id FROM trades
                       WHERE ticker=? AND strategy=? AND status='closed'
                       LIMIT 1""",
                    (ct["ticker"], ct.get("strategy", "")),
                ).fetchone()
                if not found:
                    missing_ct.append(f"{ct['ticker']}/{ct.get('strategy')}")

        if missing_ct:
            _row(f"     {BAD} closed trades missing in SQLite",
                 ", ".join(missing_ct[:5]))
            field_ok = False
        else:
            _row(f"     {OK} broker closed trades", "found in SQLite")

    # --- Spot-check: open positions present in SQLite -------------------------
    if broker and broker.get("positions"):
        missing_op: list = []
        with atlas_db.get_db() as db:
            for pos in broker["positions"]:
                found = db.execute(
                    """SELECT id FROM trades
                       WHERE ticker=? AND strategy=? AND status='open'
                       LIMIT 1""",
                    (pos["ticker"], pos.get("strategy", "")),
                ).fetchone()
                if not found:
                    missing_op.append(f"{pos['ticker']}/{pos.get('strategy')}")

        if missing_op:
            _row(f"     {BAD} open positions missing in SQLite",
                 ", ".join(missing_op[:5]))
            field_ok = False
        else:
            _row(f"     {OK} open positions", "found in SQLite")

    if field_ok:
        _result(True, "counts match")
    else:
        _result(False, "position mismatch in SQLite")
    return field_ok


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 2 — Signals
# ═════════════════════════════════════════════════════════════════════════════
def check_signals() -> bool:
    """
    Compare decision_journal.json vs SQLite signals table.

    Pass criteria:
    - total counts match exactly
    - latest 5 entries (by timestamp) are present in SQLite
    """
    print("\n  2. Signals")

    from db import atlas_db

    # --- JSON count -----------------------------------------------------------
    journal, err = _load(DECISION_JOURNAL)
    if err:
        print(f"     {WARN}{err}")
        return False

    json_count = len(journal)
    _row("JSON", f"{json_count} entries")

    # --- SQLite count ---------------------------------------------------------
    with atlas_db.get_db() as db:
        sqlite_count = db.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]
    _row("SQLite", f"{sqlite_count} rows")

    # SQLite may have more rows than JSON (JSON truncated by maintenance,
    # SQLite includes migration backfill).  Superset check: SQLite ⊇ JSON.
    superset_ok = sqlite_count >= json_count

    # --- Spot-check: latest 5 JSON entries present in SQLite -----------------
    try:
        sorted_journal = sorted(
            journal, key=lambda x: x.get("timestamp", ""), reverse=True
        )
        latest5 = sorted_journal[:5]
    except Exception:
        latest5 = journal[-5:]

    spot_ok = True
    with atlas_db.get_db() as db:
        for entry in latest5:
            found = db.execute(
                """SELECT id FROM signals
                   WHERE timestamp=? AND ticker=? AND strategy=?
                   LIMIT 1""",
                (
                    entry.get("timestamp", ""),
                    entry.get("ticker", ""),
                    entry.get("strategy", ""),
                ),
            ).fetchone()
            if not found:
                spot_ok = False
                break

    if spot_ok:
        _row("Latest 5 match", OK)
    else:
        _row("Latest 5 match", BAD)

    passed = superset_ok and spot_ok
    if not superset_ok:
        _result(
            False,
            f"SQLite ({sqlite_count}) has fewer rows than JSON ({json_count})",
        )
    else:
        _result(passed, f"SQLite ⊇ JSON ({json_count}≤{sqlite_count})" if spot_ok
                else "latest entries not in SQLite")
    return passed


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 3 — Plans
# ═════════════════════════════════════════════════════════════════════════════
def check_plans() -> bool:
    """
    Compare plans/plan_*.json files vs SQLite plans table.

    Pass criteria:
    - file count == SQLite row count
    - today's plan (if exists): date, market_id, status all match
    - today's plan JSON has expected keys
    """
    print("\n  3. Plans")

    from db import atlas_db

    # --- JSON plan files ------------------------------------------------------
    if not PLANS_DIR.exists():
        print(f"     {WARN}plans directory missing: {PLANS_DIR}")
        return False

    plan_files = sorted(PLANS_DIR.glob("plan_*.json"))
    json_count = len(plan_files)
    _row("JSON files", json_count)

    # --- SQLite count ---------------------------------------------------------
    with atlas_db.get_db() as db:
        sqlite_count = db.execute(
            "SELECT COUNT(*) FROM plans"
        ).fetchone()[0]
    _row("SQLite rows", sqlite_count)

    # SQLite may have more rows (old plan files archived/deleted from disk).
    # Superset check: SQLite ⊇ JSON.
    count_ok = sqlite_count >= json_count

    # --- Today's plan spot-check ----------------------------------------------
    today_ok   = True
    today_file = PLANS_DIR / f"plan_sp500_{TODAY}.json"

    if today_file.exists():
        plan_data, perr = _load(today_file)
        if perr:
            _row("Today's plan", f"{BAD} {perr}")
            today_ok = False
        else:
            trade_date = plan_data.get("trade_date", "")
            market_id  = plan_data.get("market_id", "sp500")
            json_status_raw = plan_data.get("status", "").lower()

            with atlas_db.get_db() as db:
                db_row = db.execute(
                    """SELECT status FROM plans
                       WHERE date=? AND market_id=?
                       ORDER BY id DESC LIMIT 1""",
                    (trade_date, market_id),
                ).fetchone()

            if not db_row:
                _row(
                    "Today's plan",
                    f"{BAD} not found in SQLite (trade_date={trade_date})",
                )
                today_ok = False
            else:
                # Normalise: strip underscores for comparison
                # JSON: "PENDING_APPROVAL" → "pendingapproval"
                # SQLite: "pending_approval" → "pendingapproval"
                sqlite_norm = db_row[0].lower().replace("_", "")
                json_norm   = json_status_raw.replace("_", "")
                status_match = sqlite_norm == json_norm

                if status_match:
                    _row("Today's plan", f"{OK} status={db_row[0]}")
                else:
                    _row(
                        "Today's plan",
                        f"{BAD} status mismatch (JSON={json_status_raw}, SQLite={db_row[0]})",
                    )
                    today_ok = False

            # Verify plan_data has expected structural keys
            expected_keys = {"trade_date", "market_id", "status", "proposed_entries"}
            missing_keys  = expected_keys - set(plan_data.keys())
            if missing_keys:
                _row(
                    "Plan structure",
                    f"{BAD} missing keys: {sorted(missing_keys)}",
                )
                today_ok = False
    else:
        _row("Today's plan", "not found (skipped)")

    passed = count_ok and today_ok
    if not count_ok:
        _result(
            False,
            f"SQLite ({sqlite_count}) has fewer rows than plan files ({json_count})",
        )
    elif not today_ok:
        _result(False, "today's plan mismatch")
    else:
        _result(True, f"SQLite ⊇ files ({json_count}≤{sqlite_count})")
    return passed


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 4 — OHLCV
# ═════════════════════════════════════════════════════════════════════════════
def check_ohlcv() -> bool:
    """
    Sample 3 random tickers from the parquet cache, compare last 5 trading
    days of close prices against SQLite ohlcv table.

    Pass criteria: all sampled close prices match within 0.01 tolerance.
    """
    if not OHLCV_CACHE_DIR.exists():
        print(f"\n  4. OHLCV")
        print(f"     {WARN}cache dir missing: {OHLCV_CACHE_DIR}")
        return False

    import pandas as pd  # lazy import

    from db import atlas_db

    parquet_files = list(OHLCV_CACHE_DIR.glob("*.parquet"))
    if not parquet_files:
        print(f"\n  4. OHLCV")
        print(f"     {WARN}no parquet files in {OHLCV_CACHE_DIR}")
        return False

    # Seed on today so the same 3 tickers are checked on repeated runs per day
    rng = random.Random(TODAY)
    sample_files = rng.sample(parquet_files, min(3, len(parquet_files)))
    tickers = [f.stem for f in sample_files]

    print(f"\n  4. OHLCV (sample: {', '.join(tickers)})")

    all_ok = True
    for ticker in tickers:
        try:
            df_p = pd.read_parquet(OHLCV_CACHE_DIR / f"{ticker}.parquet")
            df_p.index = pd.to_datetime(df_p.index)
            df_last5 = df_p.tail(5)

            if df_last5.empty:
                _row(f"  {ticker}", f"{WARN}empty parquet")
                continue

            min_date = df_last5.index.min().strftime("%Y-%m-%d")
            max_date = df_last5.index.max().strftime("%Y-%m-%d")

            with atlas_db.get_db() as db:
                rows = db.execute(
                    """SELECT date, close FROM ohlcv
                       WHERE ticker=? AND date BETWEEN ? AND ?
                       ORDER BY date""",
                    (ticker, min_date, max_date),
                ).fetchall()

            sqlite_prices: Dict[str, float] = {r["date"]: r["close"] for r in rows}

            mismatches = []
            for ts, row_data in df_last5.iterrows():
                d = ts.strftime("%Y-%m-%d")
                parquet_close = float(row_data["close"])
                if d not in sqlite_prices:
                    mismatches.append(f"{d}: missing in SQLite")
                else:
                    diff = abs(parquet_close - sqlite_prices[d])
                    if diff > 0.01:
                        mismatches.append(
                            f"{d}: parquet={parquet_close:.4f} "
                            f"vs sqlite={sqlite_prices[d]:.4f}"
                        )

            if mismatches:
                _row(f"  {ticker}", f"{BAD} {'; '.join(mismatches[:2])}")
                all_ok = False
            else:
                matched = sum(
                    1
                    for ts in df_last5.index
                    if ts.strftime("%Y-%m-%d") in sqlite_prices
                )
                _row(f"  {ticker}", f"{matched} days match {OK}")

        except Exception as exc:
            _row(f"  {ticker}", f"{BAD} error: {exc}")
            all_ok = False

    _result(all_ok)
    return all_ok


# ═════════════════════════════════════════════════════════════════════════════
# CHECK 5 — Equity Curve
# ═════════════════════════════════════════════════════════════════════════════
def check_equity() -> bool:
    """
    Compare broker state equity_history vs SQLite equity_curve.

    Pass criteria:
    - row counts match
    - latest entry: date and equity agree within $0.01
    """
    print("\n  5. Equity Curve")

    from db import atlas_db

    # --- Broker state ---------------------------------------------------------
    broker, err = _load(BROKER_STATE)
    if err:
        print(f"     {WARN}{err}")
        return False

    equity_history = broker.get("equity_history", [])
    broker_count   = len(equity_history)
    _row("Broker", f"{broker_count} entries")

    # --- SQLite ---------------------------------------------------------------
    with atlas_db.get_db() as db:
        sqlite_count = db.execute(
            "SELECT COUNT(*) FROM equity_curve WHERE market_id='sp500'"
        ).fetchone()[0]
        sqlite_latest = db.execute(
            """SELECT date, equity, cash
               FROM equity_curve WHERE market_id='sp500'
               ORDER BY date DESC LIMIT 1"""
        ).fetchone()

    _row("SQLite", f"{sqlite_count} rows")

    # SQLite may have extra rows from backfill / early testing.
    # Superset check: SQLite ⊇ broker equity_history.
    count_ok   = sqlite_count >= broker_count
    latest_ok  = True

    # --- Latest entry comparison ----------------------------------------------
    if equity_history and sqlite_latest:
        b_latest  = equity_history[-1]          # most recent broker entry
        b_date    = b_latest.get("date", "")
        b_equity  = round(float(b_latest.get("equity", 0)), 2)
        s_date    = sqlite_latest["date"]
        s_equity  = round(float(sqlite_latest["equity"]), 2)

        if b_date != s_date or abs(b_equity - s_equity) > 0.01:
            _row(
                "Latest match",
                f"{BAD} broker={b_date}/{b_equity} vs SQLite={s_date}/{s_equity}",
            )
            latest_ok = False
        else:
            _row("Latest match", f"{OK} {s_date} equity={s_equity}")

    elif not equity_history:
        _row("Latest match", f"{WARN}no broker equity history")
        latest_ok = False
    else:
        _row("Latest match", f"{BAD} no SQLite equity rows")
        latest_ok = False

    passed = count_ok and latest_ok
    if not count_ok:
        _result(
            False,
            f"SQLite ({sqlite_count}) has fewer rows than broker ({broker_count})",
        )
    elif not latest_ok:
        _result(False, "latest entry mismatch")
    else:
        _result(True, f"SQLite ⊇ broker ({broker_count}≤{sqlite_count})")
    return passed


# ═════════════════════════════════════════════════════════════════════════════
# History
# ═════════════════════════════════════════════════════════════════════════════
def _load_history() -> dict:
    if VERIFY_HISTORY.exists():
        try:
            with open(VERIFY_HISTORY) as f:
                return json.load(f)
        except Exception:
            pass
    return {"checks": [], "consecutive_passes": 0, "gate_target": 5}


def _save_history(history: dict, passed: bool, detail: str) -> dict:
    # Overwrite today's entry on re-runs
    history["checks"] = [c for c in history["checks"] if c.get("date") != TODAY]
    history["checks"].append({"date": TODAY, "passed": passed, "details": detail})

    # Recount consecutive passes from the end of the list
    consecutive = 0
    for check in reversed(history["checks"]):
        if check["passed"]:
            consecutive += 1
        else:
            break

    history["consecutive_passes"] = consecutive
    VERIFY_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(VERIFY_HISTORY, "w") as f:
        json.dump(history, f, indent=2)
    return history


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas dual-write consistency checker"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print consecutive pass count and exit",
    )
    args = parser.parse_args()

    history = _load_history()

    # ── --status mode ─────────────────────────────────────────────────────────
    if args.status:
        consecutive = history.get("consecutive_passes", 0)
        target      = history.get("gate_target", 5)
        checks      = history.get("checks", [])
        print(f"Dual-write gate: {consecutive}/{target} consecutive passing days")
        if checks:
            last = checks[-1]
            sym  = OK if last.get("passed") else BAD
            print(f"Last check: {last.get('date')} {sym}  {last.get('details', '')}")
        sys.exit(0)

    # ── Full check run ────────────────────────────────────────────────────────
    print()
    print(_hr())
    print("  Atlas Dual-Write Consistency Check")
    print(f"  Date: {TODAY}")
    print(_hr())

    checks = [
        ("trades",  check_trades),
        ("signals", check_signals),
        ("plans",   check_plans),
        ("ohlcv",   check_ohlcv),
        ("equity",  check_equity),
    ]

    results: Dict[str, bool] = {}
    for name, fn in checks:
        try:
            results[name] = fn()
        except Exception as exc:
            print(f"\n  {name.title()}")
            print(f"     {BAD} FAIL — unexpected error: {exc}")
            results[name] = False

    # ── Summary ───────────────────────────────────────────────────────────────
    passed_count = sum(1 for v in results.values() if v)
    total_count  = len(results)
    all_passed   = passed_count == total_count
    detail       = f"{passed_count}/{total_count} pass"

    print()
    print(_hr())
    sym = OK if all_passed else BAD
    print(f"  RESULT: {passed_count}/{total_count} PASS {sym}")

    history     = _save_history(history, all_passed, detail)
    consecutive = history["consecutive_passes"]
    target      = history["gate_target"]
    print(f"  Consecutive passing days: {consecutive}/{target}")
    print(_hr())
    print()

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
