#!/usr/bin/env python3
"""Verify health monitoring tables are functional.

Tests:
1. init_db() succeeds
2. record_heartbeat() writes and reads back
3. record_system_log() writes and reads back
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def main():
    from db import atlas_db

    passed = 0
    failed = 0

    # Test 1: init_db
    print("Test 1: init_db()...", end=" ")
    try:
        atlas_db.init_db()
        print("PASS")
        passed += 1
    except Exception as e:
        print(f"FAIL: {e}")
        failed += 1
        return 1  # Can't continue without DB

    # Test 2: record_heartbeat + read back
    print("Test 2: record_heartbeat()...", end=" ")
    try:
        atlas_db.record_heartbeat(
            service="verify_test",
            status="ok",
            detail={"test": True, "source": "verify_health_tables.py"},
        )
        heartbeats = atlas_db.get_heartbeats(service="verify_test")
        assert len(heartbeats) >= 1, f"Expected >=1 heartbeat, got {len(heartbeats)}"
        hb = heartbeats[0]
        assert hb["service"] == "verify_test", f"service mismatch: {hb['service']}"
        assert hb["status"] == "ok", f"status mismatch: {hb['status']}"
        print("PASS")
        passed += 1
    except Exception as e:
        print(f"FAIL: {e}")
        failed += 1

    # Test 3: record_system_log + read back
    print("Test 3: record_system_log()...", end=" ")
    try:
        atlas_db.record_system_log(
            level="info",
            service="verify_test",
            message="Health table verification",
            detail={"test": True},
        )
        logs = atlas_db.get_system_logs(service="verify_test", hours=1)
        assert len(logs) >= 1, f"Expected >=1 log entry, got {len(logs)}"
        log_entry = logs[0]
        assert log_entry["service"] == "verify_test", f"service mismatch"
        assert log_entry["level"] == "info", f"level mismatch"
        print("PASS")
        passed += 1
    except Exception as e:
        print(f"FAIL: {e}")
        failed += 1

    # Test 4: health_writer wrappers
    print("Test 4: health_writer.heartbeat()...", end=" ")
    try:
        from monitor.health_writer import heartbeat, log_info
        heartbeat("verify_writer_test", "ok", {"wrapper": True})
        hbs = atlas_db.get_heartbeats(service="verify_writer_test")
        assert len(hbs) >= 1, "heartbeat via wrapper not found"
        print("PASS")
        passed += 1
    except Exception as e:
        print(f"FAIL: {e}")
        failed += 1

    # Test 5: health_writer log_info
    print("Test 5: health_writer.log_info()...", end=" ")
    try:
        log_info("verify_writer_test", "Test log via wrapper", {"wrapper": True})
        logs = atlas_db.get_system_logs(service="verify_writer_test", hours=1)
        assert len(logs) >= 1, "system_log via wrapper not found"
        print("PASS")
        passed += 1
    except Exception as e:
        print(f"FAIL: {e}")
        failed += 1

    # Summary
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")

    # Show current table counts
    heartbeats = atlas_db.get_heartbeats()
    logs = atlas_db.get_system_logs(hours=24)
    print(f"\nCurrent heartbeats: {len(heartbeats)} services")
    for hb in heartbeats:
        print(f"  {hb['service']}: {hb['status']} @ {hb['timestamp']}")
    print(f"Current system_log (24h): {len(logs)} entries")
    for log_entry in logs[:10]:
        print(f"  [{log_entry['level']}] {log_entry['service']}: {log_entry.get('message', '')}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
