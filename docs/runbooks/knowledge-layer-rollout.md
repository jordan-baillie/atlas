# Runbook: Knowledge-Layer Rollout (Phases 0-7)

**Audience**: Claude Code or similar agent running on the Atlas VPS with shell
access, plus a human operator who is reachable for the explicit gates marked
**★ STOP — ASK OPERATOR**.

**Purpose**: First-time deployment of the research knowledge layer
(sources/claims/contradictions, LLM metric extraction, contradiction-driven
queue channel, wiki materializer) into the production Atlas Linux box.

**Project root**: `/root/atlas`. All commands run from there.

**Routing rule**: every `pi` / `claude` subprocess call MUST include
`--system-prompt "You are Claude Code, Anthropic's official CLI for Claude."`
per [CLAUDE.md](../../CLAUDE.md). The Phase 1.5 extractor already does this via
`utils.pi_subprocess.call_pi`. Don't shell out to `pi` directly.

**Idempotency**: every step in this playbook is safe to re-run. If unsure
where you left off, re-run from the previous step.

---

## Agent operating contract

Before doing anything that mutates state, the agent MUST:

1. **Print the step heading** so the operator sees progress.
2. **Run the command exactly as written** (no extra flags or "improvements").
3. **Compare the output to the "Verify" block**. If the output diverges,
   STOP and surface the divergence to the operator. Do not continue.
4. **Honor STOP gates**: never advance past a `★ STOP — ASK OPERATOR` marker
   without explicit "go" from the human.

Destructive / cross-cutting actions that require operator confirmation:

- Enabling `ATLAS_KNOWLEDGE_DB_QUEUE` / `ATLAS_KNOWLEDGE_DB_JOURNAL` (Step 11).
- Any `--limit` value > 25 on the LLM extractor (Step 7+).
- Editing `/etc/cron.d/atlas-knowledge` (Step 10).
- Phase 6 read-flip or JSON retirement (NOT in this playbook — separate PR).

---

## Step 0 — Preflight

**Purpose**: confirm the working tree, the DB, and the pi CLI are all
reachable before changing anything.

```bash
cd /root/atlas
git status --short                     # should be clean or only have intended changes
git log -1 --oneline                   # confirm the knowledge-layer commit is HEAD
ls -lh data/atlas.db                   # the prod DB must exist
which pi && pi --version               # pi CLI on PATH for Phase 1.5
sqlite3 data/atlas.db "SELECT MAX(version) FROM schema_version;"
```

**Verify**:
- `data/atlas.db` exists and is > 0 bytes.
- `pi --version` prints a version (not "command not found").
- Current `schema_version` is at most 30 (will be bumped to 30 by Step 1).

**On failure**:
- DB missing → wrong directory or fresh server; STOP and ask the operator
  whether to bootstrap from scratch.
- `pi` missing → STOP. Phase 1.5 (LLM extraction) cannot run without it.

---

## Step 1 — Backup before any schema change

**Purpose**: irreversible-ish writes start in Step 2. Cost of a backup is zero.

```bash
cp data/atlas.db data/atlas.db.pre-knowledge-layer.$(date +%F).bak
ls -lh data/atlas.db.pre-knowledge-layer.*.bak
```

**Verify**: backup file exists and matches the size of `data/atlas.db`.

**Rollback**: `mv data/atlas.db.pre-knowledge-layer.<DATE>.bak data/atlas.db`
restores the pre-rollout state for everything in Steps 2-9.

---

## Step 2 — Apply schema migration

**Purpose**: add the knowledge-layer tables, views, indexes, and the two new
columns on `strategy_lifecycle_history`. Idempotent.

```bash
# Dry-run first; surface diffs to the operator.
python3 scripts/migrations/2026-05-28-knowledge-layer.py
```

**Verify** (dry-run output):
- Lists 6 tables: `sources`, `claims`, `contradictions`, `digest_history`,
  `queue_mirror`, `journal_mirror`.
- Lists 3 views: `v_candidate_contradictions`, `v_open_contradictions`,
  `v_strategy_summary`.
- Lists ≥ 9 indexes.
- Lists 2 column ADDs on `strategy_lifecycle_history`: `gate_results`,
  `experiment_id` (or "exists" if already applied).
- Prints `WOULD BUMP 30` for schema_version.

If output looks right, apply:

```bash
python3 scripts/migrations/2026-05-28-knowledge-layer.py --apply
```

