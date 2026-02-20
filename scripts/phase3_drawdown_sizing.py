import json, os, sys, copy, time
from pathlib import Path
import pandas as pd
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

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
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap
    t0 = time.time()
    print('  Running [' + label + ']...', flush=True)
    strategies = [MeanReversion(cfg), TrendFollowing(cfg), OpeningGap(cfg)]
    eng = BacktestEngine(cfg)
    result = eng.run_walkforward(data, strategies)
    m = result.metrics
    s = time.time() - t0
    n  = m.get('total_trades', 0)
    c  = m.get('cagr', 0); c = c*100 if abs(c)<2 else c
    sh = m.get('sharpe', m.get('sharpe_ratio', 0))
    pf = m.get('profit_factor', 0)
    dd = m.get('max_drawdown', 0); dd = dd*100 if abs(dd)<2 else dd
    wr = m.get('win_rate', 0); wr = wr*100 if wr<2 else wr
    print(f'  [{label}] {int(s)}s: trades={n} CAGR={c:.2f}% Sharpe={sh:.3f} PF={pf:.3f} DD={dd:.2f}% WR={wr:.1f}%')
    return m

print('=== Phase 3: Drawdown-Based Position Sizing A/B Test ===')
print('Tiers: DD<2%=1.0x | DD 2-4%=0.85x | DD 4-6%=0.70x | DD 6%+=0.55x')
data = load_data()
print(f'Loaded {len(data)} tickers')
with open('config/active_config.json') as f:
    base = json.load(f)

# Arm A: dynamic_sizing OFF (pure fixed risk - baseline)
cfg_a = copy.deepcopy(base)
cfg_a['dynamic_sizing']['enabled'] = False

# Arm B: dynamic_sizing ON, ONLY equity_curve_scaling with graduated tiers
cfg_b = copy.deepcopy(base)
cfg_b['dynamic_sizing']['enabled'] = True
cfg_b['dynamic_sizing']['confidence_scaling']['enabled'] = False
cfg_b['dynamic_sizing']['volatility_scaling']['enabled'] = False
cfg_b['dynamic_sizing']['equity_curve_scaling']['enabled'] = True
cfg_b['dynamic_sizing']['base_risk_pct'] = 0.005
cfg_b['dynamic_sizing']['min_risk_pct'] = 0.0025
cfg_b['dynamic_sizing']['max_risk_pct'] = 0.005  # cap = base (no upside)

print('\nArm A: Fixed risk (no drawdown scaling) - baseline')
m_a = run_bt(cfg_a, data, 'A-Fixed')

print('\nArm B: Graduated drawdown scaling (2/4/6% tiers)')
m_b = run_bt(cfg_b, data, 'B-DDScale')

# Extract & normalize metrics
def get(m, *keys):
    for k in keys:
        if k in m: return m[k]
    return 0

cagr_a = get(m_a,'cagr'); cagr_a = cagr_a*100 if abs(cagr_a)<2 else cagr_a
cagr_b = get(m_b,'cagr'); cagr_b = cagr_b*100 if abs(cagr_b)<2 else cagr_b
sh_a   = get(m_a,'sharpe','sharpe_ratio')
sh_b   = get(m_b,'sharpe','sharpe_ratio')
pf_a   = get(m_a,'profit_factor')
pf_b   = get(m_b,'profit_factor')
dd_a   = get(m_a,'max_drawdown'); dd_a = dd_a*100 if abs(dd_a)<2 else dd_a
dd_b   = get(m_b,'max_drawdown'); dd_b = dd_b*100 if abs(dd_b)<2 else dd_b
wr_a   = get(m_a,'win_rate'); wr_a = wr_a*100 if wr_a<2 else wr_a
wr_b   = get(m_b,'win_rate'); wr_b = wr_b*100 if wr_b<2 else wr_b
tr_a   = get(m_a,'total_trades')
tr_b   = get(m_b,'total_trades')

print('\n=== RESULTS SUMMARY ===')
print(f'                    Fixed        DDScale     Delta')
print(f'Total Trades:       {tr_a:<12} {tr_b:<12} {tr_b-tr_a:+}')
print(f'CAGR:               {cagr_a:<12.2f} {cagr_b:<12.2f} {cagr_b-cagr_a:+.2f}%')
print(f'Sharpe:             {sh_a:<12.3f} {sh_b:<12.3f} {sh_b-sh_a:+.3f}')
print(f'Profit Factor:      {pf_a:<12.3f} {pf_b:<12.3f} {pf_b-pf_a:+.3f}')
print(f'Max Drawdown:       {dd_a:<12.2f} {dd_b:<12.2f} {dd_b-dd_a:+.2f}%')
print(f'Win Rate:           {wr_a:<12.1f} {wr_b:<12.1f} {wr_b-wr_a:+.1f}%')

# Scoring: Sharpe+DD are primary targets; CAGR+PF+WR secondary
# DD improvement = lower value is better (dd is negative pct stored as positive here)
improvements = sum([
    sh_b  > sh_a,           # higher Sharpe
    dd_b  < dd_a,           # lower drawdown
    cagr_b > cagr_a * 0.97, # CAGR within 3% of baseline (acceptable loss)
    pf_b  > pf_a,
])

print(f'\nDD scaling improved/maintained {improvements}/4 metrics')
if improvements >= 3:
    verdict = 'ENABLE graduated drawdown scaling'
elif improvements == 2:
    verdict = 'MARGINAL - review Sharpe and DD improvement magnitude'
else:
    verdict = 'DISABLE drawdown scaling'
print(f'Verdict: {verdict}')

results = {
    'arm_a': m_a, 'arm_b': m_b,
    'summary': {
        'cagr_delta': round(cagr_b - cagr_a, 3),
        'sharpe_delta': round(sh_b - sh_a, 3),
        'pf_delta': round(pf_b - pf_a, 3),
        'dd_delta': round(dd_b - dd_a, 3),
        'wr_delta': round(wr_b - wr_a, 3),
        'improvements_of_4': improvements,
        'verdict': verdict,
    }
}
out = 'backtest/results/phase3_drawdown_sizing.json'
with open(out, 'w') as f:
    json.dump(results, f, indent=2, default=str)
print(f'\nResults saved to {out}')
