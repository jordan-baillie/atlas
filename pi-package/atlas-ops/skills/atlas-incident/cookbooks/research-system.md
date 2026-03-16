# Research System — Incident Cookbook

Covers: research queue JSON corruption, stage_candidate clobbering reoptimizer output, double-multiplication in metrics display.

---

## Pattern 18: Research Queue JSON Corruption

**Symptoms:** `json.JSONDecodeError` when reading queue file.

**Root cause:** Lesson #33 — parallel writes without file locking corrupt the queue JSON.

**Fix:**
```bash
# Check queue file
python3 -m json.tool research/queue/*.json 2>&1

# If corrupted, restore from backup or recreate
ls research/queue/
# Look for a .bak or timestamped backup

# If no backup, recreate with an empty queue
echo '{"experiments": []}' > research/queue/queue.json
```

**Verify:**
```bash
python3 -m json.tool research/queue/queue.json > /dev/null && echo "Queue valid"
```

---

## Pattern 19: stage_candidate() Clobbered Reoptimizer Output

**Symptoms:** OOS validation shows identical metrics to active config.

**Root cause:** Lesson #34 — `stage_candidate` overwrites candidate file with a copy of the active config.

**Fix:** Check if candidate file was clobbered:
```bash
diff <(python3 -m json.tool config/active/sp500.json) \
     <(python3 -m json.tool config/candidates/<candidate>.json)
```

If the diff is empty (files are identical), the candidate was clobbered. Re-run the reoptimization:
```bash
# Re-trigger reoptimization (do NOT call stage_candidate manually)
cd /root/atlas && python3 scripts/cli.py -m sp500 reoptimize
```

**Verify:**
```bash
diff <(python3 -m json.tool config/active/sp500.json) \
     <(python3 -m json.tool config/candidates/<candidate>.json) | head -20
# Should show meaningful differences — not empty
```

---

## Pattern 20: Double-Multiplication in Metrics Display

**Symptoms:** CAGR showing `3814%` instead of `38.14%`.

**Root cause:** Lesson #35 — `_pct` metrics are already stored as percent values (e.g., `38.14`), but the display formatter multiplies by 100 again.

**Fix:** Check the display code for the `_ALREADY_PCT` vs `_DECIMAL_PCT` split:
```python
# Wrong — double-multiplies metrics already in percent
display_value = metric_value * 100

# Correct — detect which format the metric uses
ALREADY_PCT_METRICS = {'cagr_pct', 'win_rate_pct', 'max_drawdown_pct'}
if metric_name in ALREADY_PCT_METRICS:
    display_value = metric_value          # already in percent
else:
    display_value = metric_value * 100    # decimal → percent
```

**Verify:**
```bash
cd /root/atlas
python3 -c "
from utils.metrics import format_metric
# CAGR of 0.3814 (decimal) should display as 38.14%
print(format_metric('cagr_pct', 0.3814))
# Should print: 38.14% — not 3814%
"
```
