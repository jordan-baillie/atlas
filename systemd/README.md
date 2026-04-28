# Atlas systemd units

Version-controlled mirror of systemd unit files that live in `/etc/systemd/system/` on the Atlas production host. Edit here, run `install.sh` to deploy.

Manages **12 enabled production timers** covering backup, discovery, heartbeat/silent-failure watchdogs, weekly director, and nightly per-universe research sweeps.

## What's here

| File | Type | Purpose |
|------|------|---------|
| `atlas-heartbeat-watchdog.service` | oneshot | Runs `scripts/heartbeat_watchdog.py` — flags stale service heartbeats. |
| `atlas-heartbeat-watchdog.timer` | timer | Fires the watchdog every 15 minutes (with 5 min boot delay). |
| `atlas-research-window@.service` | template | Per-universe sweep (`scripts/research_window_universe.sh %i`). `TimeoutStartSec=6000` — covers worst-case sp500 instance (4200s sweep + 1500s LLM + 300s slack). |
| `atlas-research-window@<universe>.timer` × 7 | timers | Nightly per-universe sweeps, staggered hourly 23:00–05:00 local. |
| `atlas-director.service` | oneshot | Runs `scripts/director_cron.py` — weekly queue management + portfolio review digest. |
| `atlas-director.timer` | timer | Fires director once weekly, Sun 08:00 AEST (Sat 22:00 UTC). **Enabled in production.** |
| `atlas-backup.service` | oneshot | Runs `/root/atlas/ops/backup-all-projects.sh` — restic backup of atlas/cronus/NRL-Predict/midas/.pi/ceo-board configs, data, state, secrets, systemd units. Repo: `/root/backups/restic-repo`. Retention: 7 daily / 4 weekly / 3 monthly. |
| `atlas-backup.timer` | timer | Daily 04:00 AEST, 300s randomized delay. Persistent. |
| `atlas-discovery.service` | oneshot (static) | `research/discovery/run.py` — LLM paper-to-strategy pipeline. Static unit (no `[Install]` section), triggered only by the timer (not `systemctl enable`-able directly). |
| `atlas-discovery.timer` | timer | Weekly Wed 10:00 AEST (Wed 00:00 UTC). Midweek slot chosen to avoid competing with director. Persistent. |
| `atlas-silent-failure-watchdog.service` | oneshot (static) | `scripts/silent_failure_watchdog.py` — detects services that exit 0 but produce degraded output (atlas-discovery "Papers found: 0" loop, director low-coverage heartbeat, zero-byte autoresearch logs in 24h). Always exits 0 itself. Static unit, triggered only by its timer. |
| `atlas-silent-failure-watchdog.timer` | timer | Hourly. Persistent. |
| `atlas-research-runner.service` | simple daemon | Long-running queue consumer (`scripts/research_runner.py`) — pops pending experiments off the research queue. **Disabled in production** — mirrored for version control only; `install.sh` does NOT enable it. |

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

## Director schedule

`atlas-director.timer` fires `atlas-director.service` once weekly:

| Field | Value |
|-------|-------|
| `OnCalendar` | `Sat 22:00:00 UTC` |
| Local time | Sunday 08:00 AEST |
| `Persistent` | `true` (runs on next boot if missed) |

`director_cron.py` does a queue-health check, conditionally regenerates experiments if the queue is low, conditionally re-runs the portfolio optimizer (threshold: 7 days since last run), conditionally triggers discovery (weekly), and posts a Telegram digest.

## Research runner (disabled)

`atlas-research-runner.service` is a `Type=simple` long-running daemon that consumes the research queue continuously. It is **disabled** on the production host — the nightly per-universe sweep timers handle research work on a schedule instead. The unit file is mirrored here for version control, but `install.sh` deliberately skips it (see NOTE in the script). Enable manually only if you intentionally want continuous queue consumption:

```
sudo systemctl enable --now atlas-research-runner.service
```

## Backup system

`atlas-backup.timer` fires `atlas-backup.service` daily at 04:00 AEST (5-minute randomized delay). The backup script (`/root/atlas/ops/backup-all-projects.sh`) uses `restic` to snapshot configs, data, state, secrets, and systemd units across atlas / cronus / NRL-Predict / midas / .pi / ceo-board into `/root/backups/restic-repo`, then applies a 7-daily / 4-weekly / 3-monthly retention policy.

