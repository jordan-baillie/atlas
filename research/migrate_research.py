#!/usr/bin/env python3
"""Research database migration and backfill.

Creates missing tables and backfills TSV/brain data.
Safe to run multiple times (idempotent).
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from db.atlas_db import get_db

RESULTS_DIR = ATLAS_ROOT / "research" / "results"
BRAIN_PARAMS_DIR = ATLAS_ROOT / "research" / "brain" / "params"
BRAIN_PATTERNS_DIR = ATLAS_ROOT / "research" / "brain" / "patterns"


def create_tables():
    """Create the new research tables (IF NOT EXISTS)."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS research_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                mode TEXT NOT NULL,
                strategy TEXT,
                experiments_run INTEGER DEFAULT 0,
                experiments_kept INTEGER DEFAULT 0,
                duration_minutes REAL,
                status TEXT DEFAULT 'running'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS research_brain (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                title TEXT,
                content TEXT,
                sharpe_delta REAL,
                source_file TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS research_discoveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                papers_found INTEGER DEFAULT 0,
                papers_filtered INTEGER DEFAULT 0,
                specs_extracted INTEGER DEFAULT 0,
                strategies_generated INTEGER DEFAULT 0,
                paper_titles TEXT,
                status TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Add indexes
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_mode ON research_sessions(mode)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_brain_strategy ON research_brain(strategy)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_brain_type ON research_brain(entry_type)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_discoveries_date ON research_discoveries(run_date)")
        print("✓ Tables created")


def backfill_experiments():
    """Backfill research_experiments from TSV files.

    Clears all tsv-prefixed rows and re-inserts with unique IDs (row_idx suffix).
    Non-tsv rows (ar-*, live-*, etc.) are preserved untouched.
    Safe to run multiple times (idempotent for non-tsv rows).
    """
    if not RESULTS_DIR.exists():
        print("⚠ No results directory found")
        return

    total_inserted = 0

    with get_db() as db:
        # Remove previous backfill rows so we can re-insert with unique IDs
        db.execute("DELETE FROM research_experiments WHERE id LIKE 'tsv-%'")

        for tsv_file in sorted(RESULTS_DIR.glob("*.tsv")):
            strategy = tsv_file.stem
            with open(tsv_file, "r") as f:
                reader = csv.DictReader(f, delimiter="\t")
                for row_idx, row in enumerate(reader):
                    ts = row.get("timestamp", "")
                    if not ts:
                        continue

                    # row_idx suffix ensures uniqueness even for same-second experiments
                    exp_id = f"tsv-{strategy}-{ts.replace(':', '').replace('T', '-')}-{row_idx}"
                    status_raw = row.get("status", "").strip()
                    db_status = "kept" if status_raw == "keep" else "discarded" if status_raw == "discard" else status_raw

                    try:
                        db.execute("""
                            INSERT OR IGNORE INTO research_experiments
                                (id, strategy, universe, experiment_type, params_changed,
                                 description, sharpe, trades, max_dd_pct, profit_factor,
                                 cagr_pct, status, recommendation, agent_id, created_at, completed_at)
                            VALUES (?, ?, 'sp500', 'sweeper', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'backfill', ?, ?)
                        """, (
                            exp_id, strategy,
                            row.get("params_changed", "") or None,
                            row.get("description", "") or None,
                            float(row.get("sharpe", 0) or 0),
                            int(row.get("trades", 0) or 0),
                            float(row.get("max_dd_pct", 0) or 0),
                            float(row.get("pf", 0) or 0),
                            float(row.get("cagr_pct", 0) or 0),
                            db_status,
                            row.get("description", "") or None,
                            ts, ts,
                        ))
                        total_inserted += 1
                    except Exception as e:
                        print(f"  ⚠ Error inserting {strategy}/{ts}: {e}")

    print(f"✓ Experiments backfilled: {total_inserted} inserted from TSV")


def backfill_brain():
    """Backfill research_brain from brain markdown files."""
    total = 0

    with get_db() as db:
        # Clear and re-insert (brain data is small, simpler than dedup)
        db.execute("DELETE FROM research_brain")

        # Brain params
        if BRAIN_PARAMS_DIR.exists():
            for md_file in sorted(BRAIN_PARAMS_DIR.glob("*.md")):
                if md_file.name == "_index.md":
                    continue
                param_name = md_file.stem
                content = md_file.read_text()

                # Parse the markdown table rows to extract per-strategy entries
                # Table format: | date | strategy | change | result | sharpe_delta | new_sharpe |
                lines = content.split("\n")
                for line in lines:
                    if not line.startswith("|") or "---" in line or "date" in line.lower():
                        continue
                    parts = [p.strip() for p in line.split("|")]
                    parts = [p for p in parts if p]  # remove empty from leading/trailing |
                    if len(parts) >= 4:
                        strategy = parts[1] if len(parts) > 1 else ""
                        sharpe_delta = None
                        if len(parts) > 4:
                            try:
                                sharpe_delta = float(parts[4].replace("+", ""))
                            except (ValueError, IndexError):
                                pass
                        db.execute("""
                            INSERT INTO research_brain (strategy, entry_type, title, content, sharpe_delta, source_file)
                            VALUES (?, 'param', ?, ?, ?, ?)
                        """, (strategy, param_name, line, sharpe_delta, str(md_file.relative_to(ATLAS_ROOT))))
                        total += 1

        # Brain patterns
        if BRAIN_PATTERNS_DIR.exists():
            for md_file in sorted(BRAIN_PATTERNS_DIR.glob("*.md")):
                if md_file.name == "_index.md":
                    continue
                pattern_name = md_file.stem
                content = md_file.read_text()
                # Extract summary from first paragraph after title
                lines = content.split("\n")
                summary = ""
                for line in lines:
                    if line.startswith(">"):
                        summary = line.lstrip("> ").strip()
                        break

                db.execute("""
                    INSERT INTO research_brain (strategy, entry_type, title, content, source_file)
                    VALUES ('_global', 'pattern', ?, ?, ?)
                """, (pattern_name, summary or content[:200], str(md_file.relative_to(ATLAS_ROOT))))
                total += 1

    print(f"✓ Brain entries backfilled: {total}")


def main():
    print("=== Research Database Migration ===")
    create_tables()
    backfill_experiments()
    backfill_brain()
    print("=== Migration complete ===")


if __name__ == "__main__":
    main()
