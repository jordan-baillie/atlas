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
