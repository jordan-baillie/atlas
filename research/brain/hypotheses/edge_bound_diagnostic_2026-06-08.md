# Failure Instrumentation — "Gate-bound vs Edge-bound?" (2026-06-08)

**Trigger:** external-agent critique — stop generating families, diagnose *why* nothing survives before family #N+1. Tool: `scripts/diagnose_family_failures.py`.

## Result (23 shm families)
| family | OOStr | OOS Sharpe | cpcv | DSR | deploys broad? |
|---|---|---|---|---|---|
| lowvol_reversal | 80 | +0.77 | 0.95 | **0.986** | ✅ peak 18 — but FAILED write-once holdout (the mirage) |
| momentum (csm) | 25 | −0.40 | 0.38 | 0.84 | ✅ peak 14 |
| value_quality (fundamentals) | 28 | +0.28 | 0.24 | 0.53 | ✅ peak 15 |
| cross_sectional_factor | 35 | −0.52 | 0.23 | 0.22 | ✅ peak 15 |
| …19 single-name technicals… | — | median **−0.40** | mostly <0 | 6× NaN | ❌ peak 2 |

Summary: median OOS Sharpe **−0.40**; only **4/23 deploy as broad books**; 6/23 NaN/degenerate DSR; DSR median 0.218, max 0.986.

## Verdict: EDGE-bound, not gate-bound
- **World #2 (power/gate-bound) REFUTED for the families that matter.** The 4 broad books are well-powered (400–500 trades, peak 14–18) — not trade-starved. The gate isn't the obstacle for anything that could carry it.
- **World #1 (correlated bets) CONFIRMED for 19/23** — all single-name price-reversal flavors, collapsing to 2-name books, negative OOS Sharpe. The "twenty flavors of one idea" critique is correct.
- **World #3 (no liquid-equity edge at scale) is what the powered books show** — near-zero edge (momentum 0.38, value_quality 0.24) or an in-search mirage (lowvol_reversal DSR 0.986, broad deploy, 80 OOS trades) that the **un-peeked holdout killed**. Cleanest possible evidence: full power + broad deployment + 0.99 DSR still fails honest validation.
- **One uncorrelated axis already tested:** value/quality fundamentals (−0.195 corr w/ momentum) — well-powered, broad, **still ~0**. So "new data rescues it" is not untested upside; the basin is edge, not axis.

**Implication:** confirms the 2026-06-06 board pivot with receipts. The system was built to say "no deployable liquid-equity edge at this scale," and it is saying it.

## SF2 insider Gate-0 (the one orthogonal *kind* untested: event-driven flow)
Probed Sharadar SF2 ("Core US Insiders"). Schema is ideal: **`filingdate` (point-in-time) distinct from `transactiondate`**, `transactioncode` (P/S), `isofficer`/`isdirector`/`istenpercentowner`, `transactionshares`/`value`. Prior: Cohen-Malloy-Pomorski (cluster/opportunistic insider buys → forward drift).
- **NOT entitled on our key** — bulk export = 46 KB sample (29 Dow mega-caps); we bought SF1 only. SF2 is a **separate ~$69–99/mo** download-and-own subscription (or the bundle).
- **Open spend decision** (revenue-honesty gate): this is a SECOND paid orthogonal probe AFTER the diagnostic confirmed edge-bound AND the first uncorrelated axis (fundamentals) nulled. Lower EV than SF1's was. Added risk: insider open-market BUYS on small-caps are sparse → cross-sectional coverage/power for a clean monthly rank is uncertain (Gate-0 criterion 3 may be marginal). Free alt = SEC EDGAR Form 4 (public XML) but a real parsing build.
