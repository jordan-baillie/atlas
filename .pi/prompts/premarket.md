---
description: Run premarket workflow — ingest + plan generation for a market
---
Read these skills FIRST before acting:
- atlas-daily: main workflow (READ THIS FIRST: /skill:atlas-daily)
- atlas-state-queries: how to check data freshness, services, broker
- atlas-incident: if any service is down, diagnose using this
- atlas-lessons: critical pitfalls to avoid

Run the atlas-daily pre-market workflow for the ${1:-sp500} market ONLY:
1. Check data freshness using atlas-state-queries
2. Run cli_ingest if data is stale (pass -m ${1:-sp500})
3. Run cli_plan (pass -m ${1:-sp500})

Volatility context: $2
Config context: $3

Summarize the plan and stop — do NOT approve or execute.
Write results to logs/pi-cron-premarket-$(date '+%Y%m%d_%H%M%S').md

NOTE: A Telegram summary is sent automatically after this workflow completes — you do NOT need to send one. Focus on the workflow only.
