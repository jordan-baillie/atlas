# Post-Batch Roadmap — Next-Month Sequencing

**Date:** 2026-05-18
**Author:** Planning Lead
**Status:** Decision memo — informs Wave 2+ planning
**Scope:** Sequencing of remaining P1 work after today's 26-task batch completes

---

## 1. What today's batch unblocks

Today's batch closes 26 P1 tasks across reconcile, dual-write validation, god-file structural prep (Phase 8 TODOs), and dashboard/risk hygiene. After merge, the following gates open:

| Gate | Condition to clear | What it unblocks |
|------|-------------------|------------------|
| **Dual-write 5/5** | 5 consecutive nights of broker_orders + trades agreement | #267 bridge cut, #276 reconcile.py retirement, #278 state machine prep |
| **Reconcile canonical** | `core/reconcile.py` is sole writer for ≥7 days | Live_executor PR3 commit 9 (reconciler extract) |
| **Phase 8 TODO scaffolding** | Structural comments in place | #226 god-file decomp (this memo's central question) |
| **Healthz green for 7 days** | No red sections in hourly healthz | Higher-cadence research deployments |

## 2. Once dual-write hits 5/5 — what becomes actionable

In strict dependency order:

1. **#267 — Dual-write bridge CUT** (1-2 days). Removes legacy dual-write code path. Stops writing to deprecated tables. Pure subtraction — high confidence.
2. **#276 — `reconcile.py` retirement** (2-3 days). Deletes the legacy reconcile script. `core/reconcile.py` is now canonical. Validates the cutover stuck.
3. **#278 — Trade state machine** (4-5 days). DB-enforced state transitions via `db.transition_trade()`. Eliminates the most complex cross-concern coupling in `live_executor.py`. **Hard prerequisite for #226 PR3 commits 11-12.**
4. **#226 PR2 — `protective_orders` extraction** (3 days). Independent of #267/#276/#278. Can run parallel with #276 or #278.
5. **#226 PR3 — Reconciler extract + slim core** (5 days). Requires #267 + #276 + #278 all landed.
6. **Candidate #6 — `alpaca/broker.py` `sync_all_protective_orders` extraction** (4 days). Independent of live_executor work. Can run any time after #226 PR2.

## 3. Recommended sequencing — #226 vs #267 vs #276

**Three feasible orderings:**

| Order | Description | Calendar | Risk |
|-------|-------------|----------|------|
| **A — Serial (recommended)** | #267 → #276 → #278 → #226 PR2 → #226 PR3 (Candidate #6 parallel from week 3) | 5-6 weeks | LOW |
| **B — Parallel reconcile + decomp** | #267 + #226 PR2 in week 1; #276 + #278 in week 2; #226 PR3 + Candidate #6 in weeks 3-4 | 4 weeks | HIGH |
| **C — Decomp-first** | #226 PR2 + Candidate #6 weeks 1-2; #267 + #276 + #278 weeks 3-5 | 5 weeks | MEDIUM (#226 PR3 blocked till week 4) |

**Recommendation: Order A (strict serial with one parallel track).**

Rationale: Atlas runs live capital ($5,289 equity, 7 open positions, 23:15 AEST cron). Concurrent changes to reconcile (#267/#276), state coupling (#278), and the executor monolith (#226) compound blast radius across overlapping code paths. The marginal calendar saving from Order B (~1 week) is not worth the risk of a multi-system incident with <11 hours to recover before the next live execution window.

**Hard rules for Order A:**
- 1-week stability gate after #267 cutover before #276 deletion (validates the cutover stuck).
- 1-week stability gate after #278 before #226 PR3 commit 11 (validates state machine handles real entries/exits).
- Candidate #6 may run parallel with #226 PR2 starting week 3 since they touch different broker modules (`alpaca/broker.py` vs `live_executor.py`) and the protective-orders extraction lifts to a new `brokers/protective_orders.py` module that both will eventually consume.

## 4. Risk of doing all three in same window

| Risk | Severity | Why |
|------|----------|-----|
| Multi-system incident with <11h recovery | CRITICAL | 23:15 AEST cron is the cutoff; an incident found at 22:00 has no recovery runway if multiple subsystems changed same week |
| Reconcile drift masking executor bugs | HIGH | If #267 cutover bug appears at the same time as #226 PR3 reconciler extract, attribution is impossible |
| Test suite green is per-PR, not per-system | HIGH | Three concurrent PR streams hitting the same modules makes regression triage exponentially harder |
| Rollback complexity compounds | MEDIUM | Reverting one PR in a 3-PR-deep stack can re-introduce a bug from an earlier revert |

**Mitigation in Order A:** Each PR has a single owner, a stability gate, and a known-good baseline. Rollback is `git revert <sha>` + redeploy (~5 min).

## 5. Cronus successor — decision matrix

**Context:** Cronus (paper commodity futures via IBKR) archived 2026-05-18. Three forward paths:

| Option | Description | Effort | EV |
|--------|-------------|--------|-----|
| **A** — Non-IB commodity futures (Schwab/Tastytrade/Tradovate) | New broker adapter + paper-trading infra rebuild | ~3 weeks adapter + 4 weeks paper validation = 7 weeks | UNPROVEN — Cronus had zero live track record; the strategy was the bottleneck, not the broker |
| **B** — Atlas-only focus | All bandwidth on Atlas roadmap (#267/#276/#278/#226/Candidate #6) | 0 setup; immediate ROI on existing work | HIGH — Atlas has proven edge ($5,289 equity, growing, live track record) |
| **C** — Sidestep via Alpaca commodity ETFs | Use existing Alpaca adapter for commodity exposure via futures-tracking ETFs (USO, UNG, DBA, GLD, SLV) | ~1 week adapter changes (universe + symbol mapping) | MEDIUM — no new broker; gives commodity exposure via Atlas's existing infrastructure |

### Recommendation: Option B (Atlas-only) for the next 6 weeks; reconsider Option C in Q3

**Reasoning:**
- Option A's 7-week setup cost would consume the entire window of the #226+#267+#276+#278+Candidate #6 sequence. The opportunity cost is the entire Atlas P1 roadmap.
- Cronus's value was *strategy validation*, not broker access. Spinning up a new broker without an edge-validated strategy is high-cost, zero-EV.
- Option C is the lowest-cost commodity-exposure path. It belongs in the Q3 universe expansion bucket (~`commodity_etfs` universe — already partially scaffolded in `config/markets.json`), not as a standalone successor project.

**Trigger conditions to revisit:**
- If a specific commodity-futures strategy with backtested edge appears → re-evaluate Option A
- If Atlas universe expansion saturates and incremental edge requires non-equity exposure → activate Option C
- If Alpaca expands product into actual futures → reconsider as a Cronus revival path (zero adapter cost)

## 6. Decision summary

1. **Sequencing:** Order A (strict serial; Candidate #6 parallel from week 3). 5-6 weeks calendar.
2. **Same-window risk:** Do NOT parallelize #267 + #276 + #226. Stability gates between each.
3. **Cronus successor:** Option B (Atlas-only) for next 6 weeks. Park Option C for Q3 universe expansion. Reject Option A absent a validated commodity-futures strategy.

---

**Next review:** 2026-06-15 (post #267 cutover; re-evaluate #226 PR2 readiness).
