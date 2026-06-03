#!/usr/bin/env python3
"""Show the rapid validate->live pipeline candidate board.

Usage: python3 scripts/pipeline_status.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research import pipeline  # noqa: E402

if __name__ == "__main__":
    print(pipeline.format_status())
