# Atlas Operations

*Created 2026-06-11 with the great-deletion refactor.*

## The unit set (all under `systemd/`, installed by `sudo systemd/install.sh`)

| Unit | Schedule | Runs |
|---|---|---|
| `atlas-dashboard.service` | always-on | `uvicorn atlas.dashboard.app:app` :8899 |
| `atlas-live-shadow.timer` | Mon–Fri 22:00 UTC | `ops/forward-paper.sh` (returns → refresh → shadow loop) |
| `atlas-backup.timer` | daily 04:00 AEST | `ops/backup-all-projects.sh` (restic) |
| `unified-healthcheck.timer` | every 6 h | `ops/unified-healthcheck.sh` (Telegram report) |
| `atlas-weekly-maintenance.timer` | Sun 06:00 AEST | `ops/weekly-maintenance.sh` |
| `atlas-sediment-cleanup.timer` | daily 14:00 AEST | `ops/cleanup_sediment.py --apply` |
| `atlas-sp500-flatten.timer` | Mon–Fri 14:45 UTC | transitional — delete once the retired account is flat |

`install.sh` also **durably retires** removed units (telegram-bot, dashboard-refresh, and the
17 swing-era families) on every run — a reinstall can never re-enable them.

Cron: the root crontab holds only NRL-Predict (`scripts/atlas.crontab`), pending its move to
`/etc/cron.d/nrl-predict`. All Atlas hygiene runs on timers.

## Halting trading

```bash
python3 -m atlas.execution.kill_switch status          # which layer (if any) is tripped
python3 -m atlas.execution.kill_switch halt "reason"   # creates data/HALT + Telegram confirm
python3 -m atlas.execution.kill_switch resume          # clears HALT + AUTO_REMEDIATION_HALT
# zero-dependency fallback:
touch /root/atlas/data/HALT
```

`data/HALT` blocks orders in two layers: inside `TargetExecutor` (fail-closed) and at systemd
(`ConditionPathExists=!` on the live-shadow unit).

## Strategy lifecycle

```bash
python3 -m atlas.execution.registry list               # deployed strategies + states
python3 -m atlas.execution.registry approve NAME       # human gate for real capital
python3 -m atlas.execution.registry state NAME canary  # shadow -> canary -> live
```

A PASS arrives automatically: Crucible calls `deploy_pass()` → state `shadow` (Paper Book).
Real capital (canary/live) stays human-gated — unapproved strategies are computed dry and
flagged AWAITING APPROVAL in the daily digest.

## Smoke checks

```bash
systemctl list-timers | grep -E "atlas|unified"
curl -su USER:PASS http://127.0.0.1:8899/api/system/health | jq .services
curl -su USER:PASS http://127.0.0.1:8899/api/live | jq .
tail -50 /root/atlas/data/live/forward_paper.log
cd /root/atlas && python3 -m atlas.execution.daily --mode shadow   # manual cycle
```

---

## Deploy runbook — 2026-06-11 restructure rollout

The refactor renamed every Python import path (single `atlas/` package), retired the Telegram
bot, and consolidated systemd. Atlas and Crucible must be pulled **in the same window** (before
the next 22:00 UTC live-shadow run).

**Pre-flight (read-only):**
```bash
systemctl list-timers --all | grep -E "atlas|unified" > /tmp/pre-deploy-units.txt
systemctl cat atlas-live-shadow.service 2>/dev/null   # host-local edits? note them
systemctl cat atlas-dashboard-refresh.service 2>/dev/null  # host-local unit — being retired
crontab -l > /tmp/pre-deploy-crontab.txt
journalctl -u atlas-sp500-flatten -n 5                 # is the retired account flat yet?
```

**Deploy:**
```bash
systemctl stop atlas-telegram-bot 2>/dev/null; systemctl disable atlas-telegram-bot 2>/dev/null
cd /root/atlas    && git pull
cd /root/crucible && git pull          # coordinated deploy_pass import-path commit
cd /root/atlas    && sudo systemd/install.sh   # retires old units, links + enables new set
# clear stale leftovers so old package dirs can't shadow imports
rm -rf /root/atlas/{live,brokers,core,services,utils,db,analytics,portfolio,markets,monitor,journal,risk,alerting}/__pycache__ 2>/dev/null
find /root/atlas/{live,brokers,core,services,utils,db,analytics} -maxdepth 0 -type d 2>/dev/null  # should list nothing after pull
sudo crontab /root/atlas/scripts/atlas.crontab  # NRL only; Atlas hygiene now on timers
systemctl restart atlas-dashboard
```

**Verify:**
```bash
curl -su USER:PASS http://127.0.0.1:8899/api/system/health | jq .services
curl -su USER:PASS http://127.0.0.1:8899/api/live | jq '.strategies | length'
python3 -c "import sys; sys.path.insert(0,'/root/atlas'); from atlas.execution.providers import deploy_pass; print('contract OK')"
cd /root/atlas && python3 -m atlas.execution.record_returns && python3 -m atlas.execution.daily --mode shadow
cd /root/crucible && python3 live/deploy.py refresh
systemctl list-timers | grep atlas    # live-shadow, backup, weekly-maintenance, sediment-cleanup (+flatten)
# next morning: crucible morning report renders the forward-paper section; dashboard Live tab populated
```

**Rollback:**
```bash
cd /root/atlas    && git reset --hard pre-cleanup-2026-06-11
cd /root/crucible && git revert <coordinated-commit>
sudo /root/atlas/systemd/install.sh && systemctl daemon-reload
systemctl enable --now atlas-telegram-bot && systemctl restart atlas-dashboard
```
`data/`, `config/`, and `~/.atlas-secrets.json` are never touched by deploy or rollback.

**Post-deploy follow-ups:**
- Delete `atlas-sp500-flatten.{service,timer}` + `ops/flatten_sp500.py` once the account is flat.
- Re-point kill-switch L4 at `data/live/*/equity_state.json` (currently fail-open: stale table).
- If `data/live/demo/` exists on the host (old test pollution), remove it.
- `npx gitnexus analyze` to rebuild the code index for the new tree.
