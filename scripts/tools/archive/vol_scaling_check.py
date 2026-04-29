"""Check vol scaling readiness and current state."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.config import get_active_config
from backtest.vol_scaling import VolatilityScaler
from db.atlas_db import get_equity_curve

config = get_active_config("sp500")
vs_cfg = config.get("vol_scaling", {})
scaler = VolatilityScaler(config)
eq = get_equity_curve("sp500")
if eq:
    equities = [r["equity"] for r in eq if r.get("equity")]
    for i in range(1, len(equities)):
        if equities[i-1] > 0:
            scaler.update((equities[i] - equities[i-1]) / equities[i-1])
print(json.dumps(scaler.diagnostics(), indent=2))
