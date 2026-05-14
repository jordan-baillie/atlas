#!/usr/bin/env python3
"""One-shot extraction of _audit/_weight_update/_param_update keys from
config/active/*.json into config/audit_log/<market>.jsonl sidecars.

Run from /root/atlas. Idempotent (re-running on a trimmed config writes
zero new lines since matching keys are gone). #331.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

PREFIXES = ("_audit", "_weight_update", "_param_update", "_audit_log")
ACTIVE = Path("config/active")
SIDECAR_DIR = Path("config/audit_log")
SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d")

results: list[tuple[str, int, int]] = []
for cfg_path in sorted(ACTIVE.glob("*.json")):
    if cfg_path.name.startswith(".") or ".bak" in cfg_path.name:
        continue
    market = cfg_path.stem
    with cfg_path.open() as fh:
        data = json.load(fh)

    keys_to_extract = [k for k in data.keys() if any(k.startswith(p) for p in PREFIXES)]
    if not keys_to_extract:
        results.append((cfg_path.name, 0, 0))
        continue

    sidecar = SIDECAR_DIR / f"{market}.jsonl"
    orig_lines = sum(1 for _ in cfg_path.open())
    lines_added = 0

    with sidecar.open("a") as fh:
        for k in keys_to_extract:
            entry = {
                "event_type": k,
                "extracted_at": NOW,
                "source": str(cfg_path),
                "data": data[k],
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            lines_added += 1

    # Delete extracted keys from config (outside the writer loop)
    for k in keys_to_extract:
        del data[k]

    # Write back trimmed config
    with cfg_path.open("w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")

    new_lines = sum(1 for _ in cfg_path.open())
    results.append((cfg_path.name, lines_added, orig_lines - new_lines))

print(f"{'File':<35} {'Lines extracted':>15} {'LOC delta':>12}")
for name, n, delta in results:
    sign = "-" if delta > 0 else ("+" if delta < 0 else "")
    print(f"{name:<35} {n:>15} {sign}{abs(delta):>11}")
