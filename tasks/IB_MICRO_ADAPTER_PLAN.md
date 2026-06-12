# IB Micro-Futures Adapter — completion plan

**Why**: board sequencing (memo 2026-06-09, 5-0) — IB micro-futures is the board's
preferred first real-capital venue: natively tradable at ~$5K with an AUM floor of
$10–15K, vs $25K for the borrow-dependent smallcap equity books.

**Deadline correction (2026-06-12, caught during scoping)**: the original "must land by
2026-08-28" sequencing was tied to the Midas carry forward-verdict — but Midas was
killed 2026-06-10, so THAT VERDICT WILL NEVER ARRIVE (wiki overview, binding events).
The carry+trend book is orphaned pending a fresh carry leg with its own ~3-month
forward run. **New trigger discipline**: the adapter must be live-verified before any
futures-tradable book STARTS a forward run, so the forward window doubles as the
execution-path shakedown. No hard date; the phased timeline below is kept as the
working pace (it was comfortable even under the old deadline).

## Ground truth (scouted 2026-06-12) — most of this already exists

| Piece | State | Evidence |
|---|---|---|
| `brokers/ib/broker.py` (ib_insync, TWS/Gateway) | **BUILT** | 226 lines, MICRO_FUTURES table (MES/MNQ/M2K/MYM/MGC/SIL/MCL/M6E/MBT), front-month continuous contracts, injectable client |
| `brokers/ib_web/broker.py` (headless Web REST) | **BUILT** | 255 lines, CP-Gateway or api.ibkr.com Bearer, injectable HTTP, order-reply-confirm loop |
| `BrokerAdapter` contract + registry | **WIRED** | both registered in `brokers/registry.py` behind import guards |
| Unit tests | **PASSING** | tests/brokers/ 169 passed (fake client/HTTP) |
| `ib-insync` dependency | installed (0.9.86) | — |
| Referenced `tasks/IB_WEBAPI_INTEGRATION.md` | **MISSING** (docstring points at it; lost in the repo restructure) | — |

**The gap is operational, not code**: nothing has ever talked to a real IB endpoint.
No IB account credentials on the box, no gateway running, no end-to-end order placed,
and the BOREAS signal → `target.json` → executor path has never run with futures
(integer contracts, multipliers, no OPG mechanics — different from the equity books).

## Remaining work (phased, ~2.5 months runway)

### Phase A — account + transport decision (human-in-loop, START EARLY: lead times)
1. **Open/verify IB paper account** (human: needs IBKR login; paper account is free).
   This is the long-pole item — IB account approval can take days; do first.
