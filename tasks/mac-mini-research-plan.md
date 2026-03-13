# Atlas Distributed Research Engine — Mac Mini Plan

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   VPS (Hostinger)                    │
│  Live trading, dashboard, Telegram bot, monitoring   │
│                                                      │
│  Services:                                           │
│    atlas-dashboard           (Caddy + generate_data) │
│    atlas-telegram-bot        (user interaction)      │
│    atlas-dashboard-refresh   (10s refresh loop)      │
│    pi-cron premarket/postclose  (plan + execute)     │
│    intraday_monitor          (price + risk alerts)   │
│    sync_protective_orders    (stop-loss sync)        │
│    ceasefire_monitor         (geopolitical)          │
│                                                      │
│  NO research runs here anymore                       │
│                                                      │
│  Reads from Mac Mini:                                │
│    rsync pull every 5 min:                           │
│      research/journal.json                           │
│      research/queue.json                             │
│      research/best/*.json                            │
│      research/results/*.tsv                          │
│      research/brain/**                               │
│      /tmp/*heartbeat*.json                           │
│                                                      │
│  Pushes to Mac Mini:                                 │
│    rsync push on data refresh:                       │
│      data/cache/sp500/*.parquet                      │
│      config/active/*.json                            │
│      research/queue.json (director edits)            │
│      research/directives.json                        │
└──────────────────┬──────────────────────────────────┘
                   │ SSH tunnel / rsync over SSH
                   │ (Mac Mini ↔ VPS, key-based auth)
┌──────────────────┴──────────────────────────────────┐
│               Mac Mini (M4, 10 cores)                │
│  Research engine — 24/7 unrestricted compute         │
│                                                      │
│  Services:                                           │
│    atlas-sweep              (24/7 parameter sweep)   │
│    atlas-research-runner    (queue experiment daemon) │
│    atlas-director           (queue management)       │
│    atlas-data-sync          (rsync to/from VPS)      │
│                                                      │
│  Config:                                             │
│    sweep workers: 8 (of 10 cores, leave 2 for OS)   │
│    nice: 0 (no throttling needed — dedicated box)    │
│    cycles: 0 (infinite)                              │
│    max_runtime: 0 (no time limit)                    │
│    all strategies, top_n: 100+                       │
│                                                      │
│  Local data:                                         │
│    data/cache/sp500/*.parquet  (synced from VPS)     │
│    research/*  (authoritative copy)                  │
│    config/active/*  (synced from VPS)                │
└─────────────────────────────────────────────────────┘
```

## What Changes vs Current Setup

| Aspect | Current (VPS only) | New (VPS + Mac Mini) |
|--------|-------------------|---------------------|
| Sweep workers | 6 cores, nice +15, ionice | 8 cores, nice 0, no throttling |
| Sweep schedule | 5 × 2h windows/day (~10h/day weekdays) | 24/7 continuous |
| Weekly research hours | ~66h/week | ~168h/week (2.5×) |
| Experiments/day | ~40-60 (throttled windows) | ~150-250 (continuous, more cores) |
| Top-N tickers | 30 (time-boxed) | 100+ (no time limit) |
| VPS CPU headroom | Tight (sweep competes with trading) | 100% free for trading |
| Data freshness | Local (instant) | Synced via rsync (≤5 min lag) |
| Single point of failure | VPS = everything | Trading survives Mac Mini down; research survives VPS reboot |

## Implementation Plan

### Phase 1: Mac Mini Setup (Day 1)

1. **OS & Python environment**
   - macOS or Ubuntu (recommend Ubuntu Server for systemd compatibility)
   - Python 3.12, same as VPS
   - `pip install` same requirements.txt
   - Test: `python3 -c "import pandas, numpy, yfinance; print('OK')"`

2. **SSH key exchange**
   - Generate keypair on Mac Mini
   - Add public key to VPS `~/.ssh/authorized_keys`
   - Test: `ssh root@<vps-ip> "echo OK"` from Mac Mini
   - Add VPS host fingerprint to known_hosts

3. **Clone Atlas repo**
   - `git clone` from VPS (or same remote)
   - Copy `~/.atlas-secrets.json` (only needs `telegram_bot_token`, `telegram_chat_id` for notifications)
   - Does NOT need Alpaca keys (no trading from Mac Mini)

### Phase 2: Data Sync Service (Day 1-2)

**The key design decision: who owns what data?**

| Data | Owner | Direction | Frequency |
|------|-------|-----------|-----------|
| `data/cache/sp500/*.parquet` | VPS | VPS → Mini | On data refresh (daily premarket) |
| `config/active/*.json` | VPS | VPS → Mini | On config change |
| `research/queue.json` | Shared | Bidirectional (merge) | Every 5 min |
| `research/journal.json` | Mac Mini | Mini → VPS | Every 5 min |
| `research/best/*.json` | Mac Mini | Mini → VPS | Every 5 min |
| `research/results/*.tsv` | Mac Mini | Mini → VPS | Every 5 min |
| `research/brain/**` | Mac Mini | Mini → VPS | Every 5 min |
| `research/directives.json` | VPS | VPS → Mini | Every 5 min |
| `/tmp/*heartbeat*.json` | Mac Mini | Mini → VPS | Every 1 min |

**Sync script: `scripts/research_sync.sh`** (runs on Mac Mini)

```bash
#!/bin/bash
# Atlas Research Sync — bidirectional rsync between Mac Mini and VPS
# Runs as systemd timer every 5 minutes

VPS="root@<vps-ip>"
ATLAS="/root/atlas"
REMOTE_ATLAS="/root/atlas"

# 1. Pull data + config FROM VPS (VPS is authoritative)
rsync -az --delete \
  "$VPS:$REMOTE_ATLAS/data/cache/sp500/" \
  "$ATLAS/data/cache/sp500/"

rsync -az \
  "$VPS:$REMOTE_ATLAS/config/active/" \
  "$ATLAS/config/active/"

rsync -az \
  "$VPS:$REMOTE_ATLAS/research/directives.json" \
  "$ATLAS/research/directives.json" 2>/dev/null || true

# 2. Push research results TO VPS (Mac Mini is authoritative)
rsync -az \
  "$ATLAS/research/journal.json" \
  "$ATLAS/research/queue.json" \
  "$VPS:$REMOTE_ATLAS/research/"

rsync -az \
  "$ATLAS/research/best/" \
  "$VPS:$REMOTE_ATLAS/research/best/"

rsync -az \
  "$ATLAS/research/results/" \
  "$VPS:$REMOTE_ATLAS/research/results/"

rsync -az \
  "$ATLAS/research/brain/" \
  "$VPS:$REMOTE_ATLAS/research/brain/"

# 3. Push heartbeats TO VPS (for dashboard display)
rsync -az \
  /tmp/autoresearch-heartbeat.json \
  /tmp/runner-daemon-heartbeat.json \
  "$VPS:/tmp/" 2>/dev/null || true
```

**Heartbeat sync: separate 1-min timer** so dashboard stays responsive.

### Phase 3: Sweep Configuration (Day 2)

**Mac Mini sweep config** — no throttling, full power:

```bash
# /etc/systemd/system/atlas-sweep.service
[Unit]
Description=Atlas Autoresearch Sweep — 24/7 continuous
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/atlas
ExecStart=/usr/bin/python3 -u research/sweep.py \
    --market sp500 \
    --top-n 100 \
    --workers 8 \
    --max-fails 10 \
    --cycles 0 \
    --max-runtime 0
Restart=on-failure
RestartSec=120
Nice=0
Environment=PYTHONUNBUFFERED=1
Environment=HOME=/root
Environment=TZ=Australia/Brisbane

[Install]
WantedBy=multi-user.target
```

**Key differences from VPS config:**
- `workers: 8` (was 6, and nice+15/ionice)
- `top_n: 100` (was 30)
- `cycles: 0` (infinite — no time limit)
- `max_runtime: 0` (no 2-hour cutoff)
- `Nice=0` (dedicated box, no competition)
- `Restart=on-failure` (auto-recovery)

### Phase 4: VPS Cleanup (Day 2)

1. **Disable research services on VPS:**
   ```bash
   systemctl stop atlas-research-window.timer atlas-research-window.service
   systemctl stop atlas-research-runner.service
   systemctl disable atlas-research-window.timer atlas-research-runner.service
   ```

2. **Keep director on VPS** (or move to Mac Mini — it edits queue.json):
   - Option A: Keep on VPS — director reads dashboard data, edits queue.json, VPS pushes to Mini
   - Option B: Move to Mac Mini — director runs locally, queue edits are immediate
   - **Recommend: Option B** (director + sweep + runner all on Mac Mini = no sync lag)

3. **Dashboard `generate_data.py`** — already reads from local `research/` files.
   After sync, the VPS copy of `research/journal.json`, `best/`, `results/`, `brain/`
   are fresh. No code changes needed.

### Phase 5: queue.json Conflict Resolution (Day 2-3)

This is the trickiest part. Both sides can modify `research/queue.json`:
- **Mac Mini**: sweep.py updates experiment status (queued → running → passed/failed)
- **VPS**: director edits queue (adds experiments, changes priority)

**Solution: Mac Mini owns queue.json**

The Mac Mini is the authoritative writer. The VPS director writes to a
sidecar file `research/queue_edits.json` instead of editing queue.json directly.
The Mac Mini's sweep process picks up edits and applies them.

```python
# research/queue_inbox.py — runs on Mac Mini before each sweep cycle
# Reads queue_edits.json, applies to queue.json, clears inbox

def apply_inbox():
    edits = read_json("research/queue_edits.json")  # from VPS director
    if not edits:
        return
    queue = read_json("research/queue.json")
    for edit in edits:
        if edit["action"] == "add":
            queue.append(edit["experiment"])
        elif edit["action"] == "update_priority":
            for exp in queue:
                if exp["id"] == edit["id"]:
                    exp["priority"] = edit["priority"]
        elif edit["action"] == "retire":
            queue = [e for e in queue if e["id"] != edit["id"]]
    write_json("research/queue.json", queue)
    write_json("research/queue_edits.json", [])  # clear inbox
```

**Alternative (simpler v1):** Just let Mac Mini own queue.json entirely.
Director runs on Mac Mini and edits locally. VPS only reads queue.json
(for dashboard display). No conflict possible.

→ **Recommend the simpler v1 for launch.** Migrate to inbox pattern only
if we actually need VPS-side queue edits.

### Phase 6: Code Changes Required

| File | Change | Why |
|------|--------|-----|
| `research/sweep.py` | Remove `nice`/`ionice` env check | Mac Mini doesn't throttle |
| `research/sweep.py` | Add `--max-runtime 0` support (= infinite) | Already supported (0 = no limit) |
| `research/monitoring.py` | Read heartbeat from rsync'd `/tmp/` | Already does — no change needed |
| `dashboard/generate_data.py` | No change | Reads local `research/` — gets rsync'd data |
| `scripts/director_cron.py` | Move to Mac Mini OR write to `queue_edits.json` | Avoid queue conflicts |
| `scripts/research_cron.sh` | Delete (or repurpose for Mac Mini) | VPS no longer runs sweeps |
| NEW: `scripts/research_sync.sh` | Bidirectional rsync | Core sync mechanism |
| NEW: systemd timers for sync | 5-min data sync + 1-min heartbeat sync | Keep VPS dashboard current |

### Phase 7: Monitoring & Alerting

1. **Mac Mini offline detection** (on VPS):
   - If heartbeat files are >10 min stale → Telegram alert
   - Add check to `intraday_monitor.py` or standalone watchdog

2. **Mac Mini health** (local):
   - CPU temp monitoring (Mac Minis run hot under sustained load)
   - Disk space check (parquet cache + results grow)
   - Network connectivity check (rsync must succeed)

3. **Dashboard shows remote status**:
   - Already works — reads heartbeat JSONs
   - Agent cards will show sweep/runner status from Mac Mini
   - Add "sync lag" indicator: last successful rsync timestamp

## Migration Checklist

```
[ ] Mac Mini: install Ubuntu Server / macOS
[ ] Mac Mini: Python 3.12 + Atlas dependencies
[ ] Mac Mini: SSH keypair + VPS authorized_keys
[ ] Mac Mini: git clone atlas repo
[ ] Mac Mini: copy ~/.atlas-secrets.json (Telegram only)
[ ] Mac Mini: initial rsync of data/cache/sp500/
[ ] Mac Mini: test sweep.py runs locally (1 cycle, 1 strategy)
[ ] Mac Mini: systemd service for atlas-sweep (24/7)
[ ] Mac Mini: systemd service for atlas-research-runner
[ ] Mac Mini: systemd service + timer for research_sync.sh
[ ] Mac Mini: systemd service for atlas-director
[ ] VPS: disable atlas-research-window.timer
[ ] VPS: disable atlas-research-runner.service
[ ] VPS: disable atlas-director.timer (moved to Mini)
[ ] VPS: add stale-heartbeat watchdog
[ ] VPS: verify dashboard still shows research data
[ ] Test: run sweep for 1 hour, verify rsync, check dashboard
[ ] Test: reboot Mac Mini, verify auto-restart
[ ] Test: kill VPS rsync, verify Mac Mini keeps running independently
```

## Expected Impact

| Metric | Current | Projected |
|--------|---------|-----------|
| Experiments/day | ~50 | ~200+ |
| Strategy coverage | 30 tickers × 6 workers | 100+ tickers × 8 workers |
| Research uptime | ~66h/week (windowed) | 168h/week (24/7) |
| Time to test all 29 strategies | ~3 weeks | ~5 days |
| VPS CPU for trading | Contested | Dedicated |
| Backtest queue drain rate | ~8 exp/day (runner) | ~30 exp/day |

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Mac Mini offline | VPS trading unaffected; research pauses, resumes on reconnect |
| Sync failure | VPS uses last-known research data; dashboard shows stale indicator |
| queue.json corruption | File-level locking + atomic writes (already implemented) |
| Data staleness | VPS pushes data immediately after refresh; 5-min max lag |
| Mac Mini overheating | Monitor CPU temp; sustained 8-core load is within M4 thermal budget |
| Network latency | rsync over SSH is delta-compressed; <15MB total sync payload |
