# Virtual-book fill reconciliation — root-cause fix for book↔broker drift

**Status:** ✅ IMPLEMENTED 2026-06-16 (atlas `b495a6e3`). `daily.py` no longer touches the book;
`record_fills.reconcile_book` applies the reconciled ACTUAL fill (book-from-fills), idempotent via the
fills.jsonl done-set. 4 new tests + 110 execution tests green. Rebalancer (`atlas-live-shadow.timer`)
RE-ENABLED after the clean reset (books zeroed + registry-correct, broker flattening at the open, guard
`reconcile_books.py` wired into forward-paper.sh self-checking each cycle). Original scope below.

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

## Acceptance — MET
- Unit (test_book_from_fills.py): accepted-but-unfilled OPG order writes NOTHING; partial fill writes
  `filled_qty` at `fill_px`; idempotent (no double-apply); short books negative + adds cash. ✅
- Integration: WATCH `reconcile_books` over the first post-reset cycles (Wed 10:30 AEST onward) to confirm
  `ok` (Σbooks == broker). Futures multiplier in book-from-fills is a follow-on (equity-only today).

## Why it matters
The virtual books are the per-strategy accounting that feeds the forward-paper slippage + track-vs-expectation
evidence the **real-capital gate** depends on (crucible #14/#38). Acceptance-based accounting silently
corrupts that evidence. This is the "fix the cause" half; `reconcile_books` is the "guard against recurrence" half.