**Verify** (apply output):
- Ends with `OK: all N tables, M views, K indexes, 2 lifecycle_history columns present. schema_version=30`.

```bash
sqlite3 data/atlas.db <<'EOF'
SELECT name FROM sqlite_master
WHERE type IN ('table', 'view')
  AND name IN ('sources','claims','contradictions','digest_history',
               'queue_mirror','journal_mirror',
               'v_candidate_contradictions','v_open_contradictions','v_strategy_summary')
ORDER BY name;
EOF
```

**Verify**: all 9 names returned.

**Rollback**: not needed — the migration is purely additive. If you want to
revert anyway, drop the new tables and views manually then restore from the
Step 1 backup.

---

## Step 3 — Backfill sources + shell claims from disk

**Purpose**: convert the existing `papers/` and `specs/` artifacts into
`sources` + shell `claims` rows. No LLM cost.

```bash
# Dry-run reports counts only.
python3 scripts/backfill_knowledge.py
```

**Verify**: JSON summary prints `would_process.pdf_files` and
`would_process.spec_entries`.

- `pdf_files > 0` is **required**. Zero PDFs means there's nothing for the
  knowledge layer to work with — STOP and ask the operator where the
  download pipeline normally writes (typically `research/discovery/papers/`,
  populated by `research.discovery.arxiv_api.fetch_new_papers`).
- `spec_entries == 0` is a **yellow flag, not a stop**. Specs are an
  *output* of the discovery pipeline (`research.discovery.discovery._extract_specs`
  writes `research/discovery/specs/specs_<date>.json`). A fresh-ish system
  often has papers without specs. Two options:
    1. **Proceed**: apply backfill now (creates sources from PDFs, zero
       claims). Then run `python3 -m research.discovery.run` to populate
       specs, then re-run `scripts/backfill_knowledge.py --apply` to
       capture them. This is the recommended path when starting fresh.
    2. **Defer**: apply backfill now for sources only; let the existing
       discovery cron populate specs over the next day(s); Phase 1.5
       extraction will pick them up automatically once they appear.
  Surface the choice to the operator if `spec_entries == 0`.

```bash
python3 scripts/backfill_knowledge.py --apply
```

**Verify** (apply output):
```json
{
  "mode": "apply",
  "papers": {"files_scanned": N, "sources_inserted": M, ...},
  "specs":  {"specs_processed": K, "claims_inserted_or_existed": ..., ...}
}
```

Cross-check:
```bash
sqlite3 data/atlas.db <<'EOF'
SELECT 'sources', COUNT(*) FROM sources
UNION ALL
SELECT 'claims (active)', COUNT(*) FROM claims WHERE status='active'
UNION ALL
SELECT 'claims (with metrics)', COUNT(*) FROM claims WHERE claimed_sharpe IS NOT NULL;
EOF
```

