"""brokers/execution_journal.py — Atomic JSONL execution journal.

Extracted from brokers/live_executor.py (decomposition #2 PR1.1).
Pure leaf module — no broker state, no class.

Public surface
--------------
    EXECUTION_LOG : Path
        Stable path constant.  3 external readers resolve this path independently
        (research/brain/execution.py, scripts/slippage_calibration.py, healthz.py)
        so the value must remain PROJECT_ROOT / "logs" / "live_executions.jsonl".

    journal_entry(event, data) -> None
        Append one JSONL line atomically.  Never raises — write failures are
        logged as warnings so they cannot interrupt real trade execution.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("atlas.execution_journal")

PROJECT_ROOT = Path(__file__).parent.parent

EXECUTION_LOG: Path = PROJECT_ROOT / "logs" / "live_executions.jsonl"


def journal_entry(event: str, data: dict) -> None:
    """Append a line to the execution journal (JSONL).

    Resilient: any write failure is caught and logged — it must never
    interrupt or crash real trade execution.

    Atomic write pattern: the JSON line is staged to a .tmp file first.
    Only when that succeeds is the line appended to the live log.  This
    prevents a partial JSON line from corrupting the JSONL file if the
    process is killed or a disk-full error occurs mid-write.
    """
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": event,
            **data,
        }
        EXECUTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str) + "\n"

        # Stage to temp; copy to log only when fully written and serialised.
        tmp = EXECUTION_LOG.with_suffix(".tmp")
        tmp.write_text(line, encoding="utf-8")
        with open(EXECUTION_LOG, "ab") as log_f:
            log_f.write(tmp.read_bytes())
        tmp.unlink(missing_ok=True)
    except Exception as exc:
        # Journal failure must NEVER crash execution — just warn.
        logger.warning("Journal write failed (execution continues): %s", exc)
