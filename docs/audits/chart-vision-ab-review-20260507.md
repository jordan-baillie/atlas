# Chart-Vision A/B Review — 2026-05-07
**Phase C2 — #254: Flip `overlay_vision.log_only: true → false`**

## Executive Summary

**Verdict: FAIL — remaining in `log_only: true` mode.**

21 days of dual-mode logging (2026-04-17 → 2026-05-08, 403 overlay cycles) show that
vision has **zero instances** of the canonical "saved money" signal pattern: cases where
vision flagged `RISK → AVOID` on a ticker that text-summary missed, and the ticker
subsequently moved against the text verdict.

The PASS gate (vision saves money on ≥2 positions) is not met.

---

## Data Summary

| Metric | Value |
|--------|-------|
| Log files analysed | 17 (2026-04-17 → 2026-05-08) |
| Total overlay cycles | 403 |
| Cycles with vision response | 367 (91%) |
| Date range | 2026-04-17 → 2026-05-08 (21 calendar days) |
| Script used | `scripts/review_vision_ab.py --days 30` |

---

## Gate Criterion (per spec)

> **PASS**: vision diverges from text in ways that would have saved money on **2+ positions**.
>
> **FAIL**: divergences are noise, OR vision was wrong on the disagreements.

---

## Signal-Pattern Analysis

### Pattern A — Vision=RISK, Text=OK (the "saved money" pattern)

**Count: 0 instances across 403 entries.**

This is the pattern required for PASS: vision flags `tighten_rec=True` on a ticker while
text-summary did NOT flag that ticker as `tickers_to_avoid` and text `adjust=False`. There
are zero such events across the entire 21-day dataset. Vision has not produced any unique
downside signal that text missed.

### Pattern B — Text=RISK, Vision=OK (opposite direction)

**Count: 57 instances** (SPY×19, QQQ×19, VIX×19).

All 57 cases are "text overlay wanted to tighten/adjust (adjust=True), vision said no
adjust." These are cases where text was MORE conservative than vision — the opposite of
the saved-money pattern. The energy-sector tickers text flagged (INSW, XOP, XLE, UNG,
CVX, OXY) actually moved UP over the review period, suggesting text was overly cautious
and vision's bullish-pass was correct. However, this does not qualify under the PASS gate,
which specifically requires vision to add downside detection beyond text.

### Pattern C — Adjust Divergences (macro-level disagreement)

**Count: 19/403 cycles (4.7%)**

All 19 cases: `text.adjust=True` → cautious, `vision.adjust=False` → bullish.
Direction is consistently TEXT more cautious than VISION, never the reverse.
No ticker-level tighten_rec flags accompany any of these divergence events.

### Pattern D — Vision tighten_rec=True events

**Count: 9 entries, all on 2026-04-21, all index-level (SPY/QQQ/VIX)**

On April 21, vision raised `tighten_rec=True` for SPY/QQQ/VIX citing "V-recovery into
resistance, overbought RSI, vol divergence." However:
- **Both text AND vision agreed** on this day (`text.adjust=True` AND `vision.adjust=True`)
  — these are NOT divergent signals; vision was confirming text, not adding new information.
- **Market outcome**: SPY continued UP after April 21 (SPY Apr 21: $704.08 → Apr 27:
  $715.17, +1.6%). Both text and vision were overly cautious on this date.

| Ticker | Apr 21 close | Apr 22 | Apr 23 | Apr 24 | Apr 27 |
|--------|-------------|--------|--------|--------|--------|
| SPY | $704.08 | $711.21 | $708.45 | $713.94 | $715.17 |

---

## Per-Ticker Agreement Table

| Ticker | text=bull | text=bear | vision=bull | vision=bear |
|--------|-----------|-----------|-------------|-------------|
| SPY | 294 | 109 | 196 | 3 |
| QQQ | 141 | 109 | 43 | 3 |
| VIX | 141 | 109 | 43 | 3 |

Vision flagged bear on only 3 cycles each (all April 21, both text and vision agreed).

**Overall ticker-level divergence rate: 0.0%**
**Unique vision signal per cycle: 0.0 tickers/cycle**

---

## Decision Reasoning

The spec gate requires vision to diverge from text in ways that **save money on ≥2 positions**.
A "position saved" means: vision flags RISK on a specific ticker → text would have entered
or held → ticker moved down → vision's caution prevented a losing trade.

This data shows:
1. Vision has never uniquely flagged a ticker-level risk that text missed (0 Pattern A events).
2. The only `tighten_rec=True` events occurred when text already agreed (Apr 21, both=cautious).
3. The Apr 21 joint-caution was wrong (market went up).
4. Divergences that do exist go the opposite direction (text more cautious than vision, 57 events).
5. Vision's overall stance has been bullish/confirming text, not independently cautious.

**The vision layer is operating as a confirmer of text-overlay, not as an independent
detection layer.** In enforce mode (`log_only=false`), it would have contributed noise or
reinforced text-overlay's existing caution — neither of which justifies the latency cost
and the risk of false positives blocking valid entries.

**Decision: FAIL. `log_only: true` remains unchanged.**

---

## Pre-Apr-17 Log Sweep (Step 4, §258 follow-up)

**Effort estimate: completed, <5 min.**

Pre-Apr-17 overlay logs (`logs/overlay_2026040{2..5}.log-*` through `overlay_20260415.log-20260417`)
contain text-only overlay decisions — the vision system was not active. Zero vision
references appear in any log file prior to 2026-04-17. The A/B logging infrastructure
(writing to `logs/overlay_vision_ab/*.jsonl`) first activated on 2026-04-17. No
additional data can be recovered from pre-Apr-17 archives.

---

## Files Examined

- `logs/overlay_vision_ab/` — 17 JSONL files, 2026-04-17 to 2026-05-08
- `scripts/review_vision_ab.py` — existing A/B review script (existed pre-task)
- `config/active/sp500.json` — `overlay_vision.log_only: true` (unchanged)
- Pre-Apr-17 logs: `logs/overlay_20260402.log-20260404` through `overlay_20260415.log-20260417`

---

## Next Steps

See `tasks/todo.md` for:
- **#314** — Upgrade text-summary feature set (candle-pattern detector, multi-timeframe
  trend agreement, volume profile)
- **#258 follow-up A** — Sweep complete; no data found pre-Apr-17 (vision system didn't
  exist yet)
- Continue `log_only: true` for another review cycle (recommend re-evaluate after 30 more
  trading days or when the signal detection pattern improves — see #314)
