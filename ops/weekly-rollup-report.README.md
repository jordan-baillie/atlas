# Weekly Portfolio Rollup Report

**Purpose:** Automated Sunday morning summary of all trading systems and infrastructure status.

## Schedule
- **When:** Every Sunday at 08:00 AEST
- **Where:** Telegram (via bot)
- **Cron:** `0 8 * * 0 /root/scripts/weekly-rollup-report.sh >> /root/logs/weekly-rollup.log 2>&1`

## Message Format

```
📊 WEEKLY PORTFOLIO ROLLUP
Week XX | YYYY-MM-DD → YYYY-MM-DD

━━━━━━━━━━━━━━━━━━━━━━━━

🔵 ATLAS SP500

Equity: $X,XXX.XX
Trades this week: X
Total trades (all-time): X of 30 target
Errors logged: X

━━━━━━━━━━━━━━━━━━━━━━━━

🟠 CRONUS PAPER TRADING

Active positions: X
Paper PnL: $X.XX
Service status: 🟢 Running

━━━━━━━━━━━━━━━━━━━━━━━━

🏉 NRL-PREDICT

Tips accuracy: N/A
Auto-submit: ✅ Success
Last run: YYYY-MM-DD

━━━━━━━━━━━━━━━━━━━━━━━━

⚙️ INFRASTRUCTURE

Service restarts: X
Failed services: X
Disk usage: XX%
IB Gateway disconnections: X
Backup snapshots: X
Backup size: X.X GB

━━━━━━━━━━━━━━━━━━━━━━━━

📋 TOP PRIORITY ACTIONS

• Deploy backup script
• Push all repos to GitHub
• Implement IB Gateway auto-restart
• Add log rotation
• Review Cronus paper trading results

━━━━━━━━━━━━━━━━━━━━━━━━

Generated: YYYY-MM-DD HH:MM AEST
```

## Data Sources

| Section | Data Source | Location |
|---------|-------------|----------|
| Atlas Equity | Latest positions JSON | `/root/atlas/data/position_monitor/positions.json` |
| Atlas Trades | Trade ledger | `/root/atlas/journal/trade_ledger.json` |
| Atlas Errors | Log files (7 days) | `/root/atlas/logs/*.log` |
| Cronus Positions | Paper trading DB | `/root/_archive/cronus-2026-05-18/data/cronus_paper_state.db` |
| Cronus Status | Systemd service | `systemctl status cronus-trader.service` |
| NRL Submit | Cron logs | `/root/NRL-Predict/logs/nrl-cron-tips.log.1` |
| Service Restarts | Journal (7 days) | `journalctl --since "$START_DATE"` |
| Failed Services | Systemd | `systemctl list-units --state=failed` |
| Disk Usage | Filesystem | `df -h /` |
| IB Disconnections | Cronus logs (7 days) | `/root/_archive/cronus-2026-05-18/logs/*.log` |
| Backup Status | Restic (if configured) | `restic snapshots` |
| Action Items | Task file | `/root/tasks/portfolio-gap-analysis-2026-q1.md` |

## Manual Testing

```bash
# Run the script manually
bash /root/scripts/weekly-rollup-report.sh

# Check the log
tail -50 /root/logs/weekly-rollup.log

# Verify cron entry
crontab -l | grep weekly-rollup
```

## Credentials

Reads Telegram credentials from `/root/.atlas-secrets.json`:
```json
{
  "telegram_bot_token": "...",
  "telegram_chat_id": "..."
}
```

## Maintenance

**Add new sections:**
1. Add data collection function: `get_project_data()`
2. Update `build_message()` to parse and format the data
3. Test manually before relying on cron

**Modify existing sections:**
1. Edit the relevant `get_*_data()` function
2. Update parsing in `build_message()`
3. Test with `bash /root/scripts/weekly-rollup-report.sh`

**Troubleshooting:**
- Check log: `tail -50 /root/logs/weekly-rollup.log`
- Test Telegram API: `curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" -d "chat_id=$CHAT_ID" -d "text=test"`
- Verify data sources exist and are readable
- Check cron execution: `grep weekly-rollup /var/log/syslog`

## Future Enhancements

Potential additions for future iterations:
- [ ] Week-over-week comparison (equity delta, trade count change)
- [ ] Rolling 4-week statistics
- [ ] Performance attribution by strategy
- [ ] Alert threshold triggers (e.g., >5 errors = red flag)
- [ ] Trend arrows (📈 📉) for key metrics
- [ ] Link to live dashboard
- [ ] Include upcoming events from economic calendar
