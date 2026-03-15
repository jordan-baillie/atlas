"""Tests for research.rejected_signal_analysis.

All tests run in < 5 s and make zero network calls.

Run with:
    pytest tests/test_rejected_signal_analysis.py -v --tb=short
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from research.rejected_signal_analysis import (  # noqa: E402
    HypotheticalTrade,
    RejectedSignal,
    RejectedSignalAnalyzer,
    RejectionReport,
    _categorize_reason,
)


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

def _make_rejected_signal(
    ticker: str = "AAPL",
    strategy: str = "mean_reversion",
    rejection_reason: str = "Max positions (5) would be exceeded",
    trade_date: str = "2025-01-10",
    entry_price: float = 100.0,
    stop_price: float = 95.0,
    take_profit: float = 110.0,
    position_size: int = 10,
    confidence: float = 0.75,
) -> RejectedSignal:
    return RejectedSignal(
        ticker=ticker,
        strategy=strategy,
        rejection_reason=rejection_reason,
        trade_date=trade_date,
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit=take_profit,
        position_size=position_size,
        position_value=entry_price * position_size,
        risk_amount=abs(entry_price - stop_price) * position_size,
        confidence=confidence,
        rationale="Test rationale",
        features={"rsi": 28.0},
        sector="Technology",
        market_id="sp500",
    )


def _make_plan_json(
    trade_date: str = "2025-01-10",
    market_id: str = "sp500",
    rejected_entries: list | None = None,
) -> dict:
    """Return a minimal plan dict matching the structure in brokers/plan.py."""
    if rejected_entries is None:
        rejected_entries = [
            {
                "ticker": "AAPL",
                "strategy": "mean_reversion",
                "entry_price": 100.0,
                "stop_price": 95.0,
                "take_profit": 110.0,
                "position_size": 10,
                "position_value": 1000.0,
                "risk_amount": 50.0,
                "confidence": 0.75,
                "rationale": "RSI oversold",
                "features": {"rsi": 28.0},
                "sector": "Technology",
                "market_id": market_id,
                "rejection_reason": "Max positions (5) would be exceeded",
            }
        ]
    return {
        "trade_date": trade_date,
        "generated_at": "2025-01-10T08:00:00",
        "market_id": market_id,
        "config_version": "3.0",
        "status": "PENDING_APPROVAL",
        "portfolio_snapshot": {
            "equity": 10000.0,
            "cash": 5000.0,
            "open_positions": 5,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
        },
        "proposed_entries": [],
        "rejected_entries": rejected_entries,
        "proposed_exits": [],
        "total_signals_generated": len(rejected_entries) + 2,
        "risk_summary": {},
        "open_positions": [],
    }


def _make_price_df(
    ticker: str,
    start_date: str = "2025-01-11",
    n_days: int = 30,
    open_: float = 100.0,
    close_: float = 105.0,
    high_: float = 112.0,
    low_: float = 93.0,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame with constant prices."""
    dates = pd.date_range(start=start_date, periods=n_days, freq="B")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high_,
            "low": low_,
            "close": close_,
            "volume": 1_000_000.0,
            "ticker": ticker,
        },
        index=dates,
    )


# ===========================================================================
# 1. RejectedSignal dataclass
# ===========================================================================

