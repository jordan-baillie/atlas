"""Tests for Hong Kong (SEHK) market integration."""
import sys, json
from pathlib import Path
PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

# Test HK market profile
from markets.hk import HKMarket
from markets import get_market, list_markets

def test_hk_registered():
    assert "hk" in list_markets()
    m = get_market("hk")
    assert m.market_id == "hk"

def test_hk_properties():
    m = HKMarket()
    assert m.currency == "HKD"
    assert m.country == "HK"
    assert m.yfinance_suffix == ".HK"
    assert m.benchmark_ticker == "2800.HK"
    assert m.trading_days_per_year == 247

def test_hk_ticker_formatting():
    m = HKMarket()
    assert m.format_ticker("0700") == "0700.HK"
    assert m.format_ticker("0005") == "0005.HK"
    assert m.format_ticker("0700.HK") == "0700.HK"  # idempotent
    assert m.strip_suffix("0700.HK") == "0700"

def test_hk_universe():
    m = HKMarket()
    tickers = m.get_universe_tickers()
    assert len(tickers) >= 100
    assert "0700" in tickers  # Tencent
    assert "9988" in tickers  # Alibaba
    assert "0005" in tickers  # HSBC
    # Verify leading zeros preserved
    assert all(isinstance(t, str) for t in tickers)
    # No duplicates
    assert len(tickers) == len(set(tickers))

def test_hk_fees():
    m = HKMarket()
    fees = m.default_fees
    assert fees.commission_per_trade == 18.0
    assert fees.commission_pct == 0.0005
    assert fees.min_position_value == 2000.0

def test_hk_trading_hours():
    m = HKMarket()
    h = m.trading_hours
    assert h.timezone == "Asia/Hong_Kong"
    assert h.market_open == "09:30"
    assert h.market_close == "16:00"

def test_hk_config_loads():
    cfg_path = PROJECT / "config" / "active" / "hk.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["market"] == "hk"
    assert cfg["trading"]["broker"] == "ibkr"
    assert cfg["fees"]["commission_per_trade"] == 18.0
    assert cfg["risk"]["starting_equity"] == 30000

def test_hk_ibkr_mapper():
    from brokers.ibkr.mapper import strip_suffix, to_atlas, to_conid_lookup, get_exchange, get_currency
    assert strip_suffix("0700.HK", "hk") == "0700"
    assert to_atlas("0700", "SEHK") == "0700.HK"
    lookup = to_conid_lookup("0700.HK", "hk")
    assert lookup["symbol"] == "0700"
    assert lookup["exchange"] == "SEHK"
    assert get_exchange("hk") == "SEHK"
    assert get_currency("hk") == "HKD"

def test_hk_backtest_defaults():
    m = HKMarket()
    d = m.get_backtest_defaults()
    assert d["train_window_days"] == 247
    assert d["test_window_days"] == 61  # 247//4
    assert d["step_days"] == 20  # 247//12

def test_paper_engine_removed():
    """Verify paper trading engine has been fully removed."""
    assert not (PROJECT / "paper_engine").exists(), "paper_engine/ directory should not exist"
    assert not (PROJECT / "brokers" / "paper.py").exists(), "brokers/paper.py should not exist"

def test_hk_cron_schedule():
    m = HKMarket()
    sched = m.get_cron_schedule()
    assert sched["exchange_tz"] == "Asia/Hong_Kong"
    assert sched["operator_tz"] == "Australia/Brisbane"

if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
