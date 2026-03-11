# Data Refresh

Run a full market data refresh and verify data quality. Use when data is stale, after market holidays, or when data quality issues are suspected.

## Instructions
- Time limit: 20 minutes
- This will download fresh market data — ensure the server has internet connectivity
- Verify data quality after download — don't just trust that it worked
- Refresh the dashboard after data update

## Tasks
1. **Check current data freshness**
   ```bash
   ls -la /root/atlas/data/snapshots/ | tail -5
   python3 -c "
   from pathlib import Path
   from datetime import datetime
   snaps = sorted(Path('data/snapshots').iterdir())
   if snaps:
       latest = snaps[-1]
       age_h = (datetime.now().timestamp() - latest.stat().st_mtime) / 3600
       print(f'Latest: {latest.name}, age: {age_h:.1f}h')
   "
   ```

2. **Run data ingestion**
   ```bash
   cd /root/atlas && python3 scripts/ingest_data.py --market sp500
   ```

3. **Verify data quality**
   - Check for missing tickers in the new snapshot
   - Verify price data is reasonable (no $0 prices, no extreme jumps)
   - Compare ticker count to previous snapshot
   ```python
   # Quick quality check
   import json
   from pathlib import Path
   snaps = sorted(Path('data/snapshots').iterdir())
   latest = json.load(open(snaps[-1] / 'metadata.json')) if snaps else {}
   print(f"Tickers: {latest.get('ticker_count', '?')}")
   print(f"Date: {latest.get('date', '?')}")
   ```

4. **Refresh dashboard**
   ```bash
   cd /root/atlas && python3 dashboard/generate_data.py
   ```

5. **Verify dashboard updated**
   - Check timestamp in `dashboard/data/dashboard-data.json`

## Deliverables
- Data freshness: before and after
- Ticker count: previous vs new
- Any data quality issues found
- Dashboard refresh confirmation
