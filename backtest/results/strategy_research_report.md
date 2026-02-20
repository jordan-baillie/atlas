# Atlas-ASX Strategy Research Report: Top Strategies to Increase Trade Frequency
## Date: 2026-02-18 | Researcher: Agent Zero Deep Research

---

## Executive Summary

After exhaustive research across academic literature, practitioner evidence, and ASX-specific studies, I recommend **7 strategies ranked by expected ROI** to increase trade frequency without degrading existing performance. The current system generates ~50 trades/year with $500 deployed of $5,000 — massive idle capital. The top recommendations target signal timing gaps where neither MR (RSI<35, z-score<-2) nor TF (MA 10/30 crossover + pullback) currently fire.

**Critical architectural insight from code review**: The engine processes strategies sequentially and signals in generation order (not ranked by confidence globally). A new strategy that fires at DIFFERENT times won't crowd existing MR/TF signals — it will fill EMPTY position slots on days when MR/TF have nothing to offer. This is the key to safe expansion.

**Top 3 Quick Wins (ranked by ROI / implementation effort):**
1. **Increase Max Positions to 8** — Zero code, config change only, ~15-25 additional trades/year
2. **Short-Term Mean Reversion (3-5 day)** using different indicators — ~20-30 trades/year
3. **Opening Gap Reversal** on daily data — ~15-25 trades/year

**Projected combined impact**: +50-80 additional trades/year (from current ~50 to ~100-130), potentially doubling system throughput while maintaining quality through complementary signal timing.

---

## Strategy #1: INCREASE MAX POSITIONS FROM 5 TO 8
### Rank: #1 | Expected ROI: HIGHEST (zero implementation cost)

### Concept
The simplest path to more trades. Currently, the system has 5 position slots but generates signals that get BLOCKED when all 5 are full. The first trade plan (Feb 17) generated 32 signals but could only take 5. Increasing to 8 positions with the existing $5,000 equity means ~$625 per position (still above $500 minimum).

### Why It's Complementary
It doesn't add a new strategy — it lets EXISTING high-quality MR and TF signals through that are currently being rejected. The engine already generates these signals; they're just being blocked by the position cap.

### Expected Trade Frequency
+15-25 additional trades/year (from rejected signals that now get filled)

### ASX-Specific Suitability
N/A — this is infrastructure, not strategy. Works on any market.

### Implementation Complexity
**ZERO CODE CHANGES.** Single config parameter:
```json
"risk": {
    "max_open_positions": 8
}
