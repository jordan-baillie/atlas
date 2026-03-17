---
description: Run post-close workflow — EOD settlement + dashboard refresh for a market
---
Read these skills FIRST before acting:
- atlas-daily: main workflow (READ THIS FIRST: /skill:atlas-daily)
- atlas-state-queries: how to check equity, broker, settlement
- atlas-incident: if any service is down or settlement fails, diagnose using this
- atlas-lessons: critical pitfalls to avoid

Run the atlas-daily post-close workflow for the ${1:-sp500} market:
1. Run cli_eod_settlement (pass -m ${1:-sp500})
2. Run dashboard_generate_data

Summarize any exits triggered and the final equity snapshot.
Write results to logs/pi-cron-postclose-$(date '+%Y%m%d_%H%M%S').md

NOTE: A Telegram summary is sent automatically after this workflow completes — you do NOT need to send one. Focus on the workflow only.
