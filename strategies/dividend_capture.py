"""
Atlas Dividend Capture Strategy (Stub)
======================================
Placeholder — strategy not yet implemented.
"""

import logging
from typing import Dict, List

import pandas as pd

from strategies.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class DividendCapture(BaseStrategy):
    """Stub dividend capture strategy — returns no signals."""

    NAME = "dividend_capture"

    def __init__(self, config: dict):
        super().__init__(config)

    def generate_signals(self, data: Dict[str, pd.DataFrame], portfolio=None) -> List[Signal]:
        return []
