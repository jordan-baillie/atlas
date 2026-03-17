---
description: Run system health audit and send Saturday summary via Telegram
---
Read the atlas-healthz skill first: /skill:atlas-healthz

Run a full system health audit for the ${1:-sp500} market:

1. Run the health check job:
   Use atlas_jobs_run with job=health_check to generate the health artifact.
   Wait for it to complete, then locate the output JSON in the artifacts directory.

2. Summarize the results:
   Use atlas_artifacts_summarize on the health check JSON output.

3. Produce the report covering:
   - Overall verdict (PASS / WARN / FAIL)
   - Section-by-section status: services, broker, data freshness, config, cron, research, disk, backtest performance
   - Any failures or warnings with their specific error messages
   - Recommended fixes for anything flagged

4. Compare against last week (if available):
   Check research/reports/ for the most recent health-report-*.md
   If found, highlight any metrics that degraded or improved since last week.

5. Flag anything needing attention before Monday's premarket:
   - Data staleness issues
   - Service failures
   - Config drift
   - Disk space warnings
   - Broker connectivity

6. Save the report:
   Write a structured markdown summary to research/reports/health-report-$(date '+%Y-%m-%d').md

7. Send via Telegram:
   python3 -c "import sys; sys.path.insert(0,'/root/atlas'); from utils.telegram import send_message; send_message('''HEALTH_SUMMARY_HERE''')"
   Keep the Telegram message under 4000 chars. Use <b>bold</b> for section headers.
   Lead with the overall verdict emoji: ✅ PASS / ⚠️ WARN / ❌ FAIL
