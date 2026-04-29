"""
tests/test_state_drift_detector.py

Tests for scripts/state_drift_detector.py.

Uses tmp_path for DB + JSON state files — never touches production state.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Utilities ─────────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path, open_trades: list[dict]) -> Path:
    """Create a tmp SQLite DB with the trades table populated."""
    db_path = tmp_path / "atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT NOT NULL,
            universe    TEXT,
            direction   TEXT DEFAULT 'long',
            entry_price REAL,
            shares      INTEGER,
            stop_price  REAL,
            take_profit REAL,
            status      TEXT DEFAULT 'open'
        )
    """)
    for t in open_trades:
        conn.execute(
            "INSERT INTO trades (ticker, universe, entry_price, shares, stop_price, take_profit, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (
                t["ticker"],
                t.get("universe"),
                t.get("entry_price"),
                t.get("shares"),
                t.get("stop_price"),
                t.get("take_profit"),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _make_state_dir(tmp_path: Path, market_positions: dict[str, list[dict]]) -> Path:
    """Write live_<market>.json files in a tmp state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for market, positions in market_positions.items():
        path = state_dir / f"live_{market}.json"
        path.write_text(json.dumps({"positions": positions}))
    return state_dir


def _load_module():
    """Load the drift detector module."""
    import importlib.util
    spec_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "state_drift_detector.py"
    )
    spec = importlib.util.spec_from_file_location("drift_detector", str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def det():
    """The drift detector module (loaded once per test)."""
    return _load_module()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCleanState:
    def test_clean_state_exits_zero(self, det, tmp_path: Path) -> None:
        """No drift → run_detection returns exit_code=0."""
        db_path = _make_db(tmp_path, [
            {"ticker": "CAT", "universe": "sp500", "entry_price": 835.24, "shares": 1, "stop_price": 799.47},
        ])
        state_dir = _make_state_dir(tmp_path, {
            "sp500": [{"ticker": "CAT", "entry_price": 835.24, "shares": 1, "stop_price": 799.47}],
        })

        # Patch paths
        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        drifts, exit_code = det.run_detection(markets=["sp500"], no_alert=True, db_path=db_path)
        assert exit_code == 0
        assert len(drifts) == 0


class TestOrphanDetection:
    def test_orphan_in_sqlite_alerts(self, det, tmp_path: Path) -> None:
        """Ticker in SQLite open trades but absent from JSON → orphan_in_sqlite."""
        db_path = _make_db(tmp_path, [
            {"ticker": "MU", "universe": "sp500", "entry_price": 517.70, "shares": 2, "stop_price": 508.78},
        ])
        state_dir = _make_state_dir(tmp_path, {"sp500": []})  # MU not in JSON

        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        drifts, exit_code = det.run_detection(markets=["sp500"], no_alert=True, db_path=db_path)
        assert exit_code == 1
        assert len(drifts) == 1
        assert drifts[0].ticker == "MU"
        assert "orphan in SQLite" in drifts[0].reason

    def test_orphan_in_json_alerts(self, det, tmp_path: Path) -> None:
        """Ticker in JSON but absent from SQLite open trades → orphan_in_json."""
        db_path = _make_db(tmp_path, [])  # empty SQLite
        state_dir = _make_state_dir(tmp_path, {
            "sp500": [{"ticker": "NVDA", "entry_price": 800.0, "shares": 3, "stop_price": 760.0}],
        })

        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        drifts, exit_code = det.run_detection(markets=["sp500"], no_alert=True, db_path=db_path)
        assert exit_code == 1
        assert len(drifts) == 1
        assert drifts[0].ticker == "NVDA"
        assert "orphan in JSON" in drifts[0].reason


class TestValueDrift:
    def test_value_drift_alerts(self, det, tmp_path: Path) -> None:
        """Stop_price mismatch between JSON and SQLite → value_drift."""
        db_path = _make_db(tmp_path, [
            {"ticker": "GLD", "universe": "commodity_etfs", "entry_price": 442.8,
             "shares": 2, "stop_price": 420.66},   # SQLite has 420.66
        ])
        state_dir = _make_state_dir(tmp_path, {
            "commodity_etfs": [
                {"ticker": "GLD", "entry_price": 442.8, "shares": 2, "stop_price": 418.105}  # JSON has 418.105
            ],
        })

        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        drifts, exit_code = det.run_detection(markets=["commodity_etfs"], no_alert=True, db_path=db_path)
        assert exit_code == 1
        assert len(drifts) == 1
        d = drifts[0]
        assert d.ticker == "GLD"
        assert "drift" in d.reason
        # Verify field_diffs captures stop_price
        assert any(f[0] == "stop_price" for f in d.field_diffs)


class TestAlertCooldown:
    def test_alert_cooldown_skips_repeat(self, det, tmp_path: Path) -> None:
        """Second call within cooldown window does NOT fire alert."""
        db_path = _make_db(tmp_path, [
            {"ticker": "MU", "universe": "sp500", "entry_price": 100.0, "shares": 1},
        ])
        state_dir = _make_state_dir(tmp_path, {"sp500": []})
        cooldown_file = tmp_path / "cooldown.json"

        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        alerts_sent: list[str] = []

        def fake_send(text: str, **kwargs) -> None:
            alerts_sent.append(text)

        def fake_update_cooldown(state_file=None):
            # Write a timestamp in the PAST (just now)
            path = state_file or cooldown_file
            path.write_text(json.dumps({"last_alert_utc": datetime.now(tz=timezone.utc).isoformat()}))

        # First run — alert fires, cooldown written
        with patch.object(det, "_send_alert", side_effect=lambda d: alerts_sent.append("alert")):
            with patch.object(det, "_update_cooldown", side_effect=fake_update_cooldown):
                det.run_detection(markets=["sp500"], no_alert=False, db_path=db_path,
                                  cooldown_file=cooldown_file)

        first_count = len(alerts_sent)
        assert first_count >= 1, "Alert should fire on first run"

        # Second run within cooldown window — should NOT fire
        with patch.object(det, "_send_alert", side_effect=lambda d: alerts_sent.append("alert2")):
            with patch.object(det, "_is_in_cooldown", return_value=True):
                det.run_detection(markets=["sp500"], no_alert=False, db_path=db_path,
                                  cooldown_file=cooldown_file)

        assert len(alerts_sent) == first_count, "Alert should NOT fire within cooldown"

    def test_no_alert_flag(self, det, tmp_path: Path) -> None:
        """--no-alert flag suppresses Telegram even when drift is found."""
        db_path = _make_db(tmp_path, [
            {"ticker": "AMD", "universe": "sp500", "entry_price": 120.0, "shares": 5},
        ])
        state_dir = _make_state_dir(tmp_path, {"sp500": []})

        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        alerts_sent: list[str] = []
        with patch.object(det, "_send_alert", side_effect=lambda d: alerts_sent.append("alert")):
            drifts, exit_code = det.run_detection(markets=["sp500"], no_alert=True, db_path=db_path)

        assert exit_code == 1
        assert len(drifts) == 1
        assert len(alerts_sent) == 0, "Alert must not fire when --no-alert is set"


class TestMultiMarket:
    def test_multiple_markets_consolidated(self, det, tmp_path: Path) -> None:
        """Drift across multiple markets is returned as a single consolidated list."""
        db_path = _make_db(tmp_path, [
            {"ticker": "GLD",  "universe": "commodity_etfs", "entry_price": 442.8,  "shares": 2, "stop_price": 420.66},
            {"ticker": "XLI",  "universe": "sector_etfs",    "entry_price": 173.97, "shares": 9, "stop_price": 169.23},
            {"ticker": "AAPL", "universe": "sp500",           "entry_price": 190.0,  "shares": 1},
        ])
        state_dir = _make_state_dir(tmp_path, {
            "commodity_etfs": [
                {"ticker": "GLD", "entry_price": 442.8, "shares": 2, "stop_price": 418.10}  # drift
            ],
            "sector_etfs": [
                {"ticker": "XLI", "entry_price": 173.97, "shares": 9, "stop_price": 168.77}  # drift
            ],
            "sp500": [],  # AAPL missing from JSON → orphan_in_sqlite
        })

        det.DB_PATH = db_path
        det.STATE_DIR = state_dir

        drifts, exit_code = det.run_detection(
            markets=["sp500", "commodity_etfs", "sector_etfs"],
            no_alert=True,
            db_path=db_path,
        )

        assert exit_code == 1
        assert len(drifts) == 3

        tickers = {d.ticker for d in drifts}
        assert "GLD" in tickers
        assert "XLI" in tickers
        assert "AAPL" in tickers

        reasons = {d.ticker: d.reason for d in drifts}
        assert "drift" in reasons["GLD"]
        assert "drift" in reasons["XLI"]
        assert "orphan in SQLite" in reasons["AAPL"]
