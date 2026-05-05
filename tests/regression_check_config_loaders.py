#!/usr/bin/env python3
"""Regression check: byte-match old vs new config loader.

NOT part of pytest collection — a manual diff tool to verify the canonical
loader (get_active_config) returns byte-identical results to raw json.load()
when the config_overrides table is EMPTY.

Usage:
    python3 tests/regression_check_config_loaders.py

Expected output: PASS for all 8 market universes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.config import get_active_config, clear_config_cache

ACTIVE_DIR = PROJECT / "config" / "active"

def main() -> int:
    print("=== Regression check: get_active_config vs raw json.load ===")
    print("Assumption: config_overrides table is EMPTY (Phase 1)\n")

    fail_count = 0
    pass_count = 0

    for cfg_path in sorted(ACTIVE_DIR.glob("*.json")):
        market_id = cfg_path.stem
        if market_id == "regime":
            print(f"  SKIP  {market_id} (regime config — not a market)")
            continue

        clear_config_cache()  # ensure fresh read each time

        # 1. Raw json.load (legacy path)
        try:
            with open(cfg_path) as f:
                raw_from_file = json.load(f)
        except Exception as e:
            print(f"  ERROR  {market_id}: raw json.load failed: {e}")
            fail_count += 1
            continue

        # 2. get_active_config(apply_overrides=True)
        try:
            cfg_overrides_true = get_active_config(market_id, apply_overrides=True)
        except Exception as e:
            print(f"  ERROR  {market_id}: get_active_config(True) failed: {e}")
            fail_count += 1
            continue
        finally:
            clear_config_cache()

        # 3. get_active_config(apply_overrides=False)
        try:
            cfg_overrides_false = get_active_config(market_id, apply_overrides=False)
        except Exception as e:
            print(f"  ERROR  {market_id}: get_active_config(False) failed: {e}")
            fail_count += 1
            continue
        finally:
            clear_config_cache()

        # Compare: remove _overrides_applied marker before comparison
        def _normalize(d):
            d2 = dict(d)
            d2.pop("_overrides_applied", None)
            return json.dumps(d2, sort_keys=True)

        raw_s = _normalize(raw_from_file)
        true_s = _normalize(cfg_overrides_true)
        false_s = _normalize(cfg_overrides_false)

        # With empty override table, all three should be byte-identical
        ok = (raw_s == true_s) and (raw_s == false_s)
        # Also confirm _overrides_applied is NOT present when table is empty
        no_marker = "_overrides_applied" not in cfg_overrides_true

        if ok and no_marker:
            print(f"  PASS  {market_id}  (raw==apply_overrides==True, raw==apply_overrides==False, no marker)")
            pass_count += 1
        else:
            print(f"  FAIL  {market_id}")
            if raw_s != true_s:
                print(f"    raw vs apply_overrides=True DIFFER")
            if raw_s != false_s:
                print(f"    raw vs apply_overrides=False DIFFER")
            if not no_marker:
                print(f"    WARNING: _overrides_applied=True present (override table not empty?)")
            fail_count += 1

    print(f"\n=== Results: {pass_count} PASS, {fail_count} FAIL ===")
    if fail_count > 0:
        print("REGRESSION DETECTED — check above diffs before committing")
        return 1
    print("ALL PASS — safe to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
