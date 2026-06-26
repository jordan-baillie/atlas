#!/usr/bin/env python3
"""Ad-hoc slippage audit over data/live/<strategy>/fills.jsonl.

Read-only. Computes execution-quality metrics and renders terminal charts.
Three slippage references per fill (record_fills.py):
  - slippage_bps          vs decision_px  (CONTAMINATED by stale IEX prices)
  - slippage_open_bps     vs official open (CLEAN headline measure)
  - slippage_prevclose_bps vs prior close   (open-vs-close timing reference)
Sign convention: + = adverse (paid more on BUY / received less on SELL).
"""
import json, statistics, collections, math
from pathlib import Path

LIVE = Path("data/live")
STRATS = ["val_mom_trend_smallcap", "amihud_illiq_tranched_v3"]
MODEL_BPS = 5.0      # config slippage_pct 0.0005
GATE_BAR_BPS = 16.0  # G6 bar (gates.SLIPPAGE_BAR_BPS)

C = dict(r="\033[31m", g="\033[32m", y="\033[33m", c="\033[36m", b="\033[1m",
         d="\033[2m", x="\033[0m", m="\033[35m")

def col(s, k): return f"{C[k]}{s}{C['x']}"
def load(s): return [json.loads(l) for l in (LIVE/s/"fills.jsonl").read_text().splitlines() if l.strip()]

def stats(vals):
    if not vals: return None
    vals = sorted(vals)
    n = len(vals)
    q = statistics.quantiles(vals, n=4) if n >= 4 else [vals[0], statistics.median(vals), vals[-1]]
    return dict(n=n, mean=statistics.mean(vals), median=statistics.median(vals),
                p25=q[0], p75=q[2], p95=vals[min(n-1, int(0.95*n))],
                mn=vals[0], mx=vals[-1],
                std=statistics.pstdev(vals) if n > 1 else 0.0)

def hbar(label, val, vmax, width=40, color="c", suffix=""):
    fill = int(round(width * min(abs(val), vmax) / vmax)) if vmax else 0
    bar = "█"*fill + "·"*(width-fill)
    return f"  {label:<26}{col(bar, color)} {val:>8.1f}{suffix}"

def histogram(vals, title, lo=None, hi=None, bins=18, width=46):
    if not vals: 
        print(f"  {col('(no data)','d')}"); return
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi <= lo: hi = lo + 1
    counts = [0]*bins; bw = (hi-lo)/bins
    for v in vals:
        i = min(bins-1, max(0, int((v-lo)/bw)))
        counts[i] += 1
    cmax = max(counts) or 1
    print(f"  {col(title,'b')}  (n={len(vals)}, range {lo:.0f}..{hi:.0f} bps)")
    for i, c in enumerate(counts):
        edge = lo + i*bw
        fill = int(round(width*c/cmax))
        clr = "g" if edge < MODEL_BPS else ("y" if edge < GATE_BAR_BPS else "r")
        marker = "  <model" if lo+i*bw <= MODEL_BPS < lo+(i+1)*bw else (
                 "  <GATE BAR" if lo+i*bw <= GATE_BAR_BPS < lo+(i+1)*bw else "")
        print(f"   {edge:>7.0f} |{col('█'*fill, clr)}{' '*(width-fill)} {c:>3}{col(marker,'m')}")

def sparkline(series):
    blocks = "▁▂▃▄▅▆▇█"
    if not series: return ""
    mn, mx = min(series), max(series)
    rng = (mx-mn) or 1
    return "".join(blocks[min(7, int(7*(v-mn)/rng))] for v in series)

print(col("\n╔══════════════════════════════════════════════════════════════════════════╗", "b"))
print(col("║              ATLAS SLIPPAGE AUDIT — execution quality report               ║", "b"))
print(col("╚══════════════════════════════════════════════════════════════════════════╝", "b"))
print(f"  modeled cost = {col(f'{MODEL_BPS:.0f} bps','c')}   G6 gate bar = {col(f'{GATE_BAR_BPS:.0f} bps','c')}   "
      f"sign: {col('+','r')} adverse / {col('−','g')} favourable\n")

ALL = {s: load(s) for s in STRATS}

# ─── 1. FILL-RATE / ORDER-LIFECYCLE TABLE ────────────────────────────────────
print(col("━━━ 1. ORDER LIFECYCLE & FILL RATE ━━━", "b"))
print(f"  {'strategy':<28}{'orders':>7}{'filled':>8}{'cancel':>8}{'submit':>8}{'fill%':>8}")
life = {}
for s, rows in ALL.items():
    st = collections.Counter((r.get("status") or "?").lower() for r in rows)
    filled = st["filled"]; cancel = st["cancelled"]; sub = st["submitted"]
    tot = len(rows); fr = 100*filled/tot if tot else 0
    life[s] = dict(tot=tot, filled=filled, cancel=cancel, sub=sub, fr=fr)
    frc = "g" if fr >= 70 else ("y" if fr >= 40 else "r")
    print(f"  {s:<28}{tot:>7}{filled:>8}{cancel:>8}{sub:>8}{col(f'{fr:>7.1f}%','%s'%frc)}")
tot_o = sum(v["tot"] for v in life.values()); tot_f = sum(v["filled"] for v in life.values())
print(f"  {col('PORTFOLIO','b'):<37}{tot_f:>8}{sum(v['cancel'] for v in life.values()):>8}"
      f"{sum(v['sub'] for v in life.values()):>8}{col(f'{100*tot_f/tot_o:>7.1f}%','r')}")
