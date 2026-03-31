"""
Atlas v2.0 — One-time JSON → SQLite migration script.

Reads all legacy JSON / parquet data sources and populates data/atlas.db.
Safe to re-run (idempotent via INSERT OR REPLACE / INSERT OR IGNORE).

Usage:
    python3 db/migrate.py
    python3 -c "from db.migrate import run_migration; run_migration()"
"""

import json
import os
import sys
import glob
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── resolve project root so imports work from any cwd ────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db.atlas_db import init_db, get_db  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path, label: str):
    """Load a JSON file, returning (data, error_string)."""
    if not path.exists():
        print(f"  ⚠️  {label}: file not found — {path} (skipping)")
        return None, f"not found: {path}"
    try:
        with open(path) as f:
            return json.load(f), None
    except Exception as e:
        print(f"  ❌ {label}: failed to load — {e}")
        return None, str(e)


def _section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── 1. Trades (broker state closed + open, supplemented by ledger) ────────────

def migrate_trades() -> Tuple[int, int]:
    """Migrate trades from broker state + ledger. Returns (open_count, closed_count)."""
    _section("1/8  trades  ←  brokers/state/live_sp500.json + trade_ledger.json")

    with get_db() as db:
        db.execute("DELETE FROM trades")

    broker_path = PROJECT_ROOT / "brokers" / "state" / "live_sp500.json"
    ledger_path = PROJECT_ROOT / "journal" / "trade_ledger.json"

    broker_state, err = _load_json(broker_path, "live_sp500.json")
    if broker_state is None:
        return 0, 0

    ledger_data, _ = _load_json(ledger_path, "trade_ledger.json")

    # Build order_id → timestamp lookup from ledger entries
    ledger_entry_map: Dict[str, dict] = {}   # ticker:strategy → ledger entry
    ledger_exit_map: Dict[str, dict] = {}    # order_id → ledger exit
    if ledger_data:
        for rec in ledger_data:
            key = f"{rec.get('ticker')}:{rec.get('strategy')}"
            if rec.get("type") == "entry":
                ledger_entry_map[key] = rec
            elif rec.get("type") == "exit":
                oid = rec.get("order_id", "")
                if oid:
                    ledger_exit_map[oid] = rec

    open_count = 0
    closed_count = 0
    skipped = 0

    # ── closed trades ────────────────────────────────────────────────────────
    seen_keys = set()  # deduplicate (ticker, strategy, exit_date)
    for ct in broker_state.get("closed_trades", []):
        ticker = ct.get("ticker", "")
        strategy = ct.get("strategy", "")
        exit_date = ct.get("exit_date") or ""
        exit_price = ct.get("exit_price")
        entry_price = ct.get("entry_price")
        shares = ct.get("shares", 0)
        pnl = ct.get("pnl")
        pnl_pct = ct.get("pnl_pct")
        hold_days = ct.get("holding_days")
        exit_reason = ct.get("exit_reason", "")
        order_id = ct.get("order_id", "")

        # Skip test / blank records
        if not ticker or not exit_date:
            skipped += 1
            continue

        # Deduplication key
        dedup = f"{ticker}:{strategy}:{exit_date}:{entry_price}"
        if dedup in seen_keys:
            skipped += 1
            continue
        seen_keys.add(dedup)

        # Derive entry_date
        entry_date = ct.get("entry_date")
        if not entry_date and hold_days is not None and hold_days >= 0 and exit_date:
            try:
                from datetime import date, timedelta
                ed = date.fromisoformat(exit_date)
                entry_date = str(ed - timedelta(days=int(hold_days)))
            except Exception:
                entry_date = None
        if not entry_date:
            # Fall back to ledger
            lk = f"{ticker}:{strategy}"
            if lk in ledger_entry_map:
                ts = ledger_entry_map[lk].get("timestamp", "")
                entry_date = ts[:10] if ts else None

        # Normalise hold_days
        if (hold_days is None or hold_days < 0) and entry_date and exit_date:
            try:
                from datetime import date
                hd = (date.fromisoformat(exit_date) - date.fromisoformat(entry_date)).days
                hold_days = max(hd, 0)
            except Exception:
                hold_days = None

        # Compute pnl_pct if missing
        if pnl_pct is None and entry_price and entry_price != 0 and exit_price is not None:
            pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4)

        with get_db() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO trades
                    (ticker, strategy, universe, direction, entry_date, entry_price,
                     shares, stop_price, exit_date, exit_price, exit_reason,
                     pnl, pnl_pct, hold_days, status, config_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?)
                """,
                (
                    ticker, strategy, "sp500", "long",
                    entry_date or exit_date,
                    entry_price, shares, None,
                    exit_date, exit_price, exit_reason,
                    pnl, pnl_pct, hold_days,
                    "v3.0",
                ),
            )
        closed_count += 1

    # ── open positions ───────────────────────────────────────────────────────
    for pos in broker_state.get("positions", []):
        ticker = pos.get("ticker", "")
        strategy = pos.get("strategy", "")
        entry_date = pos.get("entry_date", "")
        entry_price = pos.get("entry_price")
        shares = pos.get("shares", 0)
        stop_price = pos.get("stop_price")

        if not ticker:
            skipped += 1
            continue

        with get_db() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO trades
                    (ticker, strategy, universe, direction, entry_date, entry_price,
                     shares, stop_price, status, config_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    ticker, strategy, "sp500", "long",
                    entry_date, entry_price, shares, stop_price, "v3.0",
                ),
            )
        open_count += 1

    print(f"  ✅  open={open_count}  closed={closed_count}  skipped={skipped}")
    return open_count, closed_count


# ── 2. Signals (decision journal) ─────────────────────────────────────────────

def migrate_signals() -> int:
    _section("2/8  signals  ←  journal/decision_journal.json")

    with get_db() as db:
        db.execute("DELETE FROM signals")

    path = PROJECT_ROOT / "journal" / "decision_journal.json"
    data, err = _load_json(path, "decision_journal.json")
    if data is None:
        return 0

    rows = []
    for rec in data:
        features = rec.get("features")
        rows.append((
            rec.get("timestamp"),
            rec.get("ticker"),
            rec.get("strategy"),
            rec.get("market_id", "sp500"),   # universe
            rec.get("direction", "long"),
            rec.get("entry_price"),
            rec.get("stop_price"),
            rec.get("take_profit"),
            rec.get("position_size"),
            rec.get("position_value"),
            rec.get("risk_amount"),
            rec.get("confidence"),
            rec.get("rationale"),
            json.dumps(features) if features is not None else None,
            rec.get("sector"),
            None,                             # regime_state — not in old format
            rec.get("action"),
            rec.get("action_reason"),
            rec.get("config_version"),
            rec.get("market_id"),
        ))

    with get_db() as db:
        db.executemany(
            """
            INSERT OR IGNORE INTO signals
                (timestamp, ticker, strategy, universe, direction,
                 entry_price, stop_price, take_profit, position_size, position_value,
                 risk_amount, confidence, rationale, features, sector, regime_state,
                 action, action_reason, config_version, market_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )

    print(f"  ✅  {len(rows)} signals migrated")
    return len(rows)


