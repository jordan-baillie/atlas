# Atlas systemd units

Version-controlled mirror of systemd unit files that live in `/etc/systemd/system/` on the Atlas production host. Edit here, run `install.sh` to deploy.

## What's here

| File | Type | Purpose |
|------|------|---------|
| `atlas-heartbeat-watchdog.service` | oneshot | Runs `scripts/heartbeat_watchdog.py` — flags stale service heartbeats. |
| `atlas-heartbeat-watchdog.timer` | timer | Fires the watchdog every 15 minutes (with 5 min boot delay). |
| `atlas-research-window.service` | oneshot | Legacy multi-phase sweep entry point (`scripts/research_cron.sh`). Currently unused in production — kept for reference / manual invocation. |
| `atlas-research-window.timer` | timer | Legacy 5-windows-per-weekday + 4-per-weekend schedule for the above. **Disabled in production.** |
| `atlas-research-window@.service` | template | Per-universe sweep (`scripts/research_window_universe.sh %i`). `TimeoutStartSec=6000` — covers worst-case sp500 instance (4200s sweep + 1500s LLM + 300s slack). |
| `atlas-research-window@<universe>.timer` × 7 | timers | Nightly per-universe sweeps, staggered hourly 23:00–05:00 local. |

## Per-universe schedule

| Universe | Start (local TZ) |
|----------|------------------|
| `sp500` | 23:00 |
| `commodity_etfs` | 00:00 |
| `sector_etfs` | 01:00 |
| `gold_etfs` | 02:00 |
| `treasury_etfs` | 03:00 |
| `defensive_etfs` | 04:00 |
| `crypto` | 05:00 |

Each timer triggers `atlas-research-window@<universe>.service`, which runs `scripts/research_window_universe.sh <universe>`.

## Deploy

Run on the target host as root:

```
sudo /root/atlas/systemd/install.sh
```

The script symlinks every `*.service`/`*.timer` in this directory into `/etc/systemd/system/`, runs `daemon-reload`, and enables+starts the production timer set (heartbeat watchdog + 7 per-universe sweeps). It is idempotent — re-runs are silent no-ops.

`atlas-research-window.timer` (legacy multi-phase) is intentionally left disabled. Enable manually with `systemctl enable --now atlas-research-window.timer` if you want the old 5-window-per-weekday schedule.

## Timeout rationale

`atlas-research-window@.service` uses `TimeoutStartSec=6000` (was 3600 before 2026-04-19). The old 3600s bound was killing long sp500 sweeps mid-LLM promotion; 6000s leaves ~100s of slack above the worst-case 4200s sweep + 1500s LLM budget. Shorter universes complete in well under an hour and exit early — the longer bound is a safety net, not the expected run time.
