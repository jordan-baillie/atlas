"""Atlas AI Overlay module (Layer 3).

Three-component AI tightening layer that sits on top of the quantitative
regime model.  Can only reduce exposure — never increase beyond the regime
default.

Components
----------
overlay.engine     — Claude-powered decision maker
overlay.sources    — Data aggregators: charts and news
overlay.evaluator  — Weekly self-evaluation of past decisions
overlay.cron       — Daily cron entry point
"""
from overlay.engine import run_overlay, OverlayDecision

__all__ = ["run_overlay", "OverlayDecision"]