# ── 3. Research experiments (research/journal.json) ───────────────────────────

def migrate_research_experiments() -> int:
    _section("3/8  research_experiments  ←  research/journal.json")

    with get_db() as db:
        db.execute("DELETE FROM research_experiments")

    path = PROJECT_ROOT / "research" / "journal.json"
    data, err = _load_json(path, "research/journal.json")
    if data is None:
        return 0

    # verdict → status mapping
    verdict_map = {
        "pass": "kept",
        "fail": "discarded",
        "error": "error",
        "kept": "kept",
        "discarded": "discarded",
    }

    rows = []
    for rec in data:
        km = rec.get("key_metrics") or {}
        verdict = rec.get("verdict", "")
        status = verdict_map.get(verdict, verdict or "discarded")

        rows.append((
            rec.get("experiment_id"),
            rec.get("strategy"),
            rec.get("market", "sp500"),       # universe
            rec.get("category"),               # experiment_type
            None,                              # params_changed — not stored
            rec.get("hypothesis"),             # description
            km.get("sharpe"),
            km.get("total_trades"),
            km.get("max_drawdown_pct"),
            km.get("profit_factor"),
            km.get("cagr_pct"),
            status,
            None,                              # recommendation
            None,                              # baseline_sharpe
            rec.get("runtime_s"),
            rec.get("agent_id"),
            rec.get("timestamp"),              # completed_at ≈ timestamp
        ))

    with get_db() as db:
        db.executemany(
            """
            INSERT OR IGNORE INTO research_experiments
                (id, strategy, universe, experiment_type, params_changed, description,
                 sharpe, trades, max_dd_pct, profit_factor, cagr_pct, status,
                 recommendation, baseline_sharpe, runtime_s, agent_id, completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )

    print(f"  ✅  {len(rows)} experiments migrated")
    return len(rows)


# ── 4. OHLCV (parquet snapshots) ──────────────────────────────────────────────

def migrate_ohlcv() -> Tuple[int, int]:
    """Returns (row_count, ticker_count)."""
    _section("4/8  ohlcv  ←  data/snapshots/sp500_v3_unadj_20260310_7yr/*.parquet")

    snap_dir = PROJECT_ROOT / "data" / "snapshots" / "sp500_v3_unadj_20260310_7yr"
    if not snap_dir.exists():
        print(f"  ⚠️  snapshot directory not found — {snap_dir} (skipping)")
        return 0, 0

    try:
        import pandas as pd
    except ImportError:
        print("  ❌ pandas not available — skipping OHLCV migration")
        return 0, 0

    parquet_files = sorted(snap_dir.glob("*.parquet"))
    if not parquet_files:
        print("  ⚠️  no parquet files found (skipping)")
        return 0, 0

    print(f"  Loading {len(parquet_files)} parquet files …")

    total_rows = 0
    ticker_count = 0
    BATCH_SIZE = 50_000  # rows per executemany call

    # Accumulate all rows, then bulk-insert in batches
    batch: List[tuple] = []

    def flush_batch(db_conn, rows: List[tuple]) -> None:
        if rows:
            db_conn.executemany(
                """
                INSERT OR REPLACE INTO ohlcv
                    (ticker, date, open, high, low, close, adj_close, volume, universe, source)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )

    with get_db() as db:
        for pfile in parquet_files:
            try:
                df = pd.read_parquet(pfile)
            except Exception as e:
                print(f"  ⚠️  failed to read {pfile.name}: {e}")
                continue

            # Ticker is stored as a column in each file
            ticker_col = df["ticker"].iloc[0] if "ticker" in df.columns else pfile.stem

            for dt_idx, row in df.iterrows():
                date_str = str(dt_idx)[:10]  # YYYY-MM-DD
                batch.append((
                    str(row.get("ticker", ticker_col)),
                    date_str,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    None,                   # adj_close not available
                    int(row["volume"]),
                    "sp500",
                    "tiingo",
                ))

                if len(batch) >= BATCH_SIZE:
                    flush_batch(db, batch)
                    total_rows += len(batch)
                    batch = []

            ticker_count += 1

        # Flush remaining
        if batch:
            flush_batch(db, batch)
            total_rows += len(batch)

    print(f"  ✅  {total_rows:,} rows across {ticker_count} tickers migrated")
    return total_rows, ticker_count


