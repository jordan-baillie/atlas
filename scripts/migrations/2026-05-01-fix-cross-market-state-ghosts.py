#!/usr/bin/env python3
"""One-shot migration: detect and fix cross-market state-file ghosts.

A "ghost" is a position in live_X.json whose canonical universe (per
universe.membership.derive_universe) differs from X.

This is idempotent — re-running has no effect if positions are already correct.

Usage:
    python3 scripts/migrations/2026-05-01-fix-cross-market-state-ghosts.py [--dry-run]

Expected outcome (2026-05-01 post-fb28c6ff): ZERO ghosts found — this
script is a preventive measure for future operator errors.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_STATE_DIR = _PROJECT_ROOT / "brokers" / "state"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s: %s", path.name, exc)
        return {}


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically using a temp file + rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def detect_ghosts(state_dir: Path = _STATE_DIR) -> list[dict]:
    """Return list of ghost records (cross-market positions)."""
    from universe.membership import check_state_file_universes, clear_cache
    clear_cache()
    return check_state_file_universes(state_dir)


def fix_ghosts(ghosts: list[dict], state_dir: Path = _STATE_DIR, dry_run: bool = True) -> int:
    """Move each ghost position to its canonical state file.

    Returns number of positions moved.
    """
    if not ghosts:
        logger.info("No ghosts found — nothing to fix.")
        return 0

    # Group by source file
    by_source: dict[str, list[dict]] = {}
    for g in ghosts:
        by_source.setdefault(g["file"], []).append(g)

    moved = 0
    for source_file, items in by_source.items():
        source_path = state_dir / source_file
        source_state = _load_state(source_path)
        if not source_state:
            logger.warning("Could not load source state %s — skipping ghosts: %s",
                           source_file, [g["ticker"] for g in items])
            continue

        for ghost in items:
            ticker = ghost["ticker"]
            src_market = ghost["market_id"]
            dst_market = ghost["canonical_universe"]
            dst_file = f"live_{dst_market}.json"
            dst_path = state_dir / dst_file

            # Find the position entry in source state
            source_positions = source_state.get("positions", [])
            pos_entry = next((p for p in source_positions if p.get("ticker") == ticker), None)
            if pos_entry is None:
                logger.warning("Ghost %s not found in %s positions — skip", ticker, source_file)
                continue

            logger.info(
                "Ghost: %s in %s → moving to %s (dry_run=%s)",
                ticker, source_file, dst_file, dry_run,
            )

            if dry_run:
                print(f"  [DRY-RUN] Would move {ticker} from {source_file} → {dst_file}")
                continue

            # Load destination state (create minimal if missing)
            dst_state = _load_state(dst_path) if dst_path.exists() else {
                "market_id": dst_market,
                "mode": "live",
                "positions": [],
                "closed_trades": [],
                "equity_history": [],
                "daily_high_water": pos_entry.get("entry_price", 0.0) * pos_entry.get("shares", 1.0),
                "daily_high_water_date": None,
                "halted": False,
                "halt_reason": "",
            }

            # Add to destination if not already there
            dst_tickers = {p.get("ticker") for p in dst_state.get("positions", [])}
            if ticker not in dst_tickers:
                dst_state.setdefault("positions", []).append(pos_entry)
                logger.info("Added %s to %s", ticker, dst_file)
            else:
                logger.info("%s already in %s — skipping add", ticker, dst_file)

            # Remove from source
            source_state["positions"] = [p for p in source_positions if p.get("ticker") != ticker]

            # Write both atomically
            _atomic_write(dst_path, dst_state)
            _atomic_write(source_path, source_state)
            moved += 1
            logger.info("Moved %s: %s → %s", ticker, source_file, dst_file)

    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Report ghosts without modifying any files (default: False)")
    args = parser.parse_args()

    print(f"Scanning state files in {_STATE_DIR}")
    ghosts = detect_ghosts()

    if not ghosts:
        print("✓ No cross-market state-file ghosts found — nothing to do.")
        return 0

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}Found {len(ghosts)} ghost(s):")
    for g in ghosts:
        print(f"  {g['ticker']} in {g['file']} (market={g['market_id']}) "
              f"→ canonical={g['canonical_universe']}")

    moved = fix_ghosts(ghosts, dry_run=args.dry_run)
    if args.dry_run:
        print(f"\n[DRY-RUN] Would have moved {len(ghosts)} position(s). "
              f"Re-run without --dry-run to apply.")
    else:
        print(f"\nMoved {moved}/{len(ghosts)} position(s) to their canonical state files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
