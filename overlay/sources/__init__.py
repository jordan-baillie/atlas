"""
overlay.sources — Data aggregators for the AI overlay layer.

Modules:
    chart_intel  — Technical analysis from cached OHLCV data (no network)
    news         — News + geopolitical + macro snapshot aggregator
"""

from .chart_intel import get_chart_analysis
from .news import get_news_summary

__all__ = ["get_chart_analysis", "get_news_summary"]
