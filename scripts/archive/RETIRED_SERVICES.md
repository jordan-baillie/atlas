# Retired Services

This file documents services and modules that have been retired and archived.

---

## atlas-research-runner.service

**Status:** RETIRED (March 2026)
**Reason:** Duplicate of sweep.py — runner_daemon was a separate queue-processing
system with its own lifecycle (QUEUED→CLAIMED→RUNNING→PASSED) that duplicated what
sweep.py already does automatically.

### What it was
A long-running systemd service that pulled experiments from `research/queue.json`,
dispatched them via `scripts/research_runner.py`, evaluated results using
`research/evaluator.py` (including DSR statistical tests), and auto-advanced lifecycle
stages (solo → optimize → combined → oos → promote).

### Why retired
- **Duplication**: sweep.py runs the same grid-search loop with auto-promotion via
  `promoter.py`. Two competing loops fight for CPU time (runner yielded to sweep via
  a lock file — a sign of the design tension).
- **Queue bottleneck**: The queue-based approach required manual population (or
  a separate director cron) and processed experiments sequentially at ~1 per 4 minutes,
  while sweep runs continuously.
- **Separate journal**: runner wrote to `experiments/*.json`; sweep wrote to
  `results/*.tsv + journal.json`. No single source of truth.
- **Option B consolidation**: As part of autoresearch alignment, the decision was made
  to unify around sweep.py as the single execution engine.

### To disable the systemd service (run once, on the VPS)
```bash
systemctl stop atlas-research-runner
systemctl disable atlas-research-runner
# Optional: remove socket file
rm -f /tmp/runner-daemon-heartbeat.json
```

### Archived files
- `scripts/archive/runner_daemon.py` — former `research/runner_daemon.py`
- `scripts/archive/evaluator.py` — former `research/evaluator.py`

### DSR logic (if needed)
The `ExperimentEvaluator.deflated_sharpe_ratio()` method in `evaluator.py` implements
Bailey & López de Prado (2014). If DSR is needed in future, import from the archived
file or move the method to a standalone `research/dsr.py` module.

### Historical data preserved
- `research/queue.json` — experiment queue (historical archive, not deleted)
- `research/experiments/` — per-experiment result envelopes (historical archive)
- `research/journal.json` — unified journal (still used by sweep.py)