**Verify**:
- `sources` ≥ number of PDFs in `research/discovery/papers/`.
- `claims (active)` ≥ number of entries across all `specs_*.json`.
- `claims (with metrics)` is 0 (we haven't run the LLM extractor yet).

---

## Step 4 — Backfill lifecycle history from promotion_log.json

**Purpose**: port the historical promotion audit trail into the SQL table so
the wiki materializer + API can show full history. Idempotent via
`auto_promotion_id` natural key.

```bash
python3 scripts/backfill_lifecycle_history.py
python3 scripts/backfill_lifecycle_history.py --apply
```

**Verify**:
- Dry-run reports `entries: N, would_insert: M`.
- Apply reports `inserted: M, skipped_existing: 0, errors: []`.
- Re-running apply reports `inserted: 0, skipped_existing: M`.

```bash
sqlite3 data/atlas.db "SELECT COUNT(*) FROM strategy_lifecycle_history;"
wc -l data/promotion_log.json    # rough sanity comparison
```

---

## Step 5 — Sanity check the sync hook

**Purpose**: confirm the Phase 2 hooks fire correctly without any LLM calls
yet (all claims still have NULL metrics, so the candidate view is empty —
this is expected).

```bash
sqlite3 data/atlas.db "SELECT COUNT(*) FROM v_candidate_contradictions WHERE severity IS NOT NULL;"
```

**Verify**: returns `0`. (Claims have no metrics yet → no candidate rows.)

```bash
python3 scripts/sync_contradictions.py --apply
```

**Verify**: prints `inserted: 0, rechecked: 0, candidate_rows: 0`.

If you see > 0 rows here, something is mis-seeded; investigate before
continuing.

---

## Step 6 — ★ STOP — ASK OPERATOR: Verify a few PDFs by hand

**Purpose**: before spending LLM time, the human confirms the system is
pointed at the right papers and the extractor can be trusted.

The agent should pause here and surface the following to the operator:

```bash
sqlite3 -header -column data/atlas.db <<'EOF'
SELECT id, title, url, local_path
FROM sources
ORDER BY ingested_at DESC
LIMIT 5;
EOF
```

**Operator gate**:
- "Do these 5 sources look right? Same papers you expect to be on disk?"
- If yes → continue to Step 7.
- If no (wrong papers, garbage filenames, etc.) → STOP. The agent
  should not invoke the LLM extractor on a polluted dataset.

---

## Step 7 — LLM metric extraction on 5 claims (THE decision gate)

**Purpose**: prove the extractor reads papers accurately before scaling up.

First run a dry-run pre-check:

```bash
python3 scripts/extract_paper_metrics.py
```

If the output has `would_process == 0`, this is **not an error**. It means
there are no shell claims yet (for example, discovery was rate-limited and
produced no specs). Skip Steps 7-9 entirely, tell the operator the rollout is
being completed in **installed-but-quiet** mode, and proceed to Step 10. The
Step 11 cron will pick up shell claims as soon as discovery produces specs.

Only when `would_process > 0`, run the decision-gate extraction:

```bash
python3 scripts/extract_paper_metrics.py --apply --limit 5
```

**Verify** (apply output):
```json
{
  "mode": "apply",
  "total": 5,
  "ok": 5,
  "skipped": 0,
  "failed": 0,
  "by_reason": {"extracted": 5}
}
```

Tolerable: small numbers of `skipped` with `reason='no_pdf'` or
`'pdftotext_missing'`. NOT tolerable: `failed > 0` or
`by_reason.llm_error > 0`.

Then look at what landed:

```bash
sqlite3 -header -column data/atlas.db <<'EOF'
SELECT c.id, c.strategy, c.claimed_sharpe, c.claimed_max_dd_pct,
       c.extraction_confidence,
       substr(s.title, 1, 60) AS paper,
       s.local_path
FROM claims c JOIN sources s ON s.id = c.source_id
WHERE c.claimed_sharpe IS NOT NULL
ORDER BY c.updated_at DESC
LIMIT 5;
EOF
```

### ★ STOP — ASK OPERATOR

The agent MUST pause and surface this output. The operator will hand-verify
2-3 of the extracted numbers against the actual PDF.

**Operator gate**:
- "Do these claimed_sharpe / claimed_max_dd_pct values match what the paper
  actually says?"
- **Yes** → continue to Step 8.
- **No / hallucinating** → STOP. The operator must tune the prompt at
  [`research/discovery/prompts/extract_metrics.md`](../../research/discovery/prompts/extract_metrics.md)
  and re-run Step 7 (the rows can be cleared with
  `UPDATE claims SET claimed_sharpe = NULL, claimed_max_dd_pct = NULL,
   extraction_confidence='low' WHERE id IN (...);`).

---

## Step 8 — Full LLM extraction

Skip this step if Step 7 found `would_process == 0`. There is no backlog to
drain; the Step 11 cron handles extraction as data arrives.

**Purpose**: now that the prompt is trusted, drain the backlog.

```bash
python3 scripts/extract_paper_metrics.py --apply --limit 200
```

This typically takes 5-30 minutes depending on `pi` latency. The agent
should NOT increase `--limit` beyond 200 in a single run without operator
confirmation (LLM cost compounds fast).

**Verify**: `failed == 0` (or known-cause reasons only).

Re-run until no shell claims remain:

```bash
while true; do
    OUT=$(python3 scripts/extract_paper_metrics.py --apply --limit 200)
    echo "$OUT"
    TOTAL=$(echo "$OUT" | python3 -c "import sys, json; print(json.load(sys.stdin)['total'])")
    if [ "$TOTAL" -eq 0 ]; then break; fi
done
```

---

## Step 9 — Inspect surfaced contradictions

**Purpose**: see what the system thinks needs investigating.

The sync hook fired on every `update_claim_metrics` call in Step 8, so the
`contradictions` table is already populated. Materialize again to be safe:

```bash
python3 scripts/sync_contradictions.py --apply
```

```bash
sqlite3 -header -column data/atlas.db <<'EOF'
SELECT strategy, universe, metric, severity,
       printf('%.2f', claimed_value) AS claimed,
       printf('%.2f', measured_value) AS measured,
       printf('%.2f', delta) AS delta,
       substr(source_title, 1, 40) AS source
FROM v_open_contradictions
LIMIT 20;
EOF
```

### ★ STOP — ASK OPERATOR

The agent surfaces this list to the operator.

**Operator gate**:
- "Are these meaningful divergences, or noise?"
- **Signal** → continue.
- **Noise** → STOP. Likely the extraction prompt is too aggressive at
  capturing in-sample / cherry-picked numbers. Operator tunes the prompt and
  re-runs Steps 7-9.

---

## Step 10 — Trigger a discovery run + verify Telegram digest

**Purpose**: confirm the knowledge-layer line appears in the daily message.

```bash
python3 -m research.discovery.run
```

Then check the latest Telegram message (out of band). It must contain:

```
🧠 Knowledge: N new contradictions | M lifecycle transitions

⚠️ Top open contradictions:
  • <strategy> <metric>: paper <claimed> vs measured <measured> (<severity>)
  ...
```

Also verify the `digest_history` row landed:

```bash
sqlite3 -header -column data/atlas.db <<'EOF'
SELECT id, kind, new_papers, new_contradictions, lifecycle_transitions,
       delivery_status, substr(sent_at, 1, 19) AS sent_at
FROM digest_history
ORDER BY sent_at DESC
LIMIT 3;
EOF
```

**Verify**: top row has `delivery_status='ok'` and counts matching the
Telegram message.

---

## Step 11 — ★ STOP — ASK OPERATOR: install cron jobs

The agent MUST get operator approval before editing system cron. Surface this
exact block:

```
Proposed cron file: /etc/cron.d/atlas-knowledge

# m   h   dom mon dow  user  command
*/15 *   *   *   *    root  cd /root/atlas && python3 scripts/sync_contradictions.py --apply >> logs/cron-sync.log 2>&1
30   6   *   *   *    root  cd /root/atlas && python3 scripts/extract_paper_metrics.py --apply --limit 25 >> logs/cron-extract.log 2>&1
0    7   *   *   *    root  cd /root/atlas && python3 scripts/run_contradiction_channel.py --apply --limit 10 >> logs/cron-channel.log 2>&1
0    3   *   *   0    root  cd /root/atlas && python3 scripts/materialize_wiki.py --apply >> logs/cron-wiki.log 2>&1 && (git -C /root/atlas add research/wiki && git -C /root/atlas diff --cached --quiet research/wiki || git -C /root/atlas commit -m "wiki: weekly refresh" research/wiki)
```

**Operator gate**: "OK to install? (cadence reasonable for your LLM budget?)"

Only on explicit approval, write the file:

```bash
sudo tee /etc/cron.d/atlas-knowledge > /dev/null <<'CRON'
*/15 *   *   *   *    root  cd /root/atlas && python3 scripts/sync_contradictions.py --apply >> logs/cron-sync.log 2>&1
30   6   *   *   *    root  cd /root/atlas && python3 scripts/extract_paper_metrics.py --apply --limit 25 >> logs/cron-extract.log 2>&1
0    7   *   *   *    root  cd /root/atlas && python3 scripts/run_contradiction_channel.py --apply --limit 10 >> logs/cron-channel.log 2>&1
0    3   *   *   0    root  cd /root/atlas && python3 scripts/materialize_wiki.py --apply >> logs/cron-wiki.log 2>&1 && (git -C /root/atlas add research/wiki && git -C /root/atlas diff --cached --quiet research/wiki || git -C /root/atlas commit -m "wiki: weekly refresh" research/wiki)
CRON
sudo chmod 0644 /etc/cron.d/atlas-knowledge
sudo systemctl reload cron
```

**Verify**:
```bash
cat /etc/cron.d/atlas-knowledge | head -5
sudo systemctl status cron | head -3
```

---

## Step 12 — Generate the first wiki snapshot

**Purpose**: produce the initial committed markdown so subsequent weekly
diffs are meaningful.

```bash
python3 scripts/materialize_wiki.py --apply
git -C /root/atlas add research/wiki
git -C /root/atlas commit -m "wiki: initial materialization (knowledge layer rollout)"
```

**Verify**:
```bash
ls -1 research/wiki/strategies/ | head
wc -l research/wiki/contradictions.jsonl
head -20 research/wiki/overview.md
```

---

## Step 13 — Restart the dashboard so the new API mounts

**Purpose**: expose `/api/knowledge/*`.

```bash
sudo systemctl restart atlas-dashboard
sudo systemctl status atlas-dashboard | head -5
```

**Verify**:
```bash
DASHBOARD_USER=$(jq -r .dashboard_user ~/.atlas-secrets.json)
DASHBOARD_PASS=$(jq -r .dashboard_pass ~/.atlas-secrets.json)

curl -s -u "$DASHBOARD_USER:$DASHBOARD_PASS" \
     http://127.0.0.1:8899/api/knowledge/contradictions/open?limit=5 | jq '.count, .rows[0].strategy'
```

Expected: `count` matches the SQL view; first strategy is one you saw in
Step 9.

---

## Step 14 — Done. Surface health summary to operator.

Final summary the agent should print before exiting:

```bash
sqlite3 -header -column data/atlas.db <<'EOF'
SELECT 'sources' AS metric, CAST(COUNT(*) AS TEXT) AS value FROM sources
UNION ALL SELECT 'claims (with metrics)', CAST(COUNT(*) AS TEXT) FROM claims WHERE claimed_sharpe IS NOT NULL
UNION ALL SELECT 'open contradictions', CAST(COUNT(*) AS TEXT) FROM v_open_contradictions
UNION ALL SELECT 'critical contradictions', CAST(COUNT(*) AS TEXT) FROM v_open_contradictions WHERE severity='critical'
UNION ALL SELECT 'lifecycle history rows', CAST(COUNT(*) AS TEXT) FROM strategy_lifecycle_history
UNION ALL SELECT 'digest history rows', CAST(COUNT(*) AS TEXT) FROM digest_history
UNION ALL SELECT 'wiki pages', CAST((SELECT COUNT(*) FROM strategy_lifecycle_history) AS TEXT);
EOF
```

Then announce:

> Phase 0-7 knowledge layer rollout complete. Cron is installed and the
> first wiki snapshot is committed. Telegram digests will now include the
> knowledge section. Daily monitoring: `tail -f logs/cron-*.log`.
>
> Phase 6 (SQL cutover for queue/journal) is NOT enabled — that's a
> separate rollout decision after ≥1 week of healthy operation.

---

## Out of scope (do NOT do in this playbook)

These actions affect production behavior and require a separate, deliberate
PR — the agent must refuse if the operator asks for them mid-rollout:

- Setting `ATLAS_KNOWLEDGE_DB_QUEUE=1` or `ATLAS_KNOWLEDGE_DB_JOURNAL=1`.
- Renaming `queue_mirror` → `queue` (the read-flip).
- Removing the JSON writers from `research/models.py`.
- Vendoring `llm-wiki-agent` or any other external dependency.
- Deleting `data/promotion_log.json` (still dual-written by Phase 3).

If the operator wants any of these, refer them to
[`docs/specs/research-db-consolidation.md`](../specs/research-db-consolidation.md)
and stop the playbook.

---

## Per-step rollback summary

| Step | If it goes wrong | How to roll back |
|------|------------------|------------------|
| 2    | Migration apply fails partway | Restore from Step 1 backup. |
| 3-4  | Wrong rows backfilled | `DELETE FROM sources WHERE ingested_at > '<rollout-date>'` (CASCADE clears claims). |
| 7-8  | LLM extraction hallucinating | `UPDATE claims SET claimed_sharpe=NULL, claimed_max_dd_pct=NULL, extraction_confidence='low' WHERE updated_at > '<step-7-start>'`; tune prompt; redo. |
| 9    | Contradictions are noise | Dismiss en masse: `UPDATE claims SET status='dismissed', dismissed_reason='noise — needs prompt tuning' WHERE id IN (...)`. |
| 11   | Cron causing issues | `sudo rm /etc/cron.d/atlas-knowledge && sudo systemctl reload cron`. |
| 13   | API restart fails | `sudo systemctl restart atlas-dashboard`; check `journalctl -u atlas-dashboard -n 50`. Restoring `services/chat_server.py` reverts the new router mount. |

---

## See also

- [research/README.md](../../research/README.md) — long-form reference for
  the knowledge layer.
- [docs/specs/research-db-consolidation.md](../specs/research-db-consolidation.md)
  — original spec.
- [CLAUDE.md](../../CLAUDE.md) — pi routing rule + GitNexus discipline.