class TestRejectedSignal:
    def test_basic_construction(self):
        sig = _make_rejected_signal()
        assert sig.ticker == "AAPL"
        assert sig.strategy == "mean_reversion"
        assert sig.entry_price == 100.0
        assert sig.stop_price == 95.0
        assert sig.take_profit == 110.0
        assert sig.position_size == 10

    def test_rejection_category_position_limit(self):
        sig = _make_rejected_signal(
            rejection_reason="Max positions (5) would be exceeded"
        )
        assert sig.rejection_category == "position_limit"

    def test_rejection_category_low_confidence(self):
        sig = _make_rejected_signal(
            rejection_reason="Confidence 0.620 below threshold 0.650"
        )
        assert sig.rejection_category == "low_confidence"

    def test_rejection_category_vix(self):
        sig = _make_rejected_signal(rejection_reason="VIX 32.5 > 30.0")
        assert sig.rejection_category == "vix_gate"

    def test_rejection_category_macro(self):
        sig = _make_rejected_signal(rejection_reason="FRED macro conditions adverse")
        assert sig.rejection_category == "macro_filter"

    def test_rejection_category_allocation(self):
        sig = _make_rejected_signal(
            rejection_reason="Allocation pool limit reached"
        )
        assert sig.rejection_category == "allocation_pool"

    def test_rejection_category_other(self):
        sig = _make_rejected_signal(rejection_reason="Unknown reason")
        assert sig.rejection_category == "other"

    def test_to_dict_includes_category(self):
        sig = _make_rejected_signal()
        d = sig.to_dict()
        assert "rejection_category" in d
        assert d["ticker"] == "AAPL"
        assert d["rejection_category"] == "position_limit"

    def test_defaults_applied(self):
        sig = RejectedSignal(
            ticker="X",
            strategy="s",
            rejection_reason="r",
            trade_date="2025-01-01",
            entry_price=10.0,
            stop_price=9.0,
            take_profit=None,
            position_size=1,
            position_value=10.0,
            risk_amount=1.0,
            confidence=0.5,
            rationale="",
        )
        assert sig.sector == "Unknown"
        assert sig.market_id == ""
        assert sig.features == {}


# ===========================================================================
# 2. RejectionReport dataclass
# ===========================================================================

class TestRejectionReport:
    def _make_report(self) -> RejectionReport:
        return RejectionReport(
            generated_at="2025-01-10T09:00:00",
            plan_dates=["2025-01-10"],
            total_rejected=3,
            reason_distribution={"Max positions exceeded": 2, "Low confidence": 1},
            category_distribution={"position_limit": 2, "low_confidence": 1},
            strategy_breakdown={
                "mean_reversion": {
                    "total": 2,
                    "reasons": {"Max positions exceeded": 2},
                },
                "momentum_breakout": {
                    "total": 1,
                    "reasons": {"Low confidence": 1},
                },
            },
            hypothetical_trades=[],
            total_hypothetical_pnl=0.0,
            hypothetical_pnl_by_strategy={},
            hypothetical_pnl_by_category={},
            win_rate=0.0,
            signals=[],
        )

    def test_construction(self):
        r = self._make_report()
        assert r.total_rejected == 3
        assert r.plan_dates == ["2025-01-10"]

    def test_to_dict_roundtrip(self):
        r = self._make_report()
        d = r.to_dict()
        assert d["total_rejected"] == 3
        assert d["category_distribution"]["position_limit"] == 2
        # JSON-serialisable (no custom types)
        assert json.dumps(d)


# ===========================================================================
# 3. RejectedSignalAnalyzer.extract_rejected
# ===========================================================================

