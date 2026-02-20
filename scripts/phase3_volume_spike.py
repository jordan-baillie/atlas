import json, os, sys, copy, time
from pathlib import Path
import pandas as pd
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

BASELINE = {'total_trades':199,'cagr':0.0834,'sharpe_ratio':0.522,'profit_factor':1.639,'max_drawdown':-0.0746,'win_rate':0.543}

def load_data():
    d = {}
    for pf in sorted(Path('data/cache').glob('*.parquet')):
        if pf.stem == 'IOZ_AX': continue
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) < 100: continue
        d[pf.stem.replace('_AX', '.AX')] = df
    return d

def run_bt(cfg, data, label):
    from backtest.engine import BacktestEngine
    from backtest.metrics import compute_metrics
    t0 = time.time()
    print('  Running [' + label + ']...', flush=True)
    eng = BacktestEngine(cfg)
    trades, equity = eng.run(data)
    m = compute_metrics(trades, equity, cfg)
    s = time.time() - t0
    n = m.get('total_trades', 0)
    c = m.get('cagr', 0) * 100
    sh = m.get('sharpe_ratio', 0)
    pf = m.get('profit_factor', 0)
    dd = m.get('max_drawdown', 0) * 100
    wr = m.get('win_rate', 0) * 100
    print('  [' + label + '] ' + str(int(s)) + 's: trades=' + str(n) + ' CAGR=' + str(round(c,2)) + '% Sharpe=' + str(round(sh,3)) + ' PF=' + str(round(pf,3)) + ' DD=' + str(round(dd,2)) + '% WR=' + str(round(wr,1)) + '%')
    return m, trades

print('=== Phase 3: Volume Spike Confirmation ===')
data = load_data()
print('Loaded ' + str(len(data)) + ' tickers')
with open('config/active_config.json') as f:
    base = json.load(f)

# Arm A: vol modifier OFF (zero boost, zero penalty)
cfg_a = copy.deepcopy(base)
cfg_a['strategies']['mean_reversion']['volume']['surge_boost'] = 0.0
cfg_a['strategies']['mean_reversion']['volume']['dry_penalty'] = 0.0
m_a, trades_a = run_bt(cfg_a, data, 'A-Baseline (vol OFF)')

# Arm B: vol modifier ON (surge_threshold=1.5, surge_boost=0.05, dry_penalty=0)
cfg_b = copy.deepcopy(base)
m_b, trades_b = run_bt(cfg_b, data, 'B-VolSpike (boost ON)')

# Comparison table
keys = ['total_trades','cagr','sharpe_ratio','profit_factor','max_drawdown','win_rate']
scale = {'cagr':100,'max_drawdown':100,'win_rate':100}
print('')
print('=' * 70)
print('  Metric                    A-Base     B-VolSpike       Delta  KnownBase')
print('-' * 70)
for k in keys:
    a = m_a.get(k, 0) or 0
    b = m_b.get(k, 0) or 0
    ref = BASELINE.get(k, 0) or 0
    s = scale.get(k, 1)
    d = b - a
    sign = '+' if d >= 0 else ''
    print('  ' + k.ljust(22) + str(round(a*s,3)).rjust(10) + str(round(b*s,3)).rjust(13) + (sign+str(round(d*s,3))).rjust(11) + str(round(ref*s,3)).rjust(12))

# Volume bucket analysis
print('')
print('=== Volume Bucket PnL Analysis ===')
buckets = {'spike (>=1.5x)': [], 'normal (0.5-1.5x)': [], 'dry (<0.5x)': []}
for t in trades_b:
    if not isinstance(t, dict): continue
    vr = t.get('features', {}).get('volume_ratio')
    pnl = t.get('pnl', 0)
    if vr is None: continue
    if vr >= 1.5: buckets['spike (>=1.5x)'].append(pnl)
    elif vr < 0.5: buckets['dry (<0.5x)'].append(pnl)
    else: buckets['normal (0.5-1.5x)'].append(pnl)
for name, pnls in buckets.items():
    if pnls:
        wr2 = sum(1 for p in pnls if p > 0) / len(pnls)
        avg = sum(pnls) / len(pnls)
        print('  ' + name + ': n=' + str(len(pnls)) + ' avg_pnl=$' + str(round(avg,2)) + ' wr=' + str(round(wr2*100,1)) + '%')
    else:
        print('  ' + name + ': n=0')

# Verdict
improve = sum([
    (m_b.get('cagr',0) or 0) > (m_a.get('cagr',0) or 0) + 0.001,
    (m_b.get('sharpe_ratio',0) or 0) > (m_a.get('sharpe_ratio',0) or 0) + 0.01,
    (m_b.get('profit_factor',0) or 0) > (m_a.get('profit_factor',0) or 0) + 0.01,
    (m_b.get('max_drawdown',0) or 0) > (m_a.get('max_drawdown',0) or 0) + 0.001,
])
keep = improve >= 2 and (m_b.get('cagr',0) or 0) >= (m_a.get('cagr',0) or 0)
verdict = 'ACCEPT — volume boost improves performance' if keep else 'REJECT — revert to info-only'
print('')
print('Improvements: ' + str(improve) + '/4 | Verdict: ' + verdict)

if not keep:
    base['strategies']['mean_reversion']['volume']['surge_boost'] = 0.0
    with open('config/active_config.json', 'w') as f: json.dump(base, f, indent=2)
    print('  config reverted: surge_boost=0.0 (info-only)')
else:
    print('  config kept: surge_boost=0.05')

results = {
    'test': 'phase3_volume_spike', 'date': '2026-02-20',
    'volume_config': base['strategies']['mean_reversion']['volume'],
    'baseline_A': {k: m_a.get(k) for k in keys},
    'volume_B': {k: m_b.get(k) for k in keys},
    'known_baseline': BASELINE,
    'improvements_of_4': improve, 'verdict': verdict, 'kept': keep,
}
with open('backtest/results/phase3_volume_spike.json', 'w') as f:
    json.dump(results, f, indent=2)
print('Results saved to backtest/results/phase3_volume_spike.json')