print(f"\n  {col('NOTE','y')}: 'submitted' orders never reached a terminal state — they carry no")
print(f"        fill price and are invisible to the G6 gate (silent fill-quality gap).\n")

# ─── 2. SLIPPAGE METRICS PER REFERENCE ───────────────────────────────────────
REFS = [("slippage_bps","vs decision_px (contaminated)"),
        ("slippage_open_bps","vs official open (CLEAN/headline)"),
        ("slippage_prevclose_bps","vs prev close (timing)")]
print(col("━━━ 2. SLIPPAGE DISTRIBUTION BY REFERENCE PRICE ━━━", "b"))
metrics = {}
for s, rows in ALL.items():
    print(f"\n  {col(s,'c')}")
    metrics[s] = {}
    for key, desc in REFS:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        st = stats(vals)
        metrics[s][key] = st
        if not st:
            print(f"    {desc:<38} {col('NO DATA','r')}")
            continue
        med = st["median"]
        verd = col("PASS","g") if med <= GATE_BAR_BPS else col("FAIL","r")
        print(f"    {col(desc,'b'):<48}")
        print(f"      n={st['n']:<4} median={col(f'{med:>7.1f}','y')} mean={st['mean']:>7.1f} "
              f"p75={st['p75']:>7.1f} p95={st['p95']:>7.1f} worst={col(f'{st['mx']:>8.1f}','r')}  [{verd} vs {GATE_BAR_BPS:.0f}]")

# ─── 3. CLEAN-MEASURE HISTOGRAM (the headline) ───────────────────────────────
print(col("\n━━━ 3. CLEAN SLIPPAGE HISTOGRAM (vs official open) ━━━", "b"))
for s, rows in ALL.items():
    vals = [float(r["slippage_open_bps"]) for r in rows if r.get("slippage_open_bps") is not None]
    print(f"\n  {col(s,'c')}")
    if vals:
        clip = [min(v, 600) for v in vals]
        histogram(clip, "open-slippage (bps, clipped@600)", lo=-100, hi=600)
    else:
        print(f"    {col('NO CLEAN MEASURE — slippage_open_bps never backfilled','r')}")

# ─── 4. BUY vs SELL DECOMPOSITION ────────────────────────────────────────────
print(col("\n━━━ 4. ADVERSE SLIPPAGE BY SIDE (clean measure) ━━━", "b"))
for s, rows in ALL.items():
    print(f"\n  {col(s,'c')}")
    any_data = False
    for side in ("BUY","SELL"):
        vals = [float(r["slippage_open_bps"]) for r in rows
                if r.get("slippage_open_bps") is not None and r.get("side")==side]
        st = stats(vals)
        if st:
            any_data = True
            vmax = max(50, abs(st["median"])*1.4)
            clr = "g" if st["median"] <= MODEL_BPS else ("y" if st["median"] <= GATE_BAR_BPS else "r")
            print(hbar(f"{side} (n={st['n']}) median", st["median"], vmax, color=clr, suffix=" bps"))
    if not any_data:
        print(f"    {col('(no clean measure)','d')}")

# ─── 5. DAILY MEDIAN SLIPPAGE TIME-SERIES ────────────────────────────────────
print(col("\n━━━ 5. DAILY MEDIAN SLIPPAGE TREND ━━━", "b"))
for s, rows in ALL.items():
    key = "slippage_open_bps"
    byday = collections.defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            byday[r["date"]].append(float(r[key]))
    if not byday:
        key = "slippage_bps"
        for r in rows:
            if r.get(key) is not None:
                byday[r["date"]].append(float(r[key]))
    days = sorted(byday)
    meds = [statistics.median(byday[d]) for d in days]
    print(f"\n  {col(s,'c')}  ({key})")
    if days:
        print(f"    {col(sparkline(meds),'c')}")
        print(f"    {days[0]} → {days[-1]}   daily medians: " +
              " ".join(col(f'{m:.0f}', 'r' if m>GATE_BAR_BPS else 'g') for m in meds))
    else:
        print(f"    {col('(no data)','d')}")

# ─── 6. MODEL vs REALIZED & GATE STANDING ────────────────────────────────────
print(col("\n━━━ 6. MODEL-vs-REALIZED & G6 GATE STANDING ━━━", "b"))
print(f"  {'strategy':<28}{'headline median':>18}{'×model':>9}{'×bar':>8}  gate")
for s in STRATS:
    m = metrics[s]
    st = m.get("slippage_open_bps") or m.get("slippage_bps")
    src = "open" if m.get("slippage_open_bps") else "decision(contam)"
    if not st:
        print(f"  {s:<28}{col('NO DATA','r'):>27}"); continue
    med = st["median"]
    mult_model = med/MODEL_BPS; mult_bar = med/GATE_BAR_BPS
    verd = col("PASS","g") if med <= GATE_BAR_BPS else col("FAIL","r")
    print(f"  {s:<28}{col(f'{med:>10.1f} bps','y')} ({src[:5]}){mult_model:>8.1f}×{mult_bar:>7.1f}×  {verd}")
print(f"\n  {col('model 5 bps','d')} │ {col('gate bar 16 bps','d')} │ realized headline medians above show "
      f"true execution cost\n")