# ── 5. Research best (research/best/*.json) ───────────────────────────────────

def migrate_research_best() -> int:
    _section("5/8  research_best  ←  research/best/*.json")

    best_dir = PROJECT_ROOT / "research" / "best"
    if not best_dir.exists():
        print(f"  ⚠️  directory not found — {best_dir} (skipping)")
        return 0

    files = sorted(best_dir.glob("*.json"))
    if not files:
        print("  ⚠️  no JSON files in research/best/ (skipping)")
        return 0

    count = 0
    errors = 0
    for fpath in files:
        data, err = _load_json(fpath, fpath.name)
        if data is None:
            errors += 1
            continue

        strategy = data.get("strategy", fpath.stem)
        universe = data.get("market", "sp500")
        params = data.get("params") or {}
        metrics = data.get("metrics") or {}

        sharpe = metrics.get("sharpe")
        trades = metrics.get("total_trades")
        max_dd = metrics.get("max_drawdown_pct")

        with get_db() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO research_best
                    (strategy, universe, params, sharpe, trades, max_dd_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy, universe,
                    json.dumps(params),
                    sharpe, trades, max_dd,
                    data.get("updated_at", datetime.now().isoformat()),
                ),
            )
        count += 1

    print(f"  ✅  {count} entries migrated  ({errors} errors)")
    return count


