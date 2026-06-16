# Virtual-book fill reconciliation — root-cause fix for book↔broker drift

**Status:** SCOPED, not yet implemented. Follow-on to the invariant guard (`reconcile_books.py`,
atlas `e1ad3812`, 2026-06-16). **BLOCKS re-enabling `atlas-live-shadow.timer`** (paused 2026-06-16 —
re-enable: `ln -sf /root/atlas/systemd/atlas-live-shadow.{timer,service} /etc/systemd/system/ &&
systemctl enable --now atlas-live-shadow.timer`).

## The bug (root cause of the 119-phantom / 10-orphan / 45-mismatch drift)
`atlas/execution/daily.py` (shadow path, ~L198–204) updates the virtual book the moment an order is
**accepted**:
```python
filled = {(r.ticker, r.side) for r in rep.results if r.success}   # success == ACCEPTED, not filled
for o in rep.orders:
    if (o.ticker, o.side) in filled:
        book.apply_fill(o.ticker, o.side.value, o.qty, o.ref_price, mult)  # assumes FULL fill at REF price
```
The shadow loop runs at 00:30 UTC, **inside the Alpaca OPG window** — orders are accepted now but the
actual fill (or non-fill) is the next morning's open, ~14h later. So:
- An OPG order that never fills (HTB short, halt, no-open) is still written into the book → **phantom**.
- Partial fills / fills at a different price are recorded as full-qty-at-ref-price → **qty + cash error**.
- Never reconciled back to the broker → drift compounds every cycle.
(The orphans/excess are a separate one-off: the 2026-06-12 bad-deploy rebuilt the book smaller while the
broker kept the old positions. The clean reset cleared those; this fix prevents the recurring phantom class.)

## The fix — consume already-reconciled actual fills (no new infra)
`atlas/execution/record_fills.py` **already** does the hard part: daily, AFTER `record_returns` and BEFORE
the new rebalance, it takes every order_id from `runs.jsonl`, queries the broker for the **actual**
`filled_qty / fill_px / status`, and writes `fills.jsonl` (idempotent, fault-tolerant, picks up missed days).

So the structural fix is to make the **virtual book a function of `fills.jsonl`, not of order acceptance**:
1. **Delete** the apply-on-acceptance block in `daily.py` (stop writing the book on submission).
2. Add a **book-from-fills** step (in `record_fills` or a sibling, run in the same pre-rebalance slot):
   for each reconciled fill not yet applied to its strategy's book, `apply_fill(ticker, side, filled_qty,
   fill_px, mult)` — actual qty at actual price. Track applied order_ids (in the book or a sidecar) for
   idempotency. Cancelled/expired/0-filled orders apply nothing.
3. The book then reflects reality every cycle; `reconcile_books` (the guard) should read OK.

## Acceptance
- Unit: an accepted-but-unfilled OPG order writes NOTHING to the book; a partial fill writes `filled_qty`
  at `fill_px`; re-running the step is idempotent (no double-apply).
- Integration: after a shadow cycle + the next open's fills, `reconcile_books` reports `ok` (Σbooks == broker).
- Re-run the guard for several cycles post-fix to confirm no new drift, THEN re-enable the timer.

## Why it matters
The virtual books are the per-strategy accounting that feeds the forward-paper slippage + track-vs-expectation
evidence the **real-capital gate** depends on (crucible #14/#38). Acceptance-based accounting silently
corrupts that evidence. This is the "fix the cause" half; `reconcile_books` is the "guard against recurrence" half.
