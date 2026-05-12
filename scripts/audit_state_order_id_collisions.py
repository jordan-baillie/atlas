"""
audit_state_order_id_collisions.py
------------------------------------
Scan all brokers/state/live_*.json files for positions where
stop_order_id == tp_order_id AND both are non-empty strings.

This is a forbidden schema state — the same Alpaca order UUID should never
be used as both the stop-loss leg and the take-profit leg of a position.

Outputs:
  - Human-readable summary to stdout
  - JSON report to data/audit/cat_state_repair_2026-05-12.json under
    key ``collision_audit`` (merged in place if file exists)

Exit codes:
  0  — no collisions found
  1  — one or more collisions found
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
STATE_DIR = PROJECT_ROOT / "brokers" / "state"
AUDIT_PATH = PROJECT_ROOT / "data" / "audit" / "cat_state_repair_2026-05-12.json"


def find_collisions(state_dir: Path = STATE_DIR) -> dict:
    """
    Scan all live_*.json state files for stop_order_id == tp_order_id collisions.

    Returns a dict with structure::

        {
            "files_scanned": int,
            "total_positions": int,
            "collisions": [
                {"market": str, "ticker": str, "colliding_uuid": str}
            ],
            "collision_count": int,
        }
    """
    state_files = sorted(state_dir.glob("live_*.json"))
    results: dict = {
        "files_scanned": 0,
        "total_positions": 0,
        "collisions": [],
        "collision_count": 0,
    }

    for state_file in state_files:
        market = state_file.stem.replace("live_", "")
        try:
            data = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not parse %s: %s", state_file, exc)
            continue

        results["files_scanned"] += 1
        positions = data.get("positions", [])
        results["total_positions"] += len(positions)

        for pos in positions:
            ticker = pos.get("ticker", "?")
            stop_id = pos.get("stop_order_id", "")
            tp_id = pos.get("tp_order_id", "")

            # Collision: both non-empty AND identical
            if stop_id and tp_id and stop_id == tp_id:
                collision = {
                    "market": market,
                    "ticker": ticker,
                    "colliding_uuid": stop_id,
                    "stop_order_id": stop_id,
                    "tp_order_id": tp_id,
                }
                results["collisions"].append(collision)
                logger.error(
                    "COLLISION: market=%s ticker=%s stop_order_id==tp_order_id==%s",
                    market, ticker, stop_id,
                )

    results["collision_count"] = len(results["collisions"])
    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = find_collisions()

    # Human-readable output
    print(f"Files scanned : {report['files_scanned']}")
    print(f"Total positions: {report['total_positions']}")
    print(f"Collisions found: {report['collision_count']}")

    if report["collisions"]:
        print("\nCOLLISIONS:")
        for c in report["collisions"]:
            print(f"  market={c['market']}  ticker={c['ticker']}  uuid={c['colliding_uuid']}")
        print("\n*** FIX REQUIRED: the above positions have stop_order_id == tp_order_id ***")
    else:
        print("No collisions detected. All positions have distinct or empty order IDs.")

    # Merge into audit JSON
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        audit: dict = json.loads(AUDIT_PATH.read_text()) if AUDIT_PATH.exists() else {}
    except json.JSONDecodeError:
        audit = {}

    audit["collision_audit"] = report
    AUDIT_PATH.write_text(json.dumps(audit, indent=2))
    print(f"\nCollision audit written to {AUDIT_PATH}")

    return 1 if report["collision_count"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
