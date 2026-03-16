# Services & Infrastructure — State Queries

## Check all services at once

```bash
systemctl is-active atlas-dashboard atlas-dashboard-refresh atlas-telegram-bot atlas-director atlas-research-runner atlas-research-window
```

Output: one line per service, `active` or `inactive`/`failed`.

## Detailed service status

```bash
systemctl status atlas-<name> --no-pager
```

## Recent service logs

```bash
# Last 50 lines
journalctl -u atlas-<name> --no-pager -n 50

# Last hour
journalctl -u atlas-<name> --no-pager --since "1 hour ago"

# Follow live
journalctl -u atlas-<name> -f
```

## Restart a service

```bash
systemctl restart atlas-<name>
# Verify after restart:
sleep 3 && systemctl is-active atlas-<name>
```

## Service → code mapping

| Service | Entry point | Config |
|---------|------------|--------|
| atlas-dashboard | `services/dashboard_server.py` | `/etc/systemd/system/atlas-dashboard.service` |
| atlas-dashboard-refresh | `scripts/dashboard_loop.sh` | `/etc/systemd/system/atlas-dashboard-refresh.service` |
| atlas-telegram-bot | `services/telegram_bot.py` | `/etc/systemd/system/atlas-telegram-bot.service` |
| atlas-director | `scripts/director_cron.py` | `/etc/systemd/system/atlas-director.service` (timer-activated) |
| atlas-research-runner | `scripts/autoresearch.py` | `/etc/systemd/system/atlas-research-runner.service` |
| atlas-research-window | (sweep script) | `/etc/systemd/system/atlas-research-window.service` (timer-activated) |

## List failed units

```bash
systemctl list-units --failed 'atlas-*' --no-pager
```

## Search for errors across all logs

```bash
grep -i "error\|exception\|traceback\|failed" /root/atlas/logs/*.log | tail -20
```
