# Atlas Trading System — Disaster Recovery Runbook

**Version:** 1.1  
**Last Updated:** 2026-04-17  
**Owner:** Atlas Operations  
**Criticality:** P0 — Trading System Continuity  
**Last DR Drill:** 2026-04-17 (restic snapshot e74810d5 → /tmp/atlas-restore-test; see `docs/DR_DRILLS.md`)

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Critical Dependencies](#critical-dependencies)
3. [Backup & Restore Procedures](#backup--restore-procedures)
4. [Disaster Scenarios](#disaster-scenarios)
   - [Scenario 1: Server Dies During Market Hours](#scenario-1-server-dies-during-market-hours)
   - [Scenario 2: Corrupted Database](#scenario-2-corrupted-database)
   - [Scenario 3: Broker API Outage](#scenario-3-broker-api-outage)
   - [Scenario 4: Bad Config Deployed](#scenario-4-bad-config-deployed)
   - [Scenario 5: Strategy Gone Haywire](#scenario-5-strategy-gone-haywire)
5. [Emergency Contacts](#emergency-contacts)
6. [Post-Incident Checklist](#post-incident-checklist)

---

## System Overview

### Architecture Summary
- **Primary Market:** Commodity ETFs (live trading via Alpaca)
- **Secondary Market:** SP500 (live trading via Alpaca)
- **Deployment:** Single Raspberry Pi server (`root@pi`)
- **Trading Mode:** Daily (market-on-open execution)
- **AUM as of 2026-04-16:** $4,997.13 tracked equity / $5,325.23 broker equity; 4 open positions (FCX, GLD, SLV, UNG) — commodity_etfs universe

### Core Components
```
atlas/
├── config/active/          # Active trading configs
├── data/
│   ├── atlas.db            # ⚠️ PRIMARY DATABASE (79 MB SQLite, 30 tables, 47 trades as of 2026-04-17)
│   ├── cache/             # Historical price data (regenerable)
│   └── processed/         # Computed features (regenerable)
├── journal/
│   └── archive/           # Historical JSON exports (pre-SQLite migration)
├── brokers/state/         # Broker connection state
└── scripts/               # Automation scripts
```

### Services (systemd)

All scheduled work runs via systemd timers — there is no crontab for trading operations.

| Unit | Type | Description |
|------|------|-------------|
| `atlas-dashboard.service` | Service | Web UI (FastAPI, port 8000, auth-protected) |
| `atlas-telegram-bot.service` | Service | Telegram alert notifications |
| `atlas-backup.timer` | Timer | Daily incremental restic backup at 04:00 AEST |
| `atlas-director.timer` | Timer | Weekly portfolio queue review (Saturday 22:00 UTC / Sunday 08:00 AEST) |
| `atlas-discovery.timer` | Timer | Weekly paper discovery (Wednesday 00:00 UTC / 10:00 AEST) |
| `atlas-heartbeat-watchdog.timer` | Timer | System heartbeat watchdog — fires every 15 minutes |
| `atlas-research-window.timer` | Timer | Multi-window research sweeps (weekdays 5×, weekends 4×) |

Verify with:
```bash
systemctl list-unit-files 'atlas-*' --state=enabled --no-pager
systemctl list-timers 'atlas-*' --all
```

### Scheduled Operations (systemd timers)
```
Every 15 min      → Heartbeat watchdog (atlas-heartbeat-watchdog.timer)
04:00 AEST daily  → Restic backup (atlas-backup.timer)
5× weekday/       → Research parameter sweeps (atlas-research-window.timer)
4× weekend
Wed 10:00 AEST    → Paper discovery scan (atlas-discovery.timer)
Sun 08:00 AEST    → Director queue review (atlas-director.timer)
```

### Critical Data Files
| File | Criticality | Recovery Method |
|------|-------------|-----------------|
| `data/atlas.db` | **P0** | Restic restore (see below) |
| `~/.atlas-secrets.json` | **P0** | Secure offline backup |
| `config/active/` | **P1** | Restic restore (included in daily backup) |
| `brokers/state/` | **P2** | Restic restore or rebuild from broker API |
| `data/cache/*` | **P3** | Regenerate via price data ingest |

---

## Critical Dependencies

### External Services
1. **Alpaca Markets API** (broker execution)
   - Live API: `https://api.alpaca.markets`
   - Paper API: `https://paper-api.alpaca.markets`
   - Credentials: `~/.atlas-secrets.json` (ALPACA_API_KEY, ALPACA_SECRET_KEY)
   - Fallback: None (single broker)

2. **Yahoo Finance** (market data)
   - Primary source for historical data
   - Fallback: Alpaca IEX feed

3. **Telegram Bot** (alerts)
   - Token: `~/.atlas-secrets.json` (TELEGRAM_BOT_TOKEN)
   - Chat ID: `~/.atlas-secrets.json` (TELEGRAM_CHAT_ID)

### Internal State
- **`data/atlas.db`:** Primary SQLite database — trades, equity curve, signals, research, portfolio snapshots, regime data (30 tables, 79 MB)
- **Broker State:** `brokers/state/` — last known positions and order state
- **Config Versions:** Historical config snapshots in `config/archive/`

---

## Backup & Restore Procedures

### Database State (verified 2026-04-17)
| Item | Value |
|------|-------|
| DB path | `/root/atlas/data/atlas.db` |
| DB size | 79 MB |
| Table count | 30 |
| Trade count (live) | 47 |
| Latest backup snapshot | `e74810d5` — 2026-04-17 04:03:04 AEST |
| Backup repo | `/root/backups/restic-repo` (local filesystem) |
| Snapshot count | 22 (as of 2026-04-17) |
| Retention policy | 7 daily, 4 weekly, 3 monthly |

> ⚠️ **OFFSITE GAP:** The restic repository is on the same server as the source data (`/root/backups/restic-repo`). A catastrophic server failure would take both source and backup. Manual offsite export is recommended monthly (see [Offsite Backup](#offsite-backup-recommended) below).

### Automated Backup System
**Schedule:** Daily at 04:00 AEST (via `atlas-backup.timer` → `atlas-backup.service`)  
**Method:** `restic` incremental snapshots via `/root/scripts/backup-all-projects.sh`  
**Repository:** `/root/backups/restic-repo` (local filesystem)  
**Retention:** 7 daily, 4 weekly, 3 monthly  

**What's Backed Up:**
- Primary database (`data/atlas.db`)
- All configs (`config/active/`, `config/archive/`)
- Journal and state (`journal/`, `brokers/state/`)
- Credentials (`~/.atlas-secrets.json`)
- Systemd service files (`/etc/systemd/system/atlas-*.service`)
- Atlas docs and tasks

**What's Excluded:**
- Large cache files (`data/cache/earnings/`, `data/cache/backtest/`)
- Regenerable data (`data/processed/`)
- Python bytecode (`__pycache__/`, `*.pyc`)

### Manual Backup (Pre-Major Change)
```bash
cd /root/atlas
timestamp=$(date +%Y%m%d_%H%M%S)

# Snapshot the DB
cp data/atlas.db /tmp/atlas_db_manual_backup_${timestamp}.db

# Full restic snapshot tagged as manual
RESTIC_PASSWORD="atlas-backup-2026" \
restic -r /root/backups/restic-repo backup \
    --tag manual-${timestamp} \
    data/atlas.db config/active/ \
    ~/.atlas-secrets.json

echo "Manual backup tagged: manual-${timestamp}"
```

### List Available Backups
```bash
# Credentials (from /root/scripts/backup-all-projects.sh)
export RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_REPOSITORY="/root/backups/restic-repo"

# List all snapshots
restic snapshots

# Show most recent 5
restic snapshots | tail -7

# List files in latest snapshot
restic ls latest | grep atlas.db

# Show snapshot details
restic snapshots --tag automated | tail -5
```

### Restore from Backup
```bash
# Credentials
export RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_REPOSITORY="/root/backups/restic-repo"

# --- Step 1: Choose snapshot ---
restic snapshots            # list all; note the ID of the snapshot to restore
SNAPSHOT_ID="latest"        # or a specific ID, e.g. "e74810d5"

# --- Step 2: Restore to scratch directory ---
RESTORE_TARGET="/tmp/atlas-restore-$(date +%s)"
mkdir -p "$RESTORE_TARGET"

restic restore $SNAPSHOT_ID \
    --target "$RESTORE_TARGET" \
    --include /root/atlas/data/atlas.db

# Restored DB ends up at:
RESTORED_DB="${RESTORE_TARGET}/root/atlas/data/atlas.db"

# --- Step 3: Verify integrity before replacing live DB ---
echo "=== Integrity check ==="
sqlite3 "$RESTORED_DB" "PRAGMA integrity_check;"

echo "=== Trade count (restored) ==="
sqlite3 "$RESTORED_DB" "SELECT count(*) FROM trades;"

echo "=== Trade count (live) ==="
sqlite3 /root/atlas/data/atlas.db "SELECT count(*) FROM trades;"

echo "=== Latest equity snapshot ==="
sqlite3 "$RESTORED_DB" \
  "SELECT date, market_id, equity, broker_equity, positions_count FROM equity_curve ORDER BY date DESC LIMIT 1;"

# --- Step 4: Replace live DB (ONLY after verifying above) ---
systemctl stop atlas-dashboard atlas-telegram-bot    # halt write traffic
cp /root/atlas/data/atlas.db /root/atlas/data/atlas.db.pre-restore-$(date +%s)  # safety snapshot
cp "$RESTORED_DB" /root/atlas/data/atlas.db
systemctl start atlas-dashboard atlas-telegram-bot

echo "✓ Restore complete"
```

### Offsite Backup (Recommended)
**Not currently automated.** Manual monthly export recommended:
```bash
cd /root/backups
tar -czf atlas_offsite_$(date +%Y%m).tar.gz restic-repo/
# Transfer to secure offsite storage (S3, external drive, etc.)
```

---

## Disaster Scenarios

---

## Scenario 1: Server Dies During Market Hours

### Symptoms
- VPS/Pi unresponsive / hardware failure
- Cannot SSH to server
- Dashboard offline
- Positions still open at broker

### Immediate Actions (Priority: 5-10 minutes)

#### Step 1: Verify Broker Positions (30 seconds)
```bash
# From ANY machine with internet:
# Option A: Alpaca web console
open https://app.alpaca.markets/paper/portfolio/positions

# Option B: Quick API check (if you have curl + credentials)
curl -X GET "https://api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET" | jq '.[] | {symbol, qty, current_price, unrealized_pl}'
```

**Decision Point:**
- If unrealized loss > 2% of equity → **HALT TRADING** (see Step 2)
- If positions look normal → Continue to recovery

#### Step 2: Emergency Trading Halt (if needed)
```bash
# Close all positions via Alpaca web console OR API
curl -X DELETE "https://api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET"

# Verify closure
curl -X GET "https://api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: YOUR_KEY" \
  -H "APCA-API-SECRET-KEY: YOUR_SECRET"
```

**⏱ Time Checkpoint: 2 minutes elapsed**

---

### Recovery Steps (Priority: Restore execution by market close)

#### Step 3: Provision New Server (5 minutes)
```bash
# Spin up new VPS (Ubuntu 22.04+, 4+ cores)
# Set timezone to AEST
sudo timedatectl set-timezone Australia/Brisbane

# Install dependencies
sudo apt update && sudo apt install -y \
    python3 python3-pip python3-venv \
    restic git curl jq sqlite3

# Clone Atlas repo
cd /root
git clone <your-atlas-repo-url> atlas
cd atlas
```

#### Step 4: Restore Critical Files (3 minutes)
```bash
# Restore from restic backup (local repo — copy from offsite if server is dead)
export RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_REPOSITORY="/root/backups/restic-repo"  # or offsite copy path

RESTORE_TARGET="/tmp/atlas-restore"
mkdir -p "$RESTORE_TARGET"

restic restore latest --target "$RESTORE_TARGET" \
    --include '/root/atlas/data/atlas.db' \
    --include '/root/atlas/config/active/' \
    --include '/root/.atlas-secrets.json'

# Copy critical files into place
cp "${RESTORE_TARGET}/root/.atlas-secrets.json" ~/.atlas-secrets.json
chmod 600 ~/.atlas-secrets.json
cp "${RESTORE_TARGET}/root/atlas/data/atlas.db" /root/atlas/data/atlas.db
cp "${RESTORE_TARGET}/root/atlas/config/active/"* /root/atlas/config/active/

# Verify DB
sqlite3 /root/atlas/data/atlas.db "SELECT count(*) FROM trades; PRAGMA integrity_check;"
```

#### Step 5: Install Dependencies (2 minutes)
```bash
cd /root/atlas
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Step 6: Reconcile Positions with Broker (2 minutes)
```bash
# Check positions stored in DB vs live broker
cd /root/atlas
sqlite3 data/atlas.db \
  "SELECT ticker, shares, entry_price, close_price FROM position_snapshots ORDER BY rowid DESC LIMIT 10;"

python3 -c "
from brokers.alpaca_adapter import AlpacaAdapter
broker = AlpacaAdapter(paper=False)
positions = broker.get_positions()
print(f'Live broker positions: {len(positions)}')
for p in positions:
    print(f'  {p.symbol}: {p.qty} shares @ current \${p.current_price}')
"
```

**⏱ Time Checkpoint: 12 minutes elapsed**

#### Step 7: Start Critical Services (1 minute)
```bash
# Copy systemd service files (from restore)
sudo cp "${RESTORE_TARGET}/etc/systemd/system/atlas-"*.service /etc/systemd/system/
sudo systemctl daemon-reload

# Start services in order
sudo systemctl start atlas-telegram-bot
sudo systemctl start atlas-dashboard

# Verify
sudo systemctl status atlas-telegram-bot atlas-dashboard
```

#### Step 8: Verify Data Integrity (2 minutes)
```bash
# Check config validity
cd /root/atlas
python3 -c "
import json, sys
cfg = json.load(open('config/active/sp500.json'))
print('Config valid:', cfg.get('version'), cfg.get('market'))
"

# Verify broker connectivity
python3 -c "
from brokers.alpaca_adapter import AlpacaAdapter
broker = AlpacaAdapter(paper=False)
account = broker.get_account()
print(f'Broker connected: equity=\${account.equity}, buying_power=\${account.buying_power}')
"

# Check DB health
sqlite3 data/atlas.db "PRAGMA integrity_check; SELECT count(*) FROM trades;"
```

**⏱ Time Checkpoint: 15 minutes elapsed**

---

### Verification Checklist

- [ ] Broker positions match `position_snapshots` in DB
- [ ] Trade count in restored DB is ≤ live count (backup from 04:00; trades can happen post-backup)
- [ ] Config version matches expected
- [ ] Telegram bot sending alerts
- [ ] Dashboard accessible at `http://<new-ip>:8000`
- [ ] Systemd timers enabled and scheduled: `systemctl list-timers atlas-*`
- [ ] Protective stop-loss orders active at broker

### Estimated Recovery Time
- **Minimal viable (positions safe):** 2-5 minutes
- **Trading operational:** 15-20 minutes
- **Full system (dashboard, all timers):** 20-30 minutes

### What Can Wait Until After Market Close
- Historical data cache regeneration (`atlas ingest`)
- Non-critical timer restarts (research, discovery, director)
- Log file restoration

---

## Scenario 2: Corrupted Database

### Symptoms
- `data/atlas.db` unreadable or fails `PRAGMA integrity_check`
- SQLite errors in service logs
- Equity calculations wrong
- PnL mismatches with broker

### Immediate Actions

#### Step 1: Assess Damage (30 seconds)
```bash
cd /root/atlas

# Check DB integrity
sqlite3 data/atlas.db "PRAGMA integrity_check;" 2>&1
# "ok" = healthy; any other output = corrupted

# Check trade count
sqlite3 data/atlas.db "SELECT count(*) FROM trades;" 2>&1

# Check recent backup
RESTIC_PASSWORD="atlas-backup-2026" restic -r /root/backups/restic-repo snapshots | tail -5
```

#### Step 2: Stop Write Traffic Immediately
```bash
# Stop services that write to the DB
sudo systemctl stop atlas-dashboard atlas-telegram-bot

# Notify via Telegram (manual)
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<YOUR_CHAT_ID> \
  -d text="🚨 TRADING HALTED: Database corruption detected. Manual recovery in progress."
```

---

### Recovery Steps

#### Step 3: Restore atlas.db from Restic Backup (2 minutes)
```bash
cd /root/atlas

# Preserve corrupted DB for diagnosis
cp data/atlas.db /tmp/atlas.db.CORRUPTED.$(date +%s)

# Restore from latest snapshot
export RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_REPOSITORY="/root/backups/restic-repo"

RESTORE_TARGET="/tmp/atlas-db-restore-$(date +%s)"
mkdir -p "$RESTORE_TARGET"

restic restore latest --target "$RESTORE_TARGET" \
    --include /root/atlas/data/atlas.db

RESTORED_DB="${RESTORE_TARGET}/root/atlas/data/atlas.db"

# Verify restored DB
sqlite3 "$RESTORED_DB" "PRAGMA integrity_check;"
sqlite3 "$RESTORED_DB" "SELECT count(*) FROM trades;"

# If healthy, replace live DB
cp "$RESTORED_DB" data/atlas.db
echo "Restore complete"
```

#### Step 4: Reconcile with Broker (5 minutes)
```bash
# Compare latest DB snapshot vs live broker
python3 << 'EOF'
from brokers.alpaca_adapter import AlpacaAdapter
import sqlite3, json

# Live broker positions
broker = AlpacaAdapter(paper=False)
broker_positions = {p.symbol: float(p.qty) for p in broker.get_positions()}
print(f"Broker positions: {broker_positions}")

# DB snapshot (latest portfolio_snapshots row)
db = sqlite3.connect('data/atlas.db')
cur = db.execute("SELECT positions FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1")
row = cur.fetchone()
if row:
    db_positions = {p['ticker']: p['shares'] for p in json.loads(row[0])}
    print(f"DB positions: {db_positions}")
    mismatch = {k for k in broker_positions if broker_positions[k] != db_positions.get(k)}
    if mismatch:
        print(f"⚠ Mismatch on: {mismatch}")
    else:
        print("✓ DB matches broker")
else:
    print("⚠ No portfolio snapshot in DB")
db.close()
EOF
```

#### Step 5: Verify Data Integrity
```bash
sqlite3 data/atlas.db << 'EOF'
.echo on
PRAGMA integrity_check;
SELECT count(*) AS trade_count FROM trades;
SELECT date, market_id, equity, broker_equity, positions_count
  FROM equity_curve
  ORDER BY date DESC
  LIMIT 3;
SELECT count(*) AS snapshot_count FROM portfolio_snapshots;
.quit
EOF
```

---

### Verification Checklist

- [ ] `PRAGMA integrity_check` returns `ok`
- [ ] Trade count is plausible (≥ trades in previous backup)
- [ ] Latest equity_curve row matches broker account equity
- [ ] Portfolio snapshots present and not null

### Resume Services
```bash
sudo systemctl start atlas-dashboard atlas-telegram-bot

# Send all-clear notification
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<YOUR_CHAT_ID> \
  -d text="✅ Database reconciliation complete. Services restarted."
```

### Estimated Recovery Time
- **Restore from backup:** 2-5 minutes
- **Broker reconciliation:** 5-10 minutes
- **Full integrity verification:** 10-15 minutes
- **Total:** 15-30 minutes

### What If Backup Is Stale?
If latest backup is >48 hours old:
1. Try an older snapshot: `restic snapshots` then restore by ID
2. Rebuild equity state by reconciling broker API closed order history
3. Update `equity_curve` table manually after confirming broker numbers
4. Flag in `docs/DR_DRILLS.md` that a recovery with stale backup occurred

---

## Scenario 3: Broker API Outage

### Symptoms
- Alpaca API returning errors (500, 503, timeout)
- Cannot fetch positions or place orders
- Dashboard showing stale data
- Timer-triggered services failing with API errors

### Context: Automatic Retry Logic
Atlas has built-in retry logic for transient failures:
- **Exponential backoff:** 1s, 2s, 4s, 8s, 16s (max 5 retries)
- **Automatic failover:** Falls back to cached data for read operations
- **Order queue:** Failed orders retry on next execution window

**What happens automatically:**
- Data fetches retry silently
- Failed orders are logged and queued for next execution window
- Protective stops remain active at broker (independent of API)

---

### When to Intervene Manually

#### Decision Tree
```
API Error Detected
    │
    ├─ Is it market hours?
    │   ├─ YES → Monitor positions (Step 1)
    │   └─ NO → Wait for auto-recovery
    │
    ├─ Duration < 15 min?
    │   ├─ YES → Auto-retry handles it
    │   └─ NO → Manual intervention (Step 2)
    │
    └─ Are protective stops at risk?
        ├─ YES → Emergency hedge (Step 3)
        └─ NO → Monitor and wait
```

### Immediate Actions

#### Step 1: Verify Positions Are Safe (1 minute)
```bash
# Check broker web console (bypasses API)
open https://app.alpaca.markets/paper/portfolio/positions

# Check for protective stop orders
open https://app.alpaca.markets/paper/orders

# Verify:
# - All open positions have stop-loss orders
# - Stop prices are within expected range (1-2 ATR from entry)
# - No positions in extreme drawdown (>5% loss)
```

#### Step 2: Monitor Outage Status (ongoing)
```bash
# Check Alpaca status page
open https://status.alpaca.markets/

# Check Atlas error logs
journalctl -u atlas-dashboard -n 50 --no-pager | grep -i "alpaca\|api\|error"
journalctl -u atlas-telegram-bot -n 50 --no-pager | grep -i "error"

# Set up alert for resolution
while ! curl -s https://api.alpaca.markets/v2/account \
    -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
    -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" > /dev/null 2>&1; do
    echo "$(date): API still down"
    sleep 60
done
echo "$(date): API RESTORED"
```

#### Step 3: Emergency Hedge (only if positions at risk)
```bash
# If API down >30min during market hours and positions losing money:

# Option A: Web console (manual closure)
# 1. Go to https://app.alpaca.markets/paper/portfolio/positions
# 2. Click "Close All" or close individual losing positions

# Option B: Mobile app
# Download Alpaca mobile app and close positions manually

# Option C: Call broker support (last resort)
# Alpaca support: support@alpaca.markets
```

---

### Post-Outage Actions

#### Step 4: Verify System State After Recovery (5 minutes)
```bash
cd /root/atlas

# Refresh position data from broker
python3 << 'EOF'
from brokers.alpaca_adapter import AlpacaAdapter

broker = AlpacaAdapter(paper=False)
positions = broker.get_positions()
print(f"Broker connectivity restored: {len(positions)} open positions")
for p in positions:
    print(f"  {p.symbol}: {p.qty} @ ${p.current_price} | unrealized PnL: ${p.unrealized_pl}")
EOF

# Verify protective stops are active
python3 << 'EOF'
from brokers.alpaca_adapter import AlpacaAdapter

broker = AlpacaAdapter(paper=False)
orders = broker.client.list_orders(status='open')
stop_orders = [o for o in orders if o.type == 'stop']
print(f"Active stop orders: {len(stop_orders)}")
for order in stop_orders:
    print(f"  {order.symbol}: stop @ ${order.stop_price}")
if not stop_orders:
    print("⚠ WARNING: No protective stops found. Re-sync required.")
EOF
```

#### Step 5: Re-Sync Protective Orders (if needed)
```bash
cd /root/atlas
python3 scripts/sync_protective_orders.py --market sp500 --force
```

#### Step 6: Check for Execution Drift
```bash
# Compare latest portfolio snapshot in DB vs live broker
sqlite3 data/atlas.db \
  "SELECT timestamp, total_equity, cash FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 3;"

python3 -c "
from brokers.alpaca_adapter import AlpacaAdapter
b = AlpacaAdapter(paper=False)
a = b.get_account()
print(f'Broker equity: \${a.equity}  Cash: \${a.cash}')
"
```

---

### Verification Checklist

- [ ] Broker API responding normally
- [ ] All open positions have protective stop orders
- [ ] Portfolio snapshot in DB matches broker equity
- [ ] No missed trade executions from outage window
- [ ] All systemd timers still scheduled: `systemctl list-timers atlas-*`

### Estimated Downtime Impact
- **Outage <15 min:** No intervention needed (auto-retry)
- **Outage 15-60 min:** Positions safe if stops are active
- **Outage >60 min:** May miss execution window, manual trade entry required
- **Recovery time after API restore:** 5-10 minutes

---

## Scenario 4: Bad Config Deployed

### Symptoms
- Unexpected trade signals
- Position sizing errors (too large/small)
- Wrong strategy weights
- Missing protective stops
- Trading mode changed (paper vs live)

### Immediate Actions

#### Step 1: Identify Bad Config (30 seconds)
```bash
cd /root/atlas

# Check current config version and mode
python3 -c "import json; c=json.load(open('config/active/sp500.json')); print(c.get('version'), c.get('trading',{}).get('mode'))"

# Check recent config changes
ls -lah config/active/
ls -lah config/archive/ | tail -5
```

#### Step 2: Halt Trading Immediately
```bash
# Stop services that may execute trades
sudo systemctl stop atlas-dashboard atlas-telegram-bot

# Notify (manual curl if bot is down)
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<YOUR_CHAT_ID> \
  -d text="🚨 BAD CONFIG DETECTED: Trading halted. Rollback in progress."
```

---

### Recovery Steps

#### Step 3: Rollback Config (2 minutes)
```bash
cd /root/atlas

# Option A: Rollback to archived version
ls config/archive/  # find the correct backup file
cp config/archive/sp500_backup.json config/active/sp500.json

# Option B: Restore from restic backup
export RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_REPOSITORY="/root/backups/restic-repo"

RESTORE_TARGET="/tmp/atlas-config-restore-$(date +%s)"
mkdir -p "$RESTORE_TARGET"

restic restore latest --target "$RESTORE_TARGET" \
    --include '/root/atlas/config/active/'

cp config/active/sp500.json config/active/sp500.json.BAD.$(date +%s)
cp "${RESTORE_TARGET}/root/atlas/config/active/sp500.json" config/active/

# Verify rollback
python3 -c "import json; c=json.load(open('config/active/sp500.json')); print('Version:', c.get('version'), '| Mode:', c.get('trading',{}).get('mode'))"
```

#### Step 4: Verify Rollback Worked
```bash
python3 << 'EOF'
import json, sys

cfg = json.load(open('config/active/sp500.json'))
checks = [
    (cfg.get('trading', {}).get('approval_required', False) == True, "Approval disabled"),
    (cfg.get('risk', {}).get('require_stop_loss', False) == True, "Stops not required"),
]
failed = [msg for check, msg in checks if not check]
if failed:
    print("❌ Config validation FAILED:")
    for msg in failed:
        print(f"  - {msg}")
    sys.exit(1)
else:
    print("✓ Config validation PASSED")
    print(f"  Version: {cfg.get('version')}")
    print(f"  Mode: {cfg.get('trading', {}).get('mode')}")
EOF
```

#### Step 5: Check for Damage from Bad Config
```bash
# Check DB for recent anomalous trades
sqlite3 data/atlas.db << 'EOF'
SELECT entry_date, ticker, strategy, shares, entry_price,
       (shares * entry_price) AS position_value
FROM trades
ORDER BY rowid DESC
LIMIT 10;
EOF

# Check current broker positions
python3 -c "
from brokers.alpaca_adapter import AlpacaAdapter
b = AlpacaAdapter(paper=False)
for p in b.get_positions():
    print(f'{p.symbol}: {p.qty} shares @ \${p.market_value} | PnL: {p.unrealized_pl}')
"
```

---

### Verification Checklist

- [ ] Config version matches expected
- [ ] Trading mode is correct (live/paper)
- [ ] Approval required = true
- [ ] Protective stops required = true
- [ ] No oversized positions at broker
- [ ] All positions have stop orders

### Resume Services
```bash
sudo systemctl start atlas-dashboard atlas-telegram-bot

curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<YOUR_CHAT_ID> \
  -d text="✅ Config rollback complete. Services resumed."
```

### Estimated Recovery Time
- **Detection + halt:** 1-2 minutes
- **Rollback:** 2-5 minutes
- **Damage assessment:** 5-10 minutes
- **Remediation (if needed):** 10-30 minutes
- **Total:** 15-45 minutes

---

## Scenario 5: Strategy Gone Haywire

### Symptoms
- Multiple rapid losses
- Equity curve dropping sharply
- Position sizing anomalies
- Strategy triggering on bad signals
- Daily loss approaching 2% (circuit breaker threshold)

### Automatic Protection: Circuit Breaker
**Config setting:** `risk.max_daily_drawdown_pct: 0.02` (2% daily loss limit)

**What happens automatically:**
1. Atlas calculates daily drawdown every execution cycle
2. If `(current_equity - starting_equity) / starting_equity >= 0.02`:
   - **All new entries blocked**
   - Telegram alert sent: "🔴 CIRCUIT BREAKER: Daily drawdown 2.0% >= 2.0%"
   - Existing positions remain (protective stops active)
3. Resets at midnight (daily equity snapshot)

**Verification:**
```bash
# Check if circuit breaker triggered today
journalctl -u atlas-dashboard --since today --no-pager | grep -i "circuit breaker\|drawdown"

# Check current drawdown from DB
sqlite3 data/atlas.db \
  "SELECT date, market_id, equity, broker_equity, day_pnl, daily_pnl_pct
   FROM equity_curve
   ORDER BY date DESC
   LIMIT 5;"
```

---

### Manual Intervention

#### Step 1: Assess Situation (1 minute)
```bash
# Check recent trade performance
sqlite3 data/atlas.db << 'EOF'
SELECT entry_date, ticker, strategy, realized_pnl, realized_pnl_pct
FROM trades
WHERE realized_pnl IS NOT NULL
ORDER BY rowid DESC
LIMIT 10;
EOF

# Today's PnL by strategy
sqlite3 data/atlas.db "
SELECT strategy, round(sum(realized_pnl),2) as pnl, count(*) as trades
FROM trades
WHERE date(entry_date) = date('now','localtime')
GROUP BY strategy
ORDER BY pnl ASC;"
```

#### Step 2: Halt Specific Strategy (2 minutes)
```bash
cd /root/atlas

PROBLEM_STRATEGY="momentum_breakout"

python3 << EOF
import json

cfg = json.load(open('config/active/sp500.json'))
cfg['strategies']['$PROBLEM_STRATEGY']['enabled'] = False

with open('config/active/sp500.json', 'w') as f:
    json.dump(cfg, f, indent=2)

print(f"✓ Disabled $PROBLEM_STRATEGY")
EOF

curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
  -d chat_id=<YOUR_CHAT_ID> \
  -d text="🔧 Strategy disabled: $PROBLEM_STRATEGY (manual intervention)"
```

#### Step 3: Close Losing Positions (if needed)
```bash
# Check open positions in DB
sqlite3 data/atlas.db "
SELECT ticker, strategy, entry_price, close_price, unrealized_pnl, unrealized_pnl_pct
FROM position_snapshots
ORDER BY timestamp DESC
LIMIT 10;"

# If closing is needed, do it via broker API or web console
python3 << 'EOF'
from brokers.alpaca_adapter import AlpacaAdapter

broker = AlpacaAdapter(paper=False)
for p in broker.get_positions():
    pct = float(p.unrealized_plpc) * 100
    print(f"  {p.symbol}: ${p.unrealized_pl:7.2f} ({pct:+6.2f}%)")
    if pct < -3:
        print(f"    ⚠ Consider closing (>3% loss)")
        # broker.client.submit_order(symbol=p.symbol, qty=p.qty,
        #     side='sell', type='market', time_in_force='day')
EOF
```

#### Step 4: Investigate Root Cause
```bash
# Look at strategy signal history in DB
sqlite3 data/atlas.db "
SELECT date, ticker, strategy, signal_type, ev, confidence
FROM signals
ORDER BY date DESC
LIMIT 20;"

# Check regime state during losses
sqlite3 data/atlas.db "
SELECT date, regime_state, equity, day_pnl
FROM equity_curve
ORDER BY date DESC
LIMIT 7;"
```

---

### Verification Checklist

- [ ] Circuit breaker triggered correctly (if ≥2% loss)
- [ ] Problem strategy identified and disabled in config
- [ ] Remaining strategies still functional
- [ ] Losing positions closed or monitored
- [ ] Root cause documented

### Recovery Steps

#### Step 5: Fix Strategy or Wait
```bash
# Option A: Tighten strategy parameters
python3 << 'EOF'
import json

cfg = json.load(open('config/active/sp500.json'))
cfg['strategies']['momentum_breakout']['lookback_days'] = 20  # Was 15
cfg['strategies']['momentum_breakout']['atr_stop_mult'] = 2.0  # Was 1.5

with open('config/active/sp500.json', 'w') as f:
    json.dump(cfg, f, indent=2)

print("✓ Strategy parameters updated")
EOF

# Option B: Keep strategy disabled until investigation complete
# Do nothing — wait until next day to re-evaluate
```

#### Step 6: Re-Enable (Next Day)
```bash
python3 << 'EOF'
import json

cfg = json.load(open('config/active/sp500.json'))
cfg['strategies']['momentum_breakout']['enabled'] = True

with open('config/active/sp500.json', 'w') as f:
    json.dump(cfg, f, indent=2)

print("✓ Strategy re-enabled")
EOF
```

### Estimated Recovery Time
- **Detection:** Automatic (circuit breaker) or 5-10 min (manual monitoring)
- **Strategy disable:** 2-5 minutes
- **Position closure:** 5-15 minutes (if needed)
- **Investigation:** 30-60 minutes
- **Fix + backtest:** 1-4 hours
- **Total downtime:** 1-6 hours (trading halted for problem strategy only)

---

## Emergency Contacts

### Broker Support
- **Alpaca Markets**
  - Email: support@alpaca.markets
  - Status: https://status.alpaca.markets/
  - Hours: 24/7 (email), business hours (phone)
  - Account: [Your account ID]

### Infrastructure
- **Server:** `root@pi` (SSH key-based auth)
  - Support: [Your VPS/hosting provider]
  - Console: [URL]

### Data Providers
- **Yahoo Finance:** API (no support, community forums)

### Internal
- **System Owner:** [Your name/contact]
- **Telegram Bot:** @atlas_bot

---

## Credentials & Access

### Location of Secrets
**Primary:** `~/.atlas-secrets.json` (chmod 600)
```json
{
  "ALPACA_API_KEY": "...",
  "ALPACA_SECRET_KEY": "...",
  "TELEGRAM_BOT_TOKEN": "...",
  "TELEGRAM_CHAT_ID": "..."
}
```

**Restic Backup Password:** `atlas-backup-2026`  
(Hardcoded in `/root/scripts/backup-all-projects.sh` — rotate if repo is exposed)

**Backup:** Encrypted offsite storage
- Location: [Your backup location]
- Encryption: [Method]
- Access: [Who has keys]

### Service Accounts
- Dashboard: `http://<server-ip>:8000` (auth: see config)
- Alpaca Console: https://app.alpaca.markets
- Server SSH: `root@pi` (key-based auth)

---

## Post-Incident Checklist

After resolving ANY disaster scenario:

### Immediate (Within 1 hour)
- [ ] All services operational (`systemctl status atlas-*`)
- [ ] Broker positions reconciled
- [ ] `atlas.db` integrity verified (`PRAGMA integrity_check`)
- [ ] Protective stops active
- [ ] Telegram notifications working
- [ ] Incident timeline documented

### Short-term (Within 24 hours)
- [ ] Root cause identified
- [ ] Backup integrity verified (`RESTIC_PASSWORD="atlas-backup-2026" restic -r /root/backups/restic-repo check`)
- [ ] Config rollback tested (if applicable)
- [ ] Monitoring alerts checked
- [ ] `docs/DR_DRILLS.md` updated

### Medium-term (Within 1 week)
- [ ] Root cause analysis written
- [ ] Runbook updated with lessons learned
- [ ] Prevention measures implemented
- [ ] Backup retention verified
- [ ] Disaster recovery drill scheduled

### Long-term (Within 1 month)
- [ ] Offsite backup performed (`tar -czf atlas_offsite_$(date +%Y%m).tar.gz /root/backups/restic-repo/`)
- [ ] System hardening complete
- [ ] Redundancy gaps addressed
- [ ] Documentation updated
- [ ] Quarterly DR test added to calendar

---

## Runbook Maintenance

**Review Schedule:** Quarterly  
**Last Reviewed:** 2026-04-17  
**Next Review:** 2026-07-17  

**Update Triggers:**
- After any disaster scenario occurs
- After major system changes (new services, schema migration, broker changes)
- After backup system changes
- When new failure modes discovered

**Ownership:** Atlas Operations  
**Approval Required:** Yes (test procedures before finalizing)

---

## Appendix: Quick Reference Commands

### Health Check
```bash
cd /root/atlas

# Full system status
systemctl status 'atlas-*' | grep -E "(Loaded|Active|Sub)"

# All enabled units
systemctl list-unit-files 'atlas-*' --state=enabled --no-pager

# Timer next-fire schedule
systemctl list-timers 'atlas-*' --all

# DB health + trade count
sqlite3 data/atlas.db "PRAGMA integrity_check; SELECT count(*) FROM trades;"

# Latest equity snapshot
sqlite3 data/atlas.db \
  "SELECT date, market_id, equity, broker_equity, positions_count FROM equity_curve ORDER BY date DESC LIMIT 3;"

# Broker connectivity
python3 -c "
from brokers.alpaca_adapter import AlpacaAdapter
a = AlpacaAdapter(paper=False).get_account()
print(f'Broker equity: \${a.equity}  Cash: \${a.cash}')
"

# Backup — latest snapshot
RESTIC_PASSWORD="atlas-backup-2026" restic -r /root/backups/restic-repo snapshots | tail -4
```

### Emergency Stop
```bash
# Halt all services that touch the broker
sudo systemctl stop atlas-dashboard atlas-telegram-bot
```

### Emergency Close All Positions
```bash
# Via API
python3 -c "from brokers.alpaca_adapter import AlpacaAdapter; AlpacaAdapter(paper=False).client.close_all_positions()"

# Or web console: https://app.alpaca.markets/paper/portfolio/positions → "Close All"
```

### Logs
```bash
# Service logs (live tail)
journalctl -u atlas-dashboard -f
journalctl -u atlas-telegram-bot -f

# Timer / service run history
journalctl -u atlas-backup.service --since "24 hours ago" --no-pager
journalctl -u atlas-heartbeat-watchdog.service -n 20 --no-pager
journalctl -u atlas-research-window.service -n 10 --no-pager
```

### Backup Quick Restore
```bash
export RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_REPOSITORY="/root/backups/restic-repo"

RESTORE_TARGET="/tmp/emergency-restore-$(date +%s)"
mkdir -p "$RESTORE_TARGET"

restic restore latest --target "$RESTORE_TARGET" \
    --include '/root/atlas/data/atlas.db' \
    --include '/root/.atlas-secrets.json'

# Verify before using
RESTORED_DB="${RESTORE_TARGET}/root/atlas/data/atlas.db"
sqlite3 "$RESTORED_DB" "PRAGMA integrity_check; SELECT count(*) FROM trades;"
```

---

**END OF RUNBOOK**

*This is a living document. Update after every incident and every major system change.*