2. **Transport decision (RESOLVED 2026-06-12 via deep research)**: primary = `ib`
   adapter (ib_insync) against **dockerized IB Gateway + IBC** (`gnzsnz/ib-gateway-docker`,
   `TRADING_MODE=paper`) — the field-proven unattended stack. Paper credentials bypass
   IB-Key 2FA entirely; IBC absorbs the daily auto-restart and ~weekly full re-login.
   Hardening: PIN a known-good Gateway+IBC version pair (auto-restart regressions are
   version-pair specific, e.g. IBC 3.21.0 + GW 10.34.1c); ensure the paper account holds
   simulated funds (unfunded paper accounts break the restart token — IbcAlpha/IBC#345);
   docker restart policy + health checks against silent Gateway exits.
   Fallback = `ib_web` + CP-Gateway/IBeam (REST; flappier session model — tickle keepalive
   + frequent re-auth). Check during signup: IBKR first-party retail OAuth Web API — if
   paper accounts are supported it removes the gateway process entirely (paper parity
   UNVERIFIED as of 2026-06; don't bet on it).
3. Secrets: add `ib` block to `~/.atlas-secrets.json` (account id, bearer/credentials),
   following the existing alpaca pattern.

### Phase B — live-paper verification (the adapter's G1 moment)
4. Recreate `tasks/IB_WEBAPI_INTEGRATION.md` endpoint map (the docstring's dangling
   reference) as part of verifying each endpoint against the real gateway.
5. End-to-end smoke vs IB paper: auth → account info → MES quote → place 1-contract
   LIMIT far from market → confirm SUBMITTED → cancel → confirm CANCELLED. Then one
   real 1-contract MARKET fill + flatten (paper $) to verify fills/positions parse.
6. Reconcile + kill-switch dry-run against the IB paper account (the substrate claims
   broker-agnostic; prove it — esp. PositionInfo with multipliers ≠ share-count).

### Phase C — BOREAS book wiring
7. Futures-aware target path: BOREAS carry+trend emits target weights over micro
   symbols; executor must round to INTEGER contracts at multiplier-adjusted notional
   (vs fractional-ish equity sizing) and respect per-contract margin. Slippage/G6
   expectations need futures cost model (half-spread + commission ≈ $0.25-0.75/side
   on micros), not equity bps.
8. Shadow-deploy the BOREAS book on IB paper ≥2 weeks BEFORE 08-28 so fills/recon
   evidence exists when the verdict arrives (mirrors the val_mom shadow pattern —
   and avoids repeating the capital/tif deploy bugs of 2026-06-12 under deadline).
9. Calendar roll policy: front-month rolls (MES quarterly, MCL monthly) — decide
   roll trigger (days-to-expiry) + who executes it (executor extension; pre-registered).
   **Progress 2026-06-12 (built credential-free):** two latent roll bugs fixed in the
   `ib` adapter — (a) orders were built on CONTFUT (continuous) contracts, which IB
   REJECTS for orders (data-only); `_contract` now does the documented two-step
   resolution (ContFuture → front-month conId → concrete orderable FUT); (b) the
   contract cache lived for the broker's lifetime so a long-lived process would never
   re-resolve after a roll — cache now cleared on connect(). Plus `check_rolls()`:
   detects positions held in a no-longer-front-month contract (where a naive reducing
   order would OPEN A CALENDAR SPREAD instead of closing) and returns the held/front
   contract pair for an explicit flatten. 4 new tests (fake two-step client). Still
   open: who calls check_rolls() daily + the pre-registered roll trigger.

### Timeline (working pace — no hard deadline after the Midas correction)
- **by 06-20**: Phase A (account live, transport chosen, keepalive unit running)
- **by 07-15**: Phase B (verified end-to-end vs IB paper, endpoint map doc rebuilt)
- **by 08-10**: Phase C wired; ready to host the next futures-tradable forward run
- The adapter is the venue for ANY future cross-asset/futures PASS (incl. crypto MBT
  micro-bitcoin) — it stays valuable regardless of which book confirms first

### Non-goals
- No real capital (board gate unchanged; this is paper infrastructure).
- No Stage D allocator (deferred to AUM gate).
- No new gates; no changes to the two equity forward books.

### Risks
- **CP-Gateway session brittleness** (known IB pain): mitigated by keepalive unit +
  fallback transport decided in Phase A, not under deadline in August.
- **ib-insync 0.9.86 is pre-rename** (project renamed ib_async after author's death,
  2024): works today; pin it, note migration path to `ib_async` if issues surface.
- **Carry leg is unresolved**: `carry_returns()` was removed 2026-06-10 (Midas killed)
  and the carry+trend book is orphaned. The most likely fresh carry leg is the
  elite-pool crypto delta-neutral funding-carry (NEAR-MISS) — but that trades on
  Binance/Bybit perps, NOT IB. If crypto-carry confirms first, the first real-capital
  venue question reopens (crypto exchange adapter vs IB) — flag to board at that point.
- **Boreas trend pipeline staleness**: /root/boreas data last ingested 2026-06-08, no
  timer found. The validated trend leg's data must be re-ingestable before any
  futures forward run — verify in Phase C.