# ── 6. Ceasefire factors ──────────────────────────────────────────────────────

def migrate_ceasefire_factors() -> int:
    _section("6/8  ceasefire_factors  ←  data/position_monitor/ceasefire_factors.json")

    path = PROJECT_ROOT / "data" / "position_monitor" / "ceasefire_factors.json"
    data, err = _load_json(path, "ceasefire_factors.json")
    if data is None:
        return 0

    factors = data.get("factors", [])
    count = 0
    errors = 0

    for f in factors:
        factor_id = f.get("id", "")
        if not factor_id:
            errors += 1
            continue

        # Map direction field to schema category (ceasefire / escalation)
        direction = f.get("direction", "")
        if direction in ("ceasefire", "escalation"):
            category = direction
        else:
            category = "ceasefire"  # safe default

        description = f.get("label") or f.get("description") or ""
        weight = float(f.get("weight", 0))
        active = 1 if f.get("active") else 0
        confidence = f.get("confidence", "medium")
        source = f.get("source")
        last_updated = f.get("last_checked") or data.get("last_updated")

        with get_db() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO ceasefire_factors
                    (id, category, description, weight, active, confidence, source, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (factor_id, category, description, weight, active, confidence,
                 source, last_updated),
            )
        count += 1

    # Also write a ceasefire_history snapshot from the top-level probability
    probability = data.get("probability")
    last_updated_ts = data.get("last_updated")
    if probability is not None and last_updated_ts:
        active_factor_ids = [
            f["id"] for f in factors if f.get("active")
        ]
        change_log = ""
        cl = data.get("change_log")
        if isinstance(cl, list) and cl:
            change_log = cl[0] if isinstance(cl[0], str) else json.dumps(cl[0])
        elif isinstance(cl, str):
            change_log = cl

        with get_db() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO ceasefire_history
                    (timestamp, probability, active_factors, change_log)
                VALUES (?, ?, ?, ?)
                """,
                (
                    last_updated_ts,
                    float(probability),
                    json.dumps(active_factor_ids),
                    change_log or None,
                ),
            )

    print(f"  ✅  {count} factors migrated  ({errors} errors)")
    return count


# ── 7. Equity curve (broker equity_history) ───────────────────────────────────

def migrate_equity_curve() -> int:
    _section("7/8  equity_curve  ←  brokers/state/live_sp500.json (equity_history)")

    with get_db() as db:
        db.execute("DELETE FROM equity_curve WHERE market_id='sp500'")
        db.execute("DELETE FROM portfolio_snapshots WHERE source='eod_history'")

    path = PROJECT_ROOT / "brokers" / "state" / "live_sp500.json"
    data, err = _load_json(path, "live_sp500.json")
    if data is None:
        return 0

    equity_history = data.get("equity_history", [])
    count = 0

    for entry in equity_history:
        date_str = entry.get("date", "")
        equity = entry.get("equity")
        cash = entry.get("cash")
        positions_value = entry.get("positions_value")

        if not date_str or equity is None:
            continue

        with get_db() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO equity_curve
                    (date, market_id, equity, cash, positions_value, day_pnl, regime_state)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (date_str, "sp500", float(equity), cash, positions_value, None, None),
            )
        count += 1

        # Also record a portfolio snapshot from each equity_history entry
        positions_list = entry.get("positions", [])
        if positions_list:
            with get_db() as db:
                db.execute(
                    """
                    INSERT INTO portfolio_snapshots
                        (timestamp, total_equity, cash, positions, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        f"{date_str}T00:00:00",
                        float(equity), cash,
                        json.dumps(positions_list),
                        "eod_history",
                    ),
                )

    print(f"  ✅  {count} equity_curve rows migrated")
    return count


