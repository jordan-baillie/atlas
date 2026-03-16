# Service Failures — Incident Cookbook

Covers: OOM kills, timeout kills, orphan processes, port conflicts, Telegram bot errors.

---

## Pattern 1: OOM Kill (research-runner, research-window)

**Symptoms:** Service status `failed`, journal shows `Killed` or `signal=9/KILL`, memory peak near limit.

**Root cause:** Large backtest consuming too much memory (350MB+ for research-runner, 1GB+ for research-window).

**Fix:**
```bash
# Check current memory usage peak
journalctl -u atlas-research-runner --no-pager -n 5 | grep "memory peak"

# Increase memory limit
systemctl edit atlas-research-runner
# Add under [Service]:
# MemoryMax=1G

systemctl daemon-reload
systemctl restart atlas-research-runner
```

**Verify:**
```bash
systemctl is-active atlas-research-runner
journalctl -u atlas-research-runner --no-pager -n 10
```

---

## Pattern 2: Timeout Kill (director, research-runner)

**Symptoms:** Journal shows `start operation timed out. Terminating`, `code=killed, status=15/TERM`.

**Root cause:** `portfolio_optimizer` or backtest took longer than systemd `TimeoutStartSec`.

**Fix:**
```bash
# Check current timeout
systemctl show atlas-director -p TimeoutStartSec

# Increase timeout
systemctl edit atlas-director
# Add under [Service]:
# TimeoutStartSec=900

systemctl daemon-reload
systemctl restart atlas-director
```

**Verify:**
```bash
systemctl is-active atlas-director
journalctl -u atlas-director --no-pager -n 10
```

---

## Pattern 3: Orphan Processes (research-window)

**Symptoms:** `Unit process XXXX remains running after unit stopped`, service fails on next start.

**Root cause:** `ProcessPoolExecutor` workers not cleaned up on service stop.

**Fix:**
```bash
# Find and kill orphan python processes from research
ps aux | grep "research\|sweep\|autoresearch" | grep -v grep
kill <pids>

# Then restart
systemctl restart atlas-research-window
```

**Verify:**
```bash
ps aux | grep "research\|sweep\|autoresearch" | grep -v grep
systemctl is-active atlas-research-window
```

---

## Pattern 4: Service Won't Start — Port Conflict (dashboard)

**Symptoms:** `Address already in use`, dashboard fails to bind.

**Fix:**
```bash
# Find what's using the port
lsof -i :8501
kill <pid>
systemctl restart atlas-dashboard
```

**Verify:**
```bash
systemctl is-active atlas-dashboard
curl -s http://localhost:8501 > /dev/null && echo "OK"
```

---

## Pattern 5: Telegram Bot Connection Error

**Symptoms:** Bot stops responding, journal shows connection timeout.

**Fix:**
```bash
systemctl restart atlas-telegram-bot
sleep 3
systemctl is-active atlas-telegram-bot
```

**Verify:**
```bash
journalctl -u atlas-telegram-bot --no-pager -n 10
# Should show "Application started" or "Polling..." — no connection errors
```
