# Data & Cache — Incident Cookbook

Covers: stale data cache, corrupted parquet files, wrong tickers (ASX contamination).

---

## Pattern 9: Stale Data Cache

**Symptoms:** Backtest results look wrong, "stale data" warnings, cache files >24h old.

**Fix:**
```bash
# Check cache age
ls -lt data/cache/sp500/ | head -5

# Refresh
cd /root/atlas && python3 scripts/cli.py -m sp500 ingest
```

**Verify:**
```bash
ls -lt data/cache/sp500/ | head -5
# Timestamps should now be within the last hour
```

---

## Pattern 10: Corrupted Parquet Files

**Symptoms:** `ArrowInvalid` or `ParquetException` errors during backtest or ingest.

**Fix:**
```bash
# Find corrupted files
cd /root/atlas
python3 -c "
import pandas as pd
from pathlib import Path
for f in Path('data/cache/sp500').glob('*.parquet'):
    try: pd.read_parquet(f)
    except: print(f'CORRUPT: {f}')
"

# Delete corrupted and re-ingest
rm <corrupted_files>
python3 scripts/cli.py -m sp500 ingest
```

**Verify:**
```bash
cd /root/atlas
python3 -c "
import pandas as pd
from pathlib import Path
errors = []
for f in Path('data/cache/sp500').glob('*.parquet'):
    try: pd.read_parquet(f)
    except Exception as e: errors.append(str(f))
print(f'Corrupt files: {len(errors)}')
"
# Should print: Corrupt files: 0
```

---

## Pattern 11: Wrong Tickers in Cache (ASX Contamination)

**Symptoms:** Backtest includes unexpected tickers, US tickers with `.AX` suffix.

**Root cause:** Lesson #25 — earlier pipeline bug left US tickers in ASX cache.

**Fix:** Filter loaded tickers against `market.get_formatted_tickers()`:
```python
# In any code loading cache data, filter tickers:
market = get_market('sp500')
valid_tickers = set(market.get_formatted_tickers())
df = df[df['ticker'].isin(valid_tickers)]
```

**Verify:**
```bash
cd /root/atlas
python3 -c "
import pandas as pd
from pathlib import Path
# Check for .AX tickers in sp500 cache
for f in Path('data/cache/sp500').glob('*.parquet'):
    df = pd.read_parquet(f)
    if 'ticker' in df.columns:
        ax = df[df['ticker'].str.endswith('.AX')]
        if len(ax): print(f'Contamination in {f}: {ax.ticker.unique()[:5]}')
print('Done')
"
```