# ── 8. Plans ──────────────────────────────────────────────────────────────────

def migrate_plans() -> int:
    _section("8/8  plans  ←  plans/plan_*.json")

    with get_db() as db:
        db.execute("DELETE FROM plans")

    plans_dir = PROJECT_ROOT / "plans"
    if not plans_dir.exists():
        print(f"  ⚠️  plans directory not found — {plans_dir} (skipping)")
        return 0

    plan_files = sorted(plans_dir.glob("plan_*.json"))
    if not plan_files:
        print("  ⚠️  no plan files found (skipping)")
        return 0

    count = 0
    errors = 0

    for fpath in plan_files:
        data, err = _load_json(fpath, fpath.name)
        if data is None:
            errors += 1
            continue

        trade_date = data.get("trade_date", "")
        market_id = data.get("market_id", "sp500")
        status = (data.get("status") or "pending").lower()
        approved_at = data.get("approved_at")
        executed_at = data.get("executed_at")
        config_version = data.get("config_version")

        if not trade_date:
            # Try to extract date from filename: plan_sp500_2026-03-04.json
            parts = fpath.stem.split("_")
            trade_date = parts[-1] if len(parts) >= 3 else ""

        if not trade_date:
            print(f"  ⚠️  could not determine trade_date for {fpath.name} — skipping")
            errors += 1
            continue

        with get_db() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO plans
                    (date, market_id, plan_data, status, approved_at, executed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_date, market_id,
                    json.dumps(data),
                    status,
                    approved_at,
                    executed_at,
                ),
            )
        count += 1

    print(f"  ✅  {count} plans migrated  ({errors} errors)")
    return count


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(db_path: Path) -> None:
    """Query and print a row-count summary from the database."""
    print(f"\n{'═' * 60}")
    print("  Migration Summary")
    print(f"{'═' * 60}")

    tables = [
        "trades",
        "signals",
        "research_experiments",
        "ohlcv",
        "research_best",
        "ceasefire_factors",
        "equity_curve",
        "portfolio_snapshots",
        "plans",
    ]

    with get_db() as db:
        for table in tables:
            try:
                total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                extra = ""
                if table == "trades":
                    open_n = db.execute(
                        "SELECT COUNT(*) FROM trades WHERE status='open'"
                    ).fetchone()[0]
                    closed_n = db.execute(
                        "SELECT COUNT(*) FROM trades WHERE status='closed'"
                    ).fetchone()[0]
                    extra = f" (open={open_n}, closed={closed_n})"
                elif table == "ohlcv":
                    tickers = db.execute(
                        "SELECT COUNT(DISTINCT ticker) FROM ohlcv"
                    ).fetchone()[0]
                    extra = f" ({tickers} tickers)"
                print(f"  {table:<26} {total:>8,} rows{extra}")
            except Exception as e:
                print(f"  {table:<26}  ERROR: {e}")

    print(f"{'═' * 60}")
    print(f"  DB path: {db_path}")
    print(f"{'═' * 60}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def run_migration() -> None:
    db_path = PROJECT_ROOT / "data" / "atlas.db"
    print(f"\n{'═' * 60}")
    print("  Atlas v2.0 — JSON → SQLite Migration")
    print(f"  Target: {db_path}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * 60}")

    # Initialise schema (idempotent)
    init_db(str(db_path))
    print("\n  ✅  Schema initialised")

    # Run all migrations
    migrate_trades()
    migrate_signals()
    migrate_research_experiments()
    migrate_ohlcv()
    migrate_research_best()
    migrate_ceasefire_factors()
    migrate_equity_curve()
    migrate_plans()

    # Final summary
    print_summary(db_path)
    print(f"  Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    run_migration()
