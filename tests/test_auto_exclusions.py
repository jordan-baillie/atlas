"""Tests for stale ticker auto-exclusion system.

Covers:
1. Auto-exclusion module (data/auto_exclusions.py)
2. Smart freshness check (graduated response in verify_ingest_freshness)
3. Universe rebuild on exclusion changes (ensure_universe_current)
4. Recovery check (scripts/stale_ticker_recovery.py)

Run: cd /root/atlas && python3 -m pytest tests/test_auto_exclusions.py -v --timeout=30
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

import pandas as pd
import pytest

import db.atlas_db as _adb
from db.atlas_db import init_db

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ── Helpers ──────────────────────────────────────────────────

def _df_with_latest(date_str: str, n_rows: int = 10) -> pd.DataFrame:
    """Build a test DataFrame whose most recent row is on *date_str*."""
    end = pd.Timestamp(date_str)
    dates = pd.bdate_range(end=end, periods=n_rows)
    return pd.DataFrame(
        {"close": [100.0 + i for i in range(len(dates))], "volume": [1000] * len(dates), "ticker": "TEST"},
        index=dates,
    )


def _last_weekday_str() -> str:
    d = datetime.now()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _old_date_str(days_ago: int = 7) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point atlas_db at a throw-away temp DB so tests never touch production."""
    db_path = str(tmp_path / "test_auto_exclusions.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    init_db()
    yield
    monkeypatch.setattr(_adb, "_db_path_override", None)


# ── Auto-exclusion module tests ──────────────────────────────

class TestAutoExclusions:
    """Test data/auto_exclusions.py CRUD operations."""

    @pytest.fixture(autouse=True)
    def mock_exclusion_file(self, tmp_path, monkeypatch):
        """Redirect auto-exclusion file to temp dir."""
        import data.auto_exclusions as ae
        self.excl_file = tmp_path / "auto_excluded_tickers.json"
        monkeypatch.setattr(ae, "AUTO_EXCLUSION_FILE", self.excl_file)

    def test_empty_exclusions_returns_empty_set(self):
        from data.auto_exclusions import get_excluded_tickers
        assert get_excluded_tickers() == set()

    def test_add_exclusion_creates_file(self):
        from data.auto_exclusions import add_exclusion
        add_exclusion("BADTICK", "sp500", "stale data", "2026-01-01")
        assert self.excl_file.exists()
        data = json.loads(self.excl_file.read_text())
        assert "BADTICK" in data["excluded"]
        assert data["excluded"]["BADTICK"]["market_id"] == "sp500"

    def test_add_and_get_exclusions(self):
        from data.auto_exclusions import add_exclusion, get_excluded_tickers
        add_exclusion("AAA", "sp500", "stale")
        add_exclusion("BBB", "sp500", "delisted")
        add_exclusion("CCC", "asx", "stale")
        assert get_excluded_tickers() == {"AAA", "BBB", "CCC"}
        assert get_excluded_tickers("sp500") == {"AAA", "BBB"}
        assert get_excluded_tickers("asx") == {"CCC"}

    def test_remove_exclusion(self):
        from data.auto_exclusions import add_exclusion, remove_exclusion, get_excluded_tickers
        add_exclusion("GONE", "sp500", "test")
        assert "GONE" in get_excluded_tickers()
        result = remove_exclusion("GONE")
        assert result is True
        assert "GONE" not in get_excluded_tickers()

    def test_remove_nonexistent_returns_false(self):
        from data.auto_exclusions import remove_exclusion
        assert remove_exclusion("NOPE") is False

    def test_update_recovery_attempt(self):
        from data.auto_exclusions import add_exclusion, update_recovery_attempt, get_exclusion_details
        add_exclusion("RETRY", "sp500", "test")
        update_recovery_attempt("RETRY")
        update_recovery_attempt("RETRY")
        details = get_exclusion_details()
        assert details["excluded"]["RETRY"]["recovery_attempts"] == 2
        assert details["excluded"]["RETRY"]["last_recovery_attempt"] is not None

    def test_ticker_uppercased(self):
        from data.auto_exclusions import add_exclusion, get_excluded_tickers
        add_exclusion("lowercase", "sp500", "test")
        assert "LOWERCASE" in get_excluded_tickers()

    def test_get_exclusion_details(self):
        from data.auto_exclusions import add_exclusion, get_exclusion_details
        add_exclusion("DET", "sp500", "test_reason", "2026-03-01")
        details = get_exclusion_details()
        assert "excluded" in details
        assert details["excluded"]["DET"]["reason"] == "test_reason"
        assert details["excluded"]["DET"]["last_data_date"] == "2026-03-01"

    def test_quarantine_cache(self, tmp_path, monkeypatch):
        from data.auto_exclusions import quarantine_cache
        # Create a fake cache file
        import data.ingest as ingest
        cache_dir = tmp_path / "cache" / "sp500"
        cache_dir.mkdir(parents=True)
        fake_cache = cache_dir / "BADTICK.parquet"
        fake_cache.write_text("fake data")

        monkeypatch.setattr(ingest, "_cache_path", lambda t, m=None: fake_cache)
        result = quarantine_cache("BADTICK", "sp500")
        assert result is not None
        assert "quarantine" in str(result)
        assert not fake_cache.exists()  # Original moved
        assert result.exists()  # Quarantine file exists

    def test_quarantine_nonexistent_returns_none(self, monkeypatch):
        from data.auto_exclusions import quarantine_cache
        import data.ingest as ingest
        monkeypatch.setattr(ingest, "_cache_path", lambda t, m=None: Path("/nonexistent/file.parquet"))
        assert quarantine_cache("NOPE", "sp500") is None

    def test_corrupted_file_handled(self):
        """Corrupted JSON file doesn't crash — returns empty."""
        from data.auto_exclusions import get_excluded_tickers
        self.excl_file.write_text("{invalid json")
        assert get_excluded_tickers() == set()


# ── Smart freshness check tests ─────────────────────────────

class TestSmartFreshnessCheck:
    """Test graduated response in verify_ingest_freshness."""

    def _fresh_data(self, n: int = 5) -> dict:
        today = _last_weekday_str()
        return {f"FRESH{i}": _df_with_latest(today) for i in range(n)}

    def _stale_data(self, n: int = 5) -> dict:
        old = _old_date_str(10)
        return {f"STALE{i}": _df_with_latest(old) for i in range(n)}

    def _mixed_data(self, fresh_count: int, stale_count: int) -> dict:
        today = _last_weekday_str()
        old = _old_date_str(10)
        data = {}
        for i in range(fresh_count):
            data[f"FRESH{i}"] = _df_with_latest(today)
        for i in range(stale_count):
            data[f"STALE{i}"] = _df_with_latest(old)
        return data

    def _cfg(self, halt: bool = True) -> dict:
        return {"trading": {"live_safety": {"halt_on_stale_data": halt}}}

    @pytest.fixture(autouse=True)
    def mock_exclusion_file(self, tmp_path, monkeypatch):
        """Redirect auto-exclusion to temp."""
        import data.auto_exclusions as ae
        self.excl_file = tmp_path / "auto_excluded_tickers.json"
        monkeypatch.setattr(ae, "AUTO_EXCLUSION_FILE", self.excl_file)

    @patch("data.auto_exclusions.quarantine_cache", return_value=None)
    @patch("utils.telegram.send_message", return_value=True)
    def test_fresh_data_passes(self, mock_tg, mock_quar):
        from data.ingest import verify_ingest_freshness
        result = verify_ingest_freshness(self._fresh_data(), config=self._cfg())
        assert result is True
        mock_tg.assert_not_called()

    @patch("data.auto_exclusions.quarantine_cache", return_value=None)
    @patch("utils.telegram.send_message", return_value=True)
    def test_few_stale_auto_excluded(self, mock_tg, mock_quar):
        """1-2 stale out of many fresh → auto-exclude, return True."""
        from data.ingest import verify_ingest_freshness
        data = self._mixed_data(fresh_count=18, stale_count=2)
        result = verify_ingest_freshness(data, config=self._cfg(), market_id="sp500")
        assert result is True  # Pipeline continues
        # Stale tickers should be removed from data dict
        assert len(data) == 18  # Only fresh remain
        assert "STALE0" not in data
        assert "STALE1" not in data
        # Telegram alert sent
        mock_tg.assert_called_once()
        alert_text = mock_tg.call_args[0][0]
        assert "AUTO-EXCLUDED" in alert_text

    @patch("utils.telegram.send_message", return_value=True)
    def test_all_stale_raises_systemic(self, mock_tg):
        """ALL tickers stale → systemic error, raises RuntimeError."""
        from data.ingest import verify_ingest_freshness
        data = self._stale_data(10)
        with pytest.raises(RuntimeError, match="SYSTEMIC"):
            verify_ingest_freshness(data, config=self._cfg(halt=True), market_id="sp500")

    @patch("utils.telegram.send_message", return_value=True)
    def test_all_stale_no_halt_returns_false(self, mock_tg):
        """ALL stale + halt=False → returns False."""
        from data.ingest import verify_ingest_freshness
        data = self._stale_data(10)
        result = verify_ingest_freshness(data, config=self._cfg(halt=False), market_id="sp500")
        assert result is False

    @patch("utils.telegram.send_message", return_value=True)
    def test_high_pct_stale_is_systemic(self, mock_tg):
        """More than 5% stale in large universe → systemic."""
        from data.ingest import verify_ingest_freshness
        # 25 tickers, 5 stale = 20% → systemic
        data = self._mixed_data(fresh_count=20, stale_count=5)
        with pytest.raises(RuntimeError, match="SYSTEMIC"):
            verify_ingest_freshness(data, config=self._cfg(halt=True), market_id="sp500")

    @patch("data.auto_exclusions.quarantine_cache", return_value=None)
    @patch("utils.telegram.send_message", return_value=True)
    def test_auto_exclusion_written_to_file(self, mock_tg, mock_quar):
        """Auto-excluded tickers are persisted to the exclusion file."""
        from data.ingest import verify_ingest_freshness
        from data.auto_exclusions import get_excluded_tickers
        data = self._mixed_data(fresh_count=20, stale_count=1)
        verify_ingest_freshness(data, config=self._cfg(), market_id="sp500")
        excluded = get_excluded_tickers("sp500")
        assert "STALE0" in excluded

    @patch("data.auto_exclusions.quarantine_cache", return_value=None)
    @patch("utils.telegram.send_message", return_value=True)
    def test_small_universe_uses_auto_exclude(self, mock_tg, mock_quar):
        """Small universe (<20 tickers) with 2 stale → auto-exclude (not systemic)."""
        from data.ingest import verify_ingest_freshness
        data = self._mixed_data(fresh_count=8, stale_count=2)
        result = verify_ingest_freshness(data, config=self._cfg(), market_id="sp500")
        assert result is True
        assert len(data) == 8  # stale removed

    @patch("data.auto_exclusions.quarantine_cache", return_value=None)
    @patch("utils.telegram.send_message", side_effect=Exception("no network"))
    def test_telegram_failure_non_fatal(self, mock_tg, mock_quar):
        """Telegram failure doesn't prevent auto-exclusion or pipeline continuation."""
        from data.ingest import verify_ingest_freshness
        data = self._mixed_data(fresh_count=20, stale_count=1)
        result = verify_ingest_freshness(data, config=self._cfg(), market_id="sp500")
        assert result is True  # Still succeeds even if Telegram fails


# ── Universe rebuild tests ───────────────────────────────────

class TestEnsureUniverseCurrent:
    """Test universe/builder.ensure_universe_current."""

    @pytest.fixture(autouse=True)
    def mock_exclusion_file(self, tmp_path, monkeypatch):
        import data.auto_exclusions as ae
        self.excl_file = tmp_path / "auto_excluded_tickers.json"
        monkeypatch.setattr(ae, "AUTO_EXCLUSION_FILE", self.excl_file)

    def _config(self, exclusions=None):
        return {
            "market": "sp500",
            "universe": {
                "method": "top_liquid",
                "top_n": 200,
                "min_median_daily_value": 5000000,
                "min_price": 5.0,
                "min_market_cap": 2000000000,
                "exclusions": exclusions or [],
            },
        }

    @patch("universe.builder.load_universe")
    def test_no_change_returns_false(self, mock_load):
        from universe.builder import ensure_universe_current
        mock_load.return_value = {
            "metadata": {"filters": {"exclusions": ["MMC"]}},
            "tickers": ["AAPL", "MSFT"],
            "details": [],
        }
        result = ensure_universe_current(self._config(exclusions=["MMC"]))
        assert result is False  # No rebuild needed

    @patch("universe.builder.load_universe")
    def test_new_exclusion_triggers_rebuild(self, mock_load, tmp_path, monkeypatch):
        from universe.builder import ensure_universe_current
        import universe.builder as ub
        monkeypatch.setattr(ub, "PROCESSED_DIR", tmp_path)
        (tmp_path / "sp500").mkdir()

        mock_load.return_value = {
            "metadata": {"filters": {"exclusions": []}, "final_count": 3},
            "tickers": ["AAPL", "MSFT", "BADCO"],
            "details": [
                {"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "BADCO"},
            ],
        }
        # Add an auto-exclusion
        from data.auto_exclusions import add_exclusion
        add_exclusion("BADCO", "sp500", "stale")

        result = ensure_universe_current(self._config())
        assert result is True  # Rebuild happened

        # Verify BADCO removed from saved universe
        saved = json.loads((tmp_path / "sp500" / "universe.json").read_text())
        assert "BADCO" not in saved["tickers"]
        assert "AAPL" in saved["tickers"]

    @patch("universe.builder.load_universe", side_effect=FileNotFoundError)
    @patch("universe.builder.build_universe")
    def test_missing_universe_triggers_full_build(self, mock_build, mock_load):
        from universe.builder import ensure_universe_current
        mock_build.return_value = ["AAPL"]
        result = ensure_universe_current(self._config())
        assert result is True
        mock_build.assert_called_once()


# ── Download universe auto-exclusion filter test ─────────────

class TestDownloadUniverseAutoExclude:
    """Test that download_universe skips auto-excluded tickers."""

    @pytest.fixture(autouse=True)
    def mock_exclusion_file(self, tmp_path, monkeypatch):
        import data.auto_exclusions as ae
        self.excl_file = tmp_path / "auto_excluded_tickers.json"
        monkeypatch.setattr(ae, "AUTO_EXCLUSION_FILE", self.excl_file)

    @patch("data.ingest.download_ticker")
    def test_auto_excluded_tickers_skipped(self, mock_dl):
        from data.ingest import download_universe
        from data.auto_exclusions import add_exclusion

        add_exclusion("BADTICK", "sp500", "stale")
        mock_dl.return_value = _df_with_latest(_last_weekday_str())

        result = download_universe(
            ["AAPL", "BADTICK", "MSFT"],
            market_id="sp500",
            use_cache=False,
            delay=0,
        )
        # BADTICK should not have been downloaded
        called_tickers = [call.args[0] for call in mock_dl.call_args_list]
        assert "BADTICK" not in called_tickers
        assert "AAPL" in called_tickers
        assert "MSFT" in called_tickers


# ── Recovery script tests ────────────────────────────────────

class TestRecoveryScript:
    """Test scripts/stale_ticker_recovery.py."""

    @pytest.fixture(autouse=True)
    def mock_exclusion_file(self, tmp_path, monkeypatch):
        import data.auto_exclusions as ae
        self.excl_file = tmp_path / "auto_excluded_tickers.json"
        monkeypatch.setattr(ae, "AUTO_EXCLUSION_FILE", self.excl_file)

    @patch("utils.telegram.send_message", return_value=True)
    @patch("data.ingest._fetch_ohlcv")
    @patch("data.ingest._save_cache")
    @patch("data.ingest._normalize_ticker", side_effect=lambda t, m=None: t)
    def test_recovery_success(self, mock_norm, mock_save, mock_fetch, mock_tg):
        """Ticker with fresh data is recovered."""
        from data.auto_exclusions import add_exclusion, get_excluded_tickers
        sys.path.insert(0, str(PROJECT / "scripts"))
        from stale_ticker_recovery import attempt_recovery

        add_exclusion("RECOVERED", "sp500", "was stale", "2026-01-01")
        assert "RECOVERED" in get_excluded_tickers()

        # Mock: ticker now has fresh data
        mock_fetch.return_value = _df_with_latest(_last_weekday_str())

        result = attempt_recovery("sp500")
        assert len(result["recovered"]) == 1
        assert result["recovered"][0]["ticker"] == "RECOVERED"
        assert "RECOVERED" not in get_excluded_tickers()  # Removed!

    @patch("utils.telegram.send_message", return_value=True)
    @patch("data.ingest._fetch_ohlcv")
    @patch("data.ingest._normalize_ticker", side_effect=lambda t, m=None: t)
    def test_recovery_still_stale(self, mock_norm, mock_fetch, mock_tg):
        """Ticker still stale stays excluded."""
        from data.auto_exclusions import add_exclusion, get_excluded_tickers
        sys.path.insert(0, str(PROJECT / "scripts"))
        from stale_ticker_recovery import attempt_recovery

        add_exclusion("STILLBAD", "sp500", "stale")

        # Mock: ticker still stale
        mock_fetch.return_value = _df_with_latest(_old_date_str(10))

        result = attempt_recovery("sp500")
        assert len(result["still_excluded"]) == 1
        assert "STILLBAD" in get_excluded_tickers()  # Still excluded

    @patch("utils.telegram.send_message", return_value=True)
    @patch("data.ingest._fetch_ohlcv")
    @patch("data.ingest._normalize_ticker", side_effect=lambda t, m=None: t)
    def test_recovery_no_data(self, mock_norm, mock_fetch, mock_tg):
        """Ticker with no data stays excluded."""
        from data.auto_exclusions import add_exclusion
        sys.path.insert(0, str(PROJECT / "scripts"))
        from stale_ticker_recovery import attempt_recovery

        add_exclusion("DELISTED", "sp500", "delisted")
        mock_fetch.return_value = pd.DataFrame()

        result = attempt_recovery("sp500")
        assert len(result["still_excluded"]) == 1

    @patch("utils.telegram.send_message", return_value=True)
    def test_recovery_empty_exclusion_list(self, mock_tg):
        """No excluded tickers → empty result, no errors."""
        sys.path.insert(0, str(PROJECT / "scripts"))
        from stale_ticker_recovery import attempt_recovery
        result = attempt_recovery("sp500")
        assert result["recovered"] == []
        assert result["still_excluded"] == []
