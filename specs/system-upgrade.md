# System Upgrade

Update Python packages, verify compatibility, and ensure all services still work. Use periodically or when a specific package update is needed.

## Instructions
- Time limit: 15 minutes
- CAUTION: package updates can break things — be careful
- Always check what would change BEFORE installing
- Test critical imports after any update
- Do NOT update system packages (apt) — only pip packages

## Tasks
1. **Check outdated packages**
   ```bash
   pip list --outdated --break-system-packages 2>/dev/null | head -20
   ```

2. **Check for security issues** (if any critical packages are outdated)
   - Focus on: yfinance, pandas, numpy, python-telegram-bot, requests

3. **If updates needed:**
   - Install one at a time
   - After each: test critical imports
   ```python
   # Critical import test
   import pandas, numpy, yfinance
   import telegram
   from research.loop import ResearchSession
   from brokers.plan import TradePlanGenerator
   from utils.telegram import send_message
   print("All critical imports OK")
   ```

4. **Verify services still work**
   ```bash
   systemctl restart atlas-telegram-bot
   sleep 3
   systemctl is-active atlas-telegram-bot
   ```

5. **Check disk space consumed by pip cache**
   ```bash
   du -sh /root/.cache/pip 2>/dev/null
   pip cache purge --break-system-packages 2>/dev/null
   ```

## Deliverables
- List of packages updated (before → after version)
- Import test results (pass/fail)
- Service verification results
- Disk space freed (if any)
