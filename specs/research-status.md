# Research Status Report

Generate a concise research status briefing.

## Instructions
- Time limit: 10 minutes.
- Read data files directly, do NOT run backtests.
- Focus on actionable insights, not raw data dumps.

## Tasks
1. Read `research/journal.json` — count today's experiments, pass rate, best Sharpe
2. Read `research/queue.json` — queue depth and what's pending
3. Read the autoresearch heartbeat — current cycle, runtime
4. Read leaderboard from `research/best/` — top 3 strategies by Sharpe
5. Check for any strategies stuck (>20 consecutive fails in journal)

## Deliverables
- One concise summary message covering all the above
- Flag any strategies needing attention
- Recommend next research priority
