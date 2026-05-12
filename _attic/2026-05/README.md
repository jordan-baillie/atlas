# Atlas Attic — 2026-05 Cleanup Wave

Files moved here are not deleted — they're quarantined for a dwell period.

## Policy
- Move via `git mv` to preserve history
- 14-day dwell minimum before considering `rm`
- If anything in `_attic/2026-05/` is referenced by an active code path during the dwell, restore it with `git mv` back to the original location.

## Recovery
- Per-file: `git mv _attic/2026-05/<dir>/<file> <original-path>/`
- Whole tier: `git checkout pre-cleanup-2026-05-12 -- <path>`

## Provenance
Each commit moving files here MUST cite the tier from `docs/cleanup-plan-2026-05.md` (or paste the relevant portion of the directive in the commit message).
