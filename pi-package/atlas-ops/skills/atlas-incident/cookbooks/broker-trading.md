# Broker & Trading — Incident Cookbook

Covers: $0 equity state corruption, Alpaca DNS failures, protective order sync errors.

---

## Pattern 6: Broker Returns $0 Equity

**Symptoms:** Equity curve shows $0, state file corruption.

**Root cause:** Broker API offline but returning valid-looking response.

**Fix:** NEVER write state when equity is $0. Check `broker_data_valid` flag. If state was corrupted:
```bash
# Check broker status
cd /root/atlas && python3 scripts/cli.py -m sp500 broker

# If broker is actually online with non-zero equity, state was corrupted
# Check equity curve for bad entries
python3 -c "
import json
curve = json.load(open('logs/equity_curve_sp500.json'))
bad = [e for e in curve if e['equity'] == 0]
print(f'Bad entries: {len(bad)}')
if bad: print(bad)
"
```

**Verify:**
```bash
cd /root/atlas && python3 scripts/cli.py -m sp500 broker
# Should show non-zero portfolio equity
```

---

## Pattern 7: Alpaca DNS Resolution Failure

**Symptoms:** Connection timeout to `api.alpaca.markets`.

**Fix:**
```bash
# Check /etc/hosts for the fix
grep alpaca /etc/hosts
# Should have: 34.x.x.x api.alpaca.markets

# If missing, add it
echo "34.232.237.2 api.alpaca.markets" >> /etc/hosts

# Verify
cd /root/atlas && python3 scripts/cli.py -m sp500 broker
```

**Verify:**
```bash
curl -s --max-time 5 https://api.alpaca.markets/v2/clock | python3 -m json.tool
# Should return market clock data, not a connection error
```

---

## Pattern 8: Protective Order Sync Failure

**Symptoms:** `sync_protective_orders.py` errors in logs.

**Fix:**
```bash
# Check the log
tail -30 /root/atlas/logs/sync_protective.log

# Manual sync
cd /root/atlas && python3 scripts/sync_protective_orders.py --market sp500
```

**Verify:**
```bash
tail -10 /root/atlas/logs/sync_protective.log
# Should show "Sync complete" or "No orders to sync" — no exceptions
```
