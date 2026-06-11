# Atlas systemd units

Version-controlled source of truth for the units on the Atlas production host.
Edit here, then `sudo ./install.sh` — it symlinks every unit in this directory,
**durably retires** anything removed from it (disable + unlink, every run), and
enables the active schedule.

| Unit | Schedule | Runs |
|---|---|---|
| `atlas-dashboard.service` | always-on | `uvicorn atlas.dashboard.app:app` :8899 |
| `atlas-live-shadow.{service,timer}` | Mon–Fri 22:00 UTC | `ops/forward-paper.sh` — the forge→live daily cycle (blocked by `data/HALT`) |
| `atlas-backup.{service,timer}` | daily 04:00 AEST | restic backup of all projects |
| `unified-healthcheck.{service,timer}` | every 6 h | cross-project health → Telegram |
| `atlas-weekly-maintenance.{service,timer}` | Sun 06:00 AEST | log rotation, cache purge, DB ANALYZE |
| `atlas-sediment-cleanup.{service,timer}` | daily 14:00 AEST | prune aged backup/incident files |
| `atlas-sp500-flatten.{service,timer}` | Mon–Fri 14:45 UTC | **transitional** — flattens the retired SP500 paper account; delete once flat |

`atlas.conf.template` seeds `/etc/atlas/atlas.conf` (shared EnvironmentFile).

See `docs/OPERATIONS.md` for runbooks (halt/resume, deploy, rollback).
