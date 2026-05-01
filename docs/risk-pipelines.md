# Risk Pipelines — `compute_daily_risk` vs `precompute_risk`

Both scripts produce cached risk data for the Atlas API. They are **not
redundant** — they run at different times and produce overlapping but
distinct artefacts.

---

## `scripts/compute_daily_risk.py`

**Trigger:** cron at `0 23 * * *` AEST (= 13:00 UTC) — immediately after US
market open, while prices are live.

**What it produces:**

| Artefact | Table / Store | Notes |
|---|---|---|
| Portfolio VaR (1d/5d, 95%) | `portfolio_risk` | Regime-aware; uses current broker prices |
| Vol cones | `vol_cones` (per-ticker) | Per open position |
| Regime distributions | `regime_distributions` | Skipped if updated <23h ago |
| Strategy EV scores | `strategy_ev` | From `signals.ev_scorer` |
| Regime forward MC forecast | `regime_forecast` | Monte Carlo paths from current regime |
| Ruin probability | `ruin_probability` | `floor_pct=0.70` |

**API consumers:** `GET /api/positions/risk` (portfolio_risk, vol_cone),
`GET /api/risk/ruin`, `GET /api/signals/ev`.

---

## `scripts/precompute_risk.py`

**Trigger:** systemd timer `atlas-risk-precompute.timer` at ~22:30 UTC
(= ~08:30 AEST next day) — after US market close when final prices are
available.

**What it produces:**

| Artefact | Table / Store | Notes |
|---|---|---|
| Portfolio VaR/CVaR | `portfolio_risk` | Same table; overwrites with EOD prices |
| Regime transition matrix (90d) | `regime_transitions_cache` | Markov matrix across 6 regime states |
| Ruin probability | `ruin_probability` | Also uses `floor_pct=0.70` |

**API consumers:** `GET /api/positions/risk`, `GET /api/risk/ruin`,
`GET /api/regime/transitions`.

**On-demand triggers:** `POST /api/risk/ruin/refresh` also calls
`precompute_risk` in the background; `/api/regime/transitions` triggers
a bg refresh when cache is stale (>24h).

---

## Schedule summary

```
23:00 AEST (13:00 UTC)  compute_daily_risk  cron    live-price snapshot
~08:30 AEST (22:30 UTC) precompute_risk     systemd EOD final-price pass
```

The two runs overlap on `portfolio_risk` and `ruin_probability`. The
EOD `precompute_risk` run is authoritative for end-of-day reporting;
the live `compute_daily_risk` run provides intraday freshness for the
dashboard during the US session.

**Neither script is redundant.** `compute_daily_risk` also produces
vol cones, strategy EV, and regime forward MC forecasts which
`precompute_risk` does **not** compute.
