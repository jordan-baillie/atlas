# Vision Overlay — Decision Log

## 2026-04-17: Initial validation (Wave 3)

### Vision response (Claude Opus 4.7, SPY 1Y daily, ~4000px PNG)

```json
{"trend":"sideways","key_levels":{"support":[630.0,650.0],"resistance":[685.0,697.0]},"pattern":"Distribution top after year-long uptrend; lower high in Feb-Mar 2026 with sharp selloff to 630 support followed by V-shaped recovery retesting prior range — potential double top / rounded top rollover, but recent bounce on rising volume keeps it range-bound rather than confirmed downtrend","confidence":0.62}
```

### Text-only equivalent (current chart_intel output, SPY excerpt)

```
Broadly bullish — SPY above 200/50SMA, low-volume — conviction suspect
```

### Verdict

Vision and text diverge materially: text says "broadly bullish" while vision scores the same chart "sideways" (confidence 0.62) and flags a distribution-top / potential double-top pattern with specific support at 630-650 and resistance at 685-697 — structural detail the single-line text summary omits entirely. However, the pattern description alone is insufficient to flip the flag ON; we need multi-ticker coverage and A/B outcome tracking. **Recommend: keep flag=OFF, run 5-day A/B log review comparing text-only vs vision-augmented signals, then reassess.**

## Implementation Notes

- `overlay_vision.enabled` defaults to `false` in `config/active/sp500.json` — zero production impact
- Vision branch is lazy-imported; no overhead when flag is off
- Circuit breaker (`utils.claude_circuit_breaker`) also guards vision path
- Model: `claude-opus-4-8` (required for image attachment support)
- Chart render pipeline: `overlay.sources.chart_renders` (mplfinance, daily 1Y + hourly 1W)
- All 15 integration tests pass (`tests/overlay/test_vision_integration.py`)
