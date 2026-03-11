# Weekly Performance Report

Generate a comprehensive performance briefing covering portfolio, research, and system health. Designed to be sent as a Telegram summary.

## Instructions
- Time limit: 10 minutes
- Read-only — do NOT modify any files or run trades
- Focus on actionable insights, not raw data dumps
- Format output as a clean summary suitable for Telegram

## Tasks
1. **Portfolio Status**
   - Read `dashboard/data/dashboard-data.json`
   - Report: total equity, cash, open positions, unrealized P&L
   - Compare current equity to 7 days ago (from equity_curve data)
   - List open positions with current P&L

2. **Closed Trades (last 7 days)**
   - Read closed_trades from dashboard data
   - Win/loss count, average P&L, best/worst trade
   - Total realized P&L for the week

3. **Research Progress**
   - Read `research/journal.json` — count experiments this week
   - Pass rate, best Sharpe found, strategies tested
   - Read `research/queue.json` — queue depth
   - Autoresearch cycle count (from heartbeat)

4. **System Health**
   - Disk space, memory, service status (one-line each)
   - Any cron failures in the past week (check log timestamps)

5. **Key Metrics Summary**
   Build a table:
   | Metric | This Week | Previous Week | Trend |
   Metrics: equity, realized P&L, experiments run, pass rate, best Sharpe

## Deliverables
- One comprehensive summary message (Telegram-friendly, <4000 chars)
- Flag any items needing immediate attention