Known failure mode: a stale restic lock (from a dead prior process) blocks the retention step. Manual fix: `RESTIC_PASSWORD=atlas-backup-2026 restic -r /root/backups/restic-repo unlock`. The backup itself (snapshot save) still succeeds during such failures — only `forget --prune` blocks on the lock.

## Discovery (weekly)

`atlas-discovery.timer` fires `atlas-discovery.service` every Wednesday at 00:00 UTC (10:00 AEST). The service runs `research/discovery/run.py` — the LLM paper-to-strategy pipeline that surfaces new candidate strategies from academic sources. The 3600s timeout bounds worst-case LLM latency.

`atlas-discovery.service` is a `static` unit — it has no `[Install]` section and cannot be `systemctl enable`d on its own. It is triggered exclusively by the timer, or manually via `systemctl start atlas-discovery.service`. This is the standard systemd pattern for "timer-only" units.

## Silent failure watchdog (hourly)

`atlas-silent-failure-watchdog.timer` fires `atlas-silent-failure-watchdog.service` every hour. The service runs `scripts/silent_failure_watchdog.py`, which performs three checks:

1. `atlas-discovery` journal scan for "Papers found: 0" (stuck loop / upstream failure)
2. `atlas-director` last heartbeat — alerts if `coverage_pct` below threshold (research matrix stale)
3. Zero-byte autoresearch log files modified in the last 24h, excluding logrotate stubs (LLM loop silent) — see commits `1deedcf5` and `18c63444` for the logrotate-stub filter that prevents false positives

The watchdog itself **always exits 0** — it must never become a paging source. All check failures are swallowed and surface only as Telegram alerts.

## Deploy

Run on the target host as root:

```
sudo /root/atlas/systemd/install.sh
```

The script symlinks every `*.service`/`*.timer` in this directory into `/etc/systemd/system/`, runs `daemon-reload`, and enables+starts the production timer set (12 timers: heartbeat + silent-failure watchdogs, backup, discovery, weekly director, 7 per-universe research sweeps). It is idempotent — re-runs are silent no-ops.

The legacy `atlas-research-window.service` / `atlas-research-window.timer` (non-templated, multi-phase) were removed 2026-04-28. The templated `atlas-research-window@<universe>.timer` set is the only schedule.

## Timeout rationale

`atlas-research-window@.service` uses `TimeoutStartSec=6000` (was 3600 before 2026-04-19). The old 3600s bound was killing long sp500 sweeps mid-LLM promotion; 6000s leaves ~100s of slack above the worst-case 4200s sweep + 1500s LLM budget. Shorter universes complete in well under an hour and exit early — the longer bound is a safety net, not the expected run time.

## Path parameterization

`/etc/atlas/atlas.conf` (deployed from `systemd/atlas.conf.template` by `install.sh`) exposes shared environment variables to every atlas unit via `EnvironmentFile=-/etc/atlas/atlas.conf` (optional — missing file is tolerated).

Current vars:

| Var | Default | Purpose |
|-----|---------|---------|
| `ATLAS_HOME` | `/root/atlas` | Project root. |
| `ATLAS_PYTHON` | `/usr/bin/python3` | Interpreter. |

**Note**: current unit files still hardcode `/root/atlas/...` and `/usr/bin/python3` in `WorkingDirectory=` and `ExecStart=`. The variables are available for future parameterization but are not yet referenced. Editing `atlas.conf` alone does NOT relocate the atlas install.

### Relocation procedure (future, when needed)

1. Edit `/etc/atlas/atlas.conf` — update `ATLAS_HOME` to the new path.
2. Edit each `.service` file in `systemd/` — replace hardcoded `/root/atlas` with `${ATLAS_HOME}` in `WorkingDirectory=` and `ExecStart=`.
3. `sudo systemctl daemon-reload`
4. `sudo systemctl restart atlas-*.service` (or individually).

Done as a separate, tested refactor — not as part of this sweep.