class TestExtractRejected:
    def test_empty_plans_dir(self, tmp_path):
        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected()
        assert signals == []

    def test_missing_plans_dir(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        analyzer = RejectedSignalAnalyzer(plans_dir=missing)
        signals = analyzer.extract_rejected()
        assert signals == []

    def test_single_plan_one_rejected(self, tmp_path):
        plan = _make_plan_json()
        (tmp_path / "plan_sp500_2025-01-10.json").write_text(json.dumps(plan))

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected()

        assert len(signals) == 1
        sig = signals[0]
        assert sig.ticker == "AAPL"
        assert sig.trade_date == "2025-01-10"
        assert sig.strategy == "mean_reversion"
        assert sig.rejection_reason == "Max positions (5) would be exceeded"

    def test_multiple_plans_aggregated(self, tmp_path):
        for date in ["2025-01-10", "2025-01-11", "2025-01-12"]:
            plan = _make_plan_json(trade_date=date)
            (tmp_path / f"plan_sp500_{date}.json").write_text(json.dumps(plan))

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected()
        assert len(signals) == 3
        dates = {s.trade_date for s in signals}
        assert dates == {"2025-01-10", "2025-01-11", "2025-01-12"}

    def test_date_range_filter(self, tmp_path):
        for date in ["2025-01-10", "2025-01-11", "2025-01-15"]:
            plan = _make_plan_json(trade_date=date)
            (tmp_path / f"plan_sp500_{date}.json").write_text(json.dumps(plan))

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected(
            date_range=("2025-01-10", "2025-01-11")
        )
        assert len(signals) == 2

    def test_explicit_plan_paths(self, tmp_path):
        plan = _make_plan_json()
        p = tmp_path / "plan_sp500_2025-01-10.json"
        p.write_text(json.dumps(plan))

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected(plan_paths=[p])
        assert len(signals) == 1

    def test_malformed_plan_skipped(self, tmp_path):
        (tmp_path / "plan_bad.json").write_text("not json {{{")
        (tmp_path / "plan_sp500_2025-01-10.json").write_text(
            json.dumps(_make_plan_json())
        )

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected()
        assert len(signals) == 1  # bad file silently skipped

    def test_no_rejected_entries(self, tmp_path):
        plan = _make_plan_json(rejected_entries=[])
        (tmp_path / "plan_sp500_2025-01-10.json").write_text(json.dumps(plan))

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected()
        assert signals == []

    def test_null_take_profit_handled(self, tmp_path):
        entry = {
            "ticker": "MSFT",
            "strategy": "trend_following",
            "entry_price": 200.0,
            "stop_price": 190.0,
            "take_profit": None,
            "position_size": 5,
            "position_value": 1000.0,
            "risk_amount": 50.0,
            "confidence": 0.8,
            "rationale": "Test",
            "features": {},
            "sector": "Technology",
            "market_id": "sp500",
            "rejection_reason": "Confidence 0.8 below threshold 0.85",
        }
        plan = _make_plan_json(rejected_entries=[entry])
        (tmp_path / "plan_sp500_2025-01-10.json").write_text(json.dumps(plan))

        analyzer = RejectedSignalAnalyzer(plans_dir=tmp_path)
        signals = analyzer.extract_rejected()
        assert len(signals) == 1
        assert signals[0].take_profit is None
        assert signals[0].ticker == "MSFT"


# ===========================================================================
# 4. RejectedSignalAnalyzer.analyze — empty data
# ===========================================================================

class TestAnalyzeEmpty:
    def test_analyze_empty_signals(self):
        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze([])

        assert report.total_rejected == 0
        assert report.plan_dates == []
        assert report.reason_distribution == {}
        assert report.category_distribution == {}
        assert report.strategy_breakdown == {}
        assert report.hypothetical_trades == []
        assert report.total_hypothetical_pnl == 0.0
        assert report.win_rate == 0.0

    def test_analyze_no_price_data_no_pnl(self):
        sig = _make_rejected_signal()
        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze([sig])

        assert report.total_rejected == 1
        assert report.hypothetical_trades == []
        assert report.total_hypothetical_pnl == 0.0


# ===========================================================================
# 5. Reason distribution & strategy breakdown
# ===========================================================================

class TestReasonDistribution:
    def _signals(self) -> list[RejectedSignal]:
        return [
            _make_rejected_signal(
                ticker="A",
                strategy="mean_reversion",
                rejection_reason="Max positions (5) would be exceeded",
            ),
            _make_rejected_signal(
                ticker="B",
                strategy="mean_reversion",
                rejection_reason="Max positions (5) would be exceeded",
            ),
            _make_rejected_signal(
                ticker="C",
                strategy="momentum_breakout",
                rejection_reason="Confidence 0.620 below threshold 0.650",
            ),
        ]

    def test_reason_distribution(self):
        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze(self._signals())

        assert report.reason_distribution["Max positions (5) would be exceeded"] == 2
        assert report.reason_distribution["Confidence 0.620 below threshold 0.650"] == 1

    def test_category_distribution(self):
        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze(self._signals())

        assert report.category_distribution["position_limit"] == 2
        assert report.category_distribution["low_confidence"] == 1

    def test_strategy_breakdown(self):
        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze(self._signals())

        assert report.strategy_breakdown["mean_reversion"]["total"] == 2
        assert report.strategy_breakdown["momentum_breakout"]["total"] == 1
        reasons_mr = report.strategy_breakdown["mean_reversion"]["reasons"]
        assert reasons_mr["Max positions (5) would be exceeded"] == 2

    def test_plan_dates_collected(self):
        sigs = [
            _make_rejected_signal(trade_date="2025-01-10"),
            _make_rejected_signal(trade_date="2025-01-11"),
            _make_rejected_signal(trade_date="2025-01-10"),  # duplicate
        ]
        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze(sigs)
        assert report.plan_dates == ["2025-01-10", "2025-01-11"]  # sorted, unique


# ===========================================================================
# 6. Hypothetical P&L (TP / SL / expired scenarios)
# ===========================================================================

class TestHypotheticalPnl:
    """
    Scenarios tested:
      TP  — price rises above take-profit within hold period.
      SL  — price falls below stop-loss within hold period.
      EXP — neither fires; exit at final close.
      MISSING — ticker absent from price_data; no trade returned.
    """

    def _make_signal_with_prices(
        self,
        ticker: str = "AAPL",
        entry: float = 100.0,
        stop: float = 95.0,
        tp: float = 110.0,
        size: int = 10,
        date: str = "2025-01-10",
    ) -> tuple[RejectedSignal, dict[str, pd.DataFrame]]:
        sig = _make_rejected_signal(
            ticker=ticker,
            entry_price=entry,
            stop_price=stop,
            take_profit=tp,
            position_size=size,
            trade_date=date,
        )
        return sig, {}

    # --- take_profit scenario ---

    def test_take_profit_hit(self):
        """High >= take_profit on day 3 → exit at TP, positive P&L."""
        sig = _make_rejected_signal(
            ticker="AAPL",
            entry_price=100.0,
            stop_price=95.0,
            take_profit=110.0,
            position_size=10,
            trade_date="2025-01-10",
        )
        # Build price data where high hits 110 on day 3
        dates = pd.date_range(start="2025-01-11", periods=5, freq="B")
        df = pd.DataFrame(
            {
                "open": [100.0] * 5,
                "high": [105.0, 107.0, 111.0, 112.0, 113.0],  # TP=110 hit day 3
                "low": [99.0, 98.0, 99.0, 99.0, 99.0],
                "close": [103.0, 106.0, 110.0, 111.0, 112.0],
                "volume": [1e6] * 5,
                "ticker": "AAPL",
            },
            index=dates,
        )
        price_data = {"AAPL": df}

        analyzer = RejectedSignalAnalyzer()
        trades, total, by_strat, by_cat = analyzer.compute_hypothetical_pnl(
            [sig], price_data
        )

        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "take_profit"
        assert t.exit_price == 110.0
        assert t.hold_days == 3
        # P&L = (110 - 100) * 10 = 100
        assert t.pnl == pytest.approx(100.0)
        assert t.pnl_pct == pytest.approx(10.0)
        assert total == pytest.approx(100.0)
        assert by_strat.get("mean_reversion", 0.0) == pytest.approx(100.0)

    # --- stop_loss scenario ---

    def test_stop_loss_hit(self):
        """Low <= stop_price on day 2 → exit at SL, negative P&L."""
        sig = _make_rejected_signal(
            ticker="MSFT",
            entry_price=200.0,
            stop_price=190.0,
            take_profit=220.0,
            position_size=5,
            trade_date="2025-02-05",
        )
        dates = pd.date_range(start="2025-02-06", periods=5, freq="B")
        df = pd.DataFrame(
            {
                "open": [200.0] * 5,
                "high": [202.0, 201.0, 203.0, 205.0, 207.0],
                "low": [198.0, 189.0, 195.0, 197.0, 199.0],  # SL=190 hit day 2
                "close": [201.0, 192.0, 196.0, 198.0, 200.0],
                "volume": [1e6] * 5,
                "ticker": "MSFT",
            },
            index=dates,
        )
        price_data = {"MSFT": df}

        analyzer = RejectedSignalAnalyzer()
        trades, total, by_strat, by_cat = analyzer.compute_hypothetical_pnl(
            [sig], price_data
        )

        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "stop_loss"
        assert t.exit_price == 190.0
        assert t.hold_days == 2
        # P&L = (190 - 200) * 5 = -50
        assert t.pnl == pytest.approx(-50.0)
        assert t.pnl_pct == pytest.approx(-5.0)
        assert total == pytest.approx(-50.0)

    # --- expired scenario ---

    def test_expired_no_tp_no_sl(self):
        """Neither TP nor SL fires → exit at last close after max_hold_days."""
        sig = _make_rejected_signal(
            ticker="GOOG",
            entry_price=150.0,
            stop_price=140.0,
            take_profit=170.0,
            position_size=4,
            trade_date="2025-03-01",
        )
        # Price stays between SL and TP the whole time
        dates = pd.date_range(start="2025-03-02", periods=10, freq="B")
        df = pd.DataFrame(
            {
                "open": [150.0] * 10,
                "high": [155.0] * 10,  # below TP=170
                "low": [145.0] * 10,   # above SL=140
                "close": [152.0] * 9 + [158.0],  # last close = 158
                "volume": [1e6] * 10,
                "ticker": "GOOG",
            },
            index=dates,
        )
        price_data = {"GOOG": df}

        analyzer = RejectedSignalAnalyzer()
        trades, total, _, _ = analyzer.compute_hypothetical_pnl(
            [sig], price_data, max_hold_days=10
        )

        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "expired"
        assert t.exit_price == pytest.approx(158.0)
        # P&L = (158 - 150) * 4 = 32
        assert t.pnl == pytest.approx(32.0)

    # --- missing ticker ---

    def test_missing_ticker_skipped(self):
        """Ticker absent from price_data → no trade simulated."""
        sig = _make_rejected_signal(ticker="UNKNOWN")
        price_data: dict = {}  # empty

        analyzer = RejectedSignalAnalyzer()
        trades, total, _, _ = analyzer.compute_hypothetical_pnl([sig], price_data)

        assert trades == []
        assert total == 0.0

    # --- TP checked before SL on same bar ---

    def test_tp_wins_when_same_bar_hits_both(self):
        """On a single bar where both TP and SL would trigger, TP is preferred."""
        sig = _make_rejected_signal(
            ticker="META",
            entry_price=100.0,
            stop_price=90.0,
            take_profit=105.0,
            position_size=10,
            trade_date="2025-04-01",
        )
        dates = pd.date_range(start="2025-04-02", periods=1, freq="B")
        # Same bar: high=110 (>TP=105), low=88 (<SL=90)
        df = pd.DataFrame(
            {
                "open": [100.0],
                "high": [110.0],
                "low": [88.0],
                "close": [100.0],
                "volume": [1e6],
                "ticker": "META",
            },
            index=dates,
        )
        price_data = {"META": df}

        analyzer = RejectedSignalAnalyzer()
        trades, _, _, _ = analyzer.compute_hypothetical_pnl([sig], price_data)

        assert len(trades) == 1
        assert trades[0].exit_reason == "take_profit"

    # --- no take_profit (None) → only SL and expired ---

    def test_no_take_profit_expires(self):
        """Signal with take_profit=None goes through to expiry."""
        sig = RejectedSignal(
            ticker="SPY",
            strategy="trend_following",
            rejection_reason="Max positions exceeded",
            trade_date="2025-05-01",
            entry_price=400.0,
            stop_price=390.0,
            take_profit=None,
            position_size=2,
            position_value=800.0,
            risk_amount=20.0,
            confidence=0.7,
            rationale="",
        )
        dates = pd.date_range(start="2025-05-02", periods=5, freq="B")
        df = pd.DataFrame(
            {
                "open": [400.0] * 5,
                "high": [405.0] * 5,  # no TP to check
                "low": [395.0] * 5,   # above SL=390
                "close": [402.0, 403.0, 404.0, 405.0, 406.0],
                "volume": [1e6] * 5,
                "ticker": "SPY",
            },
            index=dates,
        )
        price_data = {"SPY": df}

        analyzer = RejectedSignalAnalyzer()
        trades, _, _, _ = analyzer.compute_hypothetical_pnl([sig], price_data, max_hold_days=5)

        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "expired"
        assert t.exit_price == pytest.approx(406.0)

    # --- win_rate in analyze ---

    def test_win_rate_calculation(self):
        """2 winning trades, 1 losing → win_rate ~ 66.7%."""
        sigs = [
            _make_rejected_signal(ticker="A", trade_date="2025-01-10", entry_price=100.0, take_profit=110.0, stop_price=90.0, position_size=1),
            _make_rejected_signal(ticker="B", trade_date="2025-01-10", entry_price=100.0, take_profit=110.0, stop_price=90.0, position_size=1),
            _make_rejected_signal(ticker="C", trade_date="2025-01-10", entry_price=100.0, take_profit=110.0, stop_price=90.0, position_size=1),
        ]
        dates = pd.date_range(start="2025-01-11", periods=5, freq="B")
        # A and B hit TP, C hits SL
        price_data = {
            "A": pd.DataFrame({"open": 100, "high": 115.0, "low": 95.0, "close": 112.0, "volume": 1e6, "ticker": "A"}, index=dates),
            "B": pd.DataFrame({"open": 100, "high": 115.0, "low": 95.0, "close": 112.0, "volume": 1e6, "ticker": "B"}, index=dates),
            "C": pd.DataFrame({"open": 100, "high": 100.0, "low": 88.0, "close": 89.0, "volume": 1e6, "ticker": "C"}, index=dates),
        }

        analyzer = RejectedSignalAnalyzer()
        report = analyzer.analyze(sigs, price_data=price_data)

        assert report.win_rate == pytest.approx(66.7, abs=0.1)
        assert len(report.hypothetical_trades) == 3

    # --- pnl_by_category ---

    def test_pnl_by_category(self):
        """P&L is correctly bucketed by rejection category."""
        sig = _make_rejected_signal(
            ticker="AAPL",
            rejection_reason="Max positions (5) would be exceeded",
            entry_price=100.0,
            take_profit=120.0,
            stop_price=90.0,
            position_size=10,
            trade_date="2025-01-10",
        )
        dates = pd.date_range(start="2025-01-11", periods=3, freq="B")
        df = pd.DataFrame(
            {"open": 100.0, "high": 125.0, "low": 99.0, "close": 120.0, "volume": 1e6, "ticker": "AAPL"},
            index=dates,
        )
        price_data = {"AAPL": df}

        analyzer = RejectedSignalAnalyzer()
        _, _, _, by_cat = analyzer.compute_hypothetical_pnl([sig], price_data)

        assert "position_limit" in by_cat
        # (120 - 100) * 10 = 200
        assert by_cat["position_limit"] == pytest.approx(200.0)


# ===========================================================================
# 7. format_telegram
# ===========================================================================

class TestFormatTelegram:
    def _make_full_report(self) -> RejectionReport:
        return RejectionReport(
            generated_at="2025-01-10T09:00:00",
            plan_dates=["2025-01-10", "2025-01-11"],
            total_rejected=4,
            reason_distribution={
                "Max positions exceeded": 3,
                "Low confidence": 1,
            },
            category_distribution={
                "position_limit": 3,
                "low_confidence": 1,
            },
            strategy_breakdown={
                "mean_reversion": {"total": 3, "reasons": {"Max positions exceeded": 3}},
                "momentum_breakout": {"total": 1, "reasons": {"Low confidence": 1}},
            },
            hypothetical_trades=[
                HypotheticalTrade(
                    ticker="AAPL",
                    strategy="mean_reversion",
                    trade_date="2025-01-10",
                    entry_price=100.0,
                    exit_price=110.0,
                    exit_reason="take_profit",
                    hold_days=3,
                    pnl=100.0,
                    pnl_pct=10.0,
                )
            ],
            total_hypothetical_pnl=100.0,
            hypothetical_pnl_by_strategy={"mean_reversion": 100.0},
            hypothetical_pnl_by_category={"position_limit": 100.0},
            win_rate=100.0,
            signals=[],
        )

    def test_contains_header(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert "Rejected Signal Analysis" in text

    def test_contains_total_count(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert "4" in text

    def test_contains_dates(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert "2025-01-10" in text
        assert "2025-01-11" in text

    def test_contains_pnl(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert "100.00" in text

    def test_no_price_data_message(self):
        report = RejectionReport(
            generated_at="2025-01-10T09:00:00",
            plan_dates=["2025-01-10"],
            total_rejected=1,
            reason_distribution={"Max positions exceeded": 1},
            category_distribution={"position_limit": 1},
            strategy_breakdown={"mean_reversion": {"total": 1, "reasons": {}}},
            hypothetical_trades=[],
            total_hypothetical_pnl=0.0,
            hypothetical_pnl_by_strategy={},
            hypothetical_pnl_by_category={},
            win_rate=0.0,
            signals=[],
        )
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(report)
        assert "No price data" in text

    def test_output_is_string(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert isinstance(text, str)
        assert len(text) > 0

    def test_categories_present(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert "position_limit" in text
        assert "low_confidence" in text

    def test_strategy_breakdown_present(self):
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(self._make_full_report())
        assert "mean_reversion" in text
        assert "momentum_breakout" in text

    def test_empty_report(self):
        report = RejectionReport(
            generated_at="2025-01-10T09:00:00",
            plan_dates=[],
            total_rejected=0,
            reason_distribution={},
            category_distribution={},
            strategy_breakdown={},
            hypothetical_trades=[],
            total_hypothetical_pnl=0.0,
            hypothetical_pnl_by_strategy={},
            hypothetical_pnl_by_category={},
            win_rate=0.0,
            signals=[],
        )
        analyzer = RejectedSignalAnalyzer()
        text = analyzer.format_telegram(report)
        assert isinstance(text, str)
        assert "0" in text  # total_rejected = 0


# ===========================================================================
# 8. save_report
# ===========================================================================

class TestSaveReport:
    def _make_minimal_report(self) -> RejectionReport:
        return RejectionReport(
            generated_at="2025-01-10T09:00:00",
            plan_dates=["2025-01-10"],
            total_rejected=1,
            reason_distribution={"Max positions exceeded": 1},
            category_distribution={"position_limit": 1},
            strategy_breakdown={"mean_reversion": {"total": 1, "reasons": {}}},
            hypothetical_trades=[],
            total_hypothetical_pnl=0.0,
            hypothetical_pnl_by_strategy={},
            hypothetical_pnl_by_category={},
            win_rate=0.0,
            signals=[_make_rejected_signal()],
        )

    def test_save_to_explicit_path(self, tmp_path):
        output = tmp_path / "report.json"
        analyzer = RejectedSignalAnalyzer()
        path = analyzer.save_report(self._make_minimal_report(), output_path=output)

        assert path == output
        assert output.exists()
        data = json.loads(output.read_text())
        assert data["total_rejected"] == 1

    def test_save_auto_path(self, tmp_path):
        analyzer = RejectedSignalAnalyzer(reports_dir=tmp_path)
        path = analyzer.save_report(self._make_minimal_report())

        assert path.exists()
        assert "rejected_signal_analysis_" in path.name
        assert path.suffix == ".json"
        data = json.loads(path.read_text())
        assert data["total_rejected"] == 1

    def test_auto_creates_reports_dir(self, tmp_path):
        reports_dir = tmp_path / "nested" / "reports"
        assert not reports_dir.exists()

        analyzer = RejectedSignalAnalyzer(reports_dir=reports_dir)
        path = analyzer.save_report(self._make_minimal_report())

        assert reports_dir.exists()
        assert path.exists()

    def test_explicit_path_creates_parent_dir(self, tmp_path):
        output = tmp_path / "sub" / "dir" / "report.json"
        analyzer = RejectedSignalAnalyzer()
        path = analyzer.save_report(self._make_minimal_report(), output_path=output)

        assert path == output
        assert output.exists()

    def test_saved_json_is_valid(self, tmp_path):
        output = tmp_path / "report.json"
        analyzer = RejectedSignalAnalyzer()
        analyzer.save_report(self._make_minimal_report(), output_path=output)

        raw = output.read_text()
        data = json.loads(raw)  # must not raise
        # Check key fields
        assert "plan_dates" in data
        assert "category_distribution" in data
        assert "signals" in data
        assert isinstance(data["signals"], list)

    def test_signals_serialised_with_category(self, tmp_path):
        output = tmp_path / "report.json"
        analyzer = RejectedSignalAnalyzer()
        analyzer.save_report(self._make_minimal_report(), output_path=output)

        data = json.loads(output.read_text())
        sig_dict = data["signals"][0]
        assert "rejection_category" in sig_dict
        assert sig_dict["rejection_category"] == "position_limit"


# ===========================================================================
# 9. _categorize_reason (module-level helper)
# ===========================================================================

class TestCategorizeReason:
    @pytest.mark.parametrize(
        "reason, expected",
        [
            ("Max positions (5) would be exceeded", "position_limit"),
            ("Max positions limit", "position_limit"),
            ("Confidence 0.60 below threshold 0.65", "low_confidence"),
            ("VIX 35.0 > 30.0", "vix_gate"),
            ("FRED macro conditions adverse", "macro_filter"),
            ("yield curve inverted", "macro_filter"),
            ("Allocation pool capacity reached", "allocation_pool"),
            ("Pool limit for trend_following", "allocation_pool"),
            ("Sector concentration limit hit", "sector_limit"),
            ("Risk check failed: drawdown", "risk_limit"),
            ("Totally unknown reason", "other"),
        ],
    )
    def test_categorize(self, reason, expected):
        assert _categorize_reason(reason) == expected


# ===========================================================================
# 10. Integration: full round-trip
# ===========================================================================

class TestIntegration:
    """End-to-end: write plan files, extract, analyze (with price data), save."""

    def test_full_pipeline(self, tmp_path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        reports_dir = tmp_path / "reports"

        # Write two plan files
        for date in ["2025-06-01", "2025-06-02"]:
            plan = _make_plan_json(
                trade_date=date,
                rejected_entries=[
                    {
                        "ticker": "AAPL",
                        "strategy": "momentum_breakout",
                        "entry_price": 180.0,
                        "stop_price": 170.0,
                        "take_profit": 200.0,
                        "position_size": 5,
                        "position_value": 900.0,
                        "risk_amount": 50.0,
                        "confidence": 0.70,
                        "rationale": "Breakout",
                        "features": {},
                        "sector": "Technology",
                        "market_id": "sp500",
                        "rejection_reason": "Max positions (5) would be exceeded",
                    }
                ],
            )
            (plans_dir / f"plan_sp500_{date}.json").write_text(json.dumps(plan))

        # Price data: AAPL hits TP after entry date
        dates_p1 = pd.date_range(start="2025-06-02", periods=10, freq="B")
        dates_p2 = pd.date_range(start="2025-06-03", periods=10, freq="B")
        df_p1 = pd.DataFrame(
            {"open": 180.0, "high": 201.0, "low": 179.0, "close": 200.0, "volume": 1e6, "ticker": "AAPL"},
            index=dates_p1,
        )
        price_data = {"AAPL": df_p1}

        analyzer = RejectedSignalAnalyzer(plans_dir=plans_dir, reports_dir=reports_dir)
        signals = analyzer.extract_rejected()
        assert len(signals) == 2

        report = analyzer.analyze(signals, price_data=price_data)
        assert report.total_rejected == 2
        assert report.category_distribution.get("position_limit") == 2
        assert len(report.hypothetical_trades) >= 1  # at least one ticker matched

        # format_telegram returns non-empty string
        text = analyzer.format_telegram(report)
        assert len(text) > 50

        # save_report persists the file
        saved = analyzer.save_report(report)
        assert saved.exists()
        data = json.loads(saved.read_text())
        assert data["total_rejected"] == 2
