# Weekly Re-optimization Check

Assess whether any strategies have degraded and need parameter re-optimization. Run weekly (Sunday) or ad-hoc when performance concerns arise.

## Instructions
- Time limit: 30 minutes
- Read data from existing files — do NOT run full backtests (too slow for this spec)
- Focus on detecting degradation, not fixing it
- If a strategy needs re-optimization, queue it via the research system — don't do it here

## Tasks
1. Read the current leaderboard: `python3 -c "from research.loop import leaderboard; print(leaderboard())"`
2. Read the research journal for the past 7 days:
   ```python
   import json
   from datetime import datetime, timedelta
   journal = json.load(open('research/journal.json'))
   week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
   recent = [e for e in journal if e.get('timestamp', '') >= week_ago]
   ```
3. For each strategy in the leaderboard, check:
   - Has Sharpe declined from best-ever? (compare `research/best/<strategy>.json` metrics)
   - Has pass rate dropped below 30% this week?
   - Are there >15 consecutive fails in recent journal entries?
4. Check the autoresearch cycle count and runtime — is it making progress?
5. Check queue depth — is work flowing through?

## Deliverables
- Strategy health table: strategy | current Sharpe | best Sharpe | status (healthy/degraded/stale)
- Recommendations: which strategies need attention and what kind (re-sweep, deeper research, retire)
- Queue recommendation: what to prioritize next week
