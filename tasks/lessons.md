# Atlas Operational Lessons

## 2026-06-11 — A "cleanup" script that resolves its target dynamically is a loaded gun after a repoint
The sp500-flatten job ("flatten the retired paper account") resolved its broker from
config + ALPACA_PAPER_* secrets. Two days earlier those secrets were repointed to the NEW
shared paper account that the live forward experiment trades on. The script's NAME still said
"retired account"; its RESOLUTION said "the forward book". Running it queued market-sells of
all 50 forward positions (caught after-hours, cancelled, zero fills, book verified intact).
RULES:
1. Destructive ops scripts must PIN their target (hardcoded account id + assert account_number
   matches) — never resolve dynamically through config/secrets that something else may repoint.
2. Before running ANY destructive script, verify what it will actually touch (account id,
   positions list) — not what its name/docstring claims it touches.
3. When retiring an account/resource, delete or disarm its cleanup automation IN THE SAME
   CHANGE (543356c3 said "retired atlas-sp500-flatten.timer" but the unit survived).
4. A script that crashes on every scheduled run is not "safely broken" — fixing the crash
   without re-validating the intent re-arms it. The crash was the only thing keeping it safe.

## 2026-06-11 — Script-mode sys.path failures are systemic, not one-off
`python3 path/to/script.py` puts the SCRIPT's dir on sys.path, not the repo. Two production
casualties in one day: crucible live/deploy.py (forward-paper weight refresh silently FAILED
on night one) and ops/flatten_sp500.py (every timer run). Fix pattern: self-insert repo root
at the top of any script meant to run by path, or always invoke via `python3 -m`.

## 2026-06-13 — Refactor deletions leave three kinds of debris
The June 9 "old Atlas is no more" refactor deleted scripts but left: (a) cron entries
calling the deleted paths (failing silently every day — `|| true`-style logging hid it),
(b) 4.1GB of artifacts the dead producer had accumulated (nothing watched its output dir),
(c) a restic retention policy silently split into per-path-set groups (75 snapshots vs
keep-14). RULE: any refactor that deletes a producer/script must sweep (1) consumers,
(2) cron/systemd callers, (3) the producer's accumulated output, (4) retention/monitoring
that assumed the old shape. Verification = run the hygiene jobs and READ their output,
not just check they exit 0.

## 2026-06-14 — Weekly-maintenance deleted canonical caches + crashed on empty glob (cascade #4)
crucible-sentinel alerted: sep_long_v2.parquet missing. Root cause was atlas-weekly-maintenance
(Sun 06:00), TWO bugs: (1) it deleted ALL root-level data/cache/*.parquet on a stale "those are
dupes, real caches live in subdirs" assumption — but crucible's sdk/adapters.py (added Jun 10-12)
deliberately writes sep_long_v2/sf1_long/futcurve_* to the cache ROOT. Result: 1.36GB of base
cache nuked every Sunday, ~3min cold rebuild every Monday. (2) it then CRASHED (exit 2) on
`ls -1t .../atlas.db.bak*` under `set -euo pipefail` — the morning storage sweep had removed the
last atlas.db.bak files, so the glob was empty, ls exits 2, pipefail+set -e kills the script.
FIX: removed the root-parquet deletion (premise dead; adapters self-invalidate via _stale());
made backup-pruning empty-glob-safe with find -printf|sort. LESSON (reinforced 4x today): when a
NEW component changes a shared-resource convention (where caches live), audit EVERY janitor that
operates on that resource. And: `ls glob*` is never empty-safe under pipefail — use find. My own
cleanup this morning TRIGGERED a latent crash in another script — cleanups have blast radius into
other scripts' assumptions, not just their own files.

## 2026-06-16 — Virtual books recorded fills on ACCEPTANCE, not execution (silent drift)
The shadow virtual books (`virtual_book.py`) updated on `OrderResult.success` (= order ACCEPTED) using the
requested qty at ref price. But the shadow loop runs in the Alpaca OPG window — actual fills land ~14h later
at the open, and OPG no-fills (HTB shorts, halts) never happen. Result: 119 phantom + 45 qty-mismatched
positions accumulated silently (books claimed 176 vs broker's 67), corrupting the per-strategy accounting that
feeds the forward-paper evidence the real-capital gate depends on. Nothing caught it — `reconcile_shadow`
only covers the legacy SP500 book. LESSONS: (1) a virtual book must be a function of RECONCILED ACTUAL FILLS,
never order acceptance — `record_fills.py` already resolves order_id -> actual filled_qty/fill_px, so consume
that. (2) Every shared-account invariant (Σ sub-books == broker) needs an explicit guard that ALERTS, or it
drifts silently — built `reconcile_books.py` + wired into forward-paper.sh. (3) When resetting, the registry
(`registry.deployed()`), not the (corrupted) book file, is the authoritative capital_base — val_mom's book had
drifted to $14,500 vs the registry's $5,000.
