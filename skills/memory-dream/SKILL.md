---
name: memory-dream
description: "Batch memory curation for the agent-architecture runtime — dedup, merge, supersede, archive, and flag typed_memory rows based on recent runtime activity."
version: 0.1.0
author: Agent Architecture contributors
license: MIT
metadata:
  agent_architecture:
    tags: [memory, curation, batch, semantic, dream]
    related: [memory, retrieval, trace]
    related_skills: [hermes-dream, hermes-agent-skill-authoring, agent-learning-systems, cron-job-workflows]
---

# Memory Dream (agent-architecture edition)

Memory Dream is a batch memory-curation skill for the
agent-architecture runtime. It is a Postgres-native port of
[hermes-dream](https://github.com/lamenting-hawthorn/agent-architecture-public)
(hermes-agent's personal memory curator), adapted to operate on
`memory.typed_memory` rows instead of §-delimited markdown files.

It is the missing maintenance layer for the runtime's three-layer
memory model:

```text
event_store.events        ──►  memory.typed_memory  ──►  LangGraph retrieval
                                  ▲
                                  │   dream runs here, off the hot path
                                  │
                            every N days, batch
```

## What it does

1. Reads all live rows from `memory.typed_memory` for one actor.
2. Mines recent runtime activity from `event_store.events`,
   `memory.retrieval_logs`, and `memory.trace_events` (last 90 days
   by default).
3. Asks the configured LLM to produce a per-row proposal:
   `keep`, `merge`, `supersede`, `archive`, or `flag_for_review`.
4. Stages the proposals in `memory.dream_proposals` (one row per
   existing typed_memory row, tied to a `memory.dream_runs` row).
5. Writes a human-readable diff to `dream.py diff <run_id>`.
6. On `dream.py adopt <run_id>`, applies all pending proposals
   in a single Postgres transaction:
   - `keep`         — no-op, just records the audit.
   - `merge`        — `UPDATE content = proposed_replacement`,
                     `confidence -= 0.1` (marked as a rewrite).
   - `supersede`    — sets `superseded_by` on the loser, boosts the
                     winner's confidence, inserts a `supersedes`
                     edge in `memory.memory_edges`.
   - `archive`      — sets `expires_at = now() + 30d` so the TTL
                     cleanup eventually removes the row.
   - `flag_for_review` — adds a `metadata.dream_flag` jsonb entry
                     the next `notify_review.py` will surface in
                     the Telegram digest.

Every write also emits a `memory_written` / `memory_updated` row
in `memory.audit_log` with the dream run id, so the full change
history is auditable.

## Why

The runtime writes new memory incrementally (event worker,
SkillLoop connector, vault bridge) but never:

1. **Merges** semantically similar rows that accumulated over time.
2. **Resolves** contradictions between old and new versions.
3. **Archives** rows whose topics fell out of the user's interests.
4. **Flags** rows the user has not retrieved in months.

The audit recorded 147 rows in the receiving repo, **all**
`memory_type='semantic'` / `category='fact'`, zero episodic and
zero procedural — the runtime never exercises the schema's
maintenance hooks. dream fills that gap. It runs off the hot path
(offline, batch) and writes only after human review.

## Composes with

Dream is a sibling of the existing maintenance systems. The split
is by time-scale:

| System | Cadence | Scope | What it does |
|--------|---------|-------|--------------|
| `event_worker.py` | Continuous | events → typed_memory | Per-event salience + write |
| `scripts/connect_skillloop.py` | Hourly | approved proposals → typed_memory | SkillLoop writeback |
| `scripts/bridge_vault_and_sessions.py` | Hourly | vault + hermes DB → typed_memory | External import |
| `scripts/notify_review.py` | Hourly | pending proposals → Telegram | Human notification |
| **`memory-dream` (this skill)** | **Biweekly** | **typed_memory** | **Batch dedup / merge / archive** |

## When to use

- Every 2 weeks if you're an active user (sessions accumulate fast).
- After a long project (memory may have drifted from current interests).
- Before a model switch (clean the input the new model will see).
- When `memory.typed_memory` exceeds ~500 rows.
- To surface rows the runtime keeps retrieving but that are
  no longer useful (candidates for archive).

Do **not** use:

- On every session (overhead vs. value is poor — ~$0.10 + 30s per run).
- For a brand-new install (no past activity to mine).
- To replace the per-event write path (they serve different time-scales).

## Usage

```bash
# From the repo root
export DATABASE_URL=postgresql:///agent_memory
export ACTOR_ID=u_owner
export LLM_API_KEY=...

# One-off run with defaults
python skills/memory-dream/scripts/dream.py run

# Conservative mode (no LLM, just lexical dedup)
python skills/memory-dream/scripts/dream.py run --no-llm

# Dry run — show prompt without calling the LLM
python skills/memory-dream/scripts/dream.py run --dry-run

# Steer the LLM with explicit guidance
python skills/memory-dream/scripts/dream.py run --focus "Conservative: only merge exact duplicates. Never supersede."

# Inspect the result
python skills/memory-dream/scripts/dream.py status
python skills/memory-dream/scripts/dream.py runs
python skills/memory-dream/scripts/dream.py proposals <run_id>
python skills/memory-dream/scripts/dream.py diff <run_id>

# Apply the proposals (writes to typed_memory)
python skills/memory-dream/scripts/dream.py adopt <run_id> -y

# Throw them away
python skills/memory-dream/scripts/dream.py discard <run_id> -y
```

## Files

```text
skills/memory-dream/
├── SKILL.md                      # this file
├── scripts/
│   ├── dream.py                  # CLI orchestrator
│   ├── parser.py                 # typed_memory rows → MemoryEntry
│   ├── collector.py              # event_store / retrieval_logs / trace_events mining
│   ├── deduplicator.py           # exact / substring / prefix / semantic (pgvector) dedup
│   ├── synthesizer.py            # LLM curation pass
│   ├── controller.py             # staging (memory.dream_proposals + memory.dream_runs) + adopt / discard
│   ├── diff.py                   # human-readable diff report
│   ├── _loadenv.py               # secret-safe .env loader
│   └── test_dream.py             # smoke test (no DB required)
└── references/
    └── schema-mapping.md         # how dream maps to the agent-architecture schema
```

## How it works (detailed)

1. **Parse** — `parser.parse_typed_memory(user_id)` reads
   `memory.typed_memory WHERE superseded_by IS NULL` for one actor,
   ordered by `(memory_type, confidence DESC, created_at DESC)`.
   Skips already-superseded rows so the curator doesn't propose
   to re-merge retired facts.

2. **Collect** — `collector.collect_activity(user_id, max_age_days=90)`
   pulls:
   - `event_store.events` filtered to `event_type IN ('message_received', 'model_answer', 'tool_result')`,
     grouped by `session_id`, with `_extract_text()` pulling the
     `text` / `output` / `content` field out of jsonb payloads.
   - `memory.retrieval_logs` for queries the runtime actually ran
     (a strong signal of what the user is *actually* asking about).
   - `memory.trace_events` for `status IN ('error', 'fallback')`
     (which rows the runtime can't successfully use).

   The collector returns at most 30 sessions within a 50k-char
   budget, so the LLM prompt stays bounded.

3. **Dedup (pre-LLM)** — `deduplicator.find_all_dupes(store, include_semantic=False)`
   catches the easy cases (exact, substring, prefix) before the
   LLM sees the data. The semantic pass is skipped by default
   because the LLM does the harder paraphrasing work; flip
   `include_semantic=True` if you want pure-pgvector dedup.

4. **Synthesize (LLM)** — `synthesizer.synthesize(store, excerpts, ...)`
   sends the current store (grouped by memory_type), the recent
   activity, and an optional `--focus` string to the LLM. The
   prompt asks for JSON in this shape:

   ```json
   {
     "proposals": [
       {
         "row_id": "<uuid>",
         "action": "keep|merge|supersede|archive|flag_for_review",
         "proposed_replacement": "<text, only for merge>",
         "proposed_superseded_by_id": "<uuid, only for supersede>",
         "confidence": 0.0-1.0,
         "rationale": "<one sentence>"
       }
     ],
     "summary": "1-2 sentence description"
   }
   ```

5. **Stage** — `controller.stage_proposals(run_id, result, store)`
   validates each proposal (must reference a real `row_id` in the
   store; `supersede` must reference a second real row) and inserts
   a row into `memory.dream_proposals`. Bad proposals are silently
   dropped — better to skip a hallucinated `row_id` than to surface
   it to the user.

6. **Review** — `dream.py diff <run_id>` renders a markdown report
   grouped by action, with row previews, the proposed replacement
   text (for merges), the target row (for supersedes), the model's
   confidence, and the rationale.

7. **Adopt** — `controller.adopt_run(run_id)` processes all pending
   proposals in a single transaction. The `_apply_*` helpers are
   per-action; each writes to `memory.audit_log` with the dream run
   id, the loser / winner ids (for supersede), and a text preview
   of what changed. A failure in any helper rolls back the entire
   batch.

8. **Discard** — `controller.discard_run(run_id)` marks all pending
   proposals as `rejected` and sets the run status to `discarded`.
   No write to `typed_memory`.

## Cost (bounded)

| typed_memory size | Input tokens | Cost (DeepSeek v4 Flash) | Wall time |
|-------------------|--------------|--------------------------|-----------|
| 100 rows | ~8k | ~$0.02 | ~15s |
| 500 rows | ~30k | ~$0.08 | ~30s |
| 2000 rows | ~110k | ~$0.30 | ~90s |
| 5000 rows | ~250k | ~$0.70 | ~3min |

A biweekly cadence on a 500-row store ≈ **$2/year, ~30 min/year of
human review time**. The default `LLM_MODEL=deepseek-v4-flash` is
chosen for cost; switch to a more careful model via `--model` for
higher-stakes runs.

## Scheduling

The repo's existing launchd plist pattern (see
`docs/launchd/com.agent_architecture.controller.plist` etc.) is
the recommended host. A sample plist lives at
`docs/launchd/com.agent_architecture.dream.plist` and runs the
dream on the 1st and 15th of each month at 11:00.

```bash
# Bootstrap the plist (after install.sh sets up the repo)
cp docs/launchd/com.agent_architecture.dream.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agent_architecture.dream.plist
launchctl enable gui/$(id -u)/com.agent_architecture.dream
```

## Safety

- **Original rows never modified** until `adopt` is run.
- **Atomic batch apply** — failure in any proposal rolls back the
  entire transaction.
- **Audit chain** — every `merge` / `supersede` / `archive` /
  `flag_for_review` writes a `memory.audit_log` row with the
  dream run id, the action, the row id, and a text preview.
- **Permission scope is enforced** — `parser.parse_typed_memory()`
  filters by `user_id` and the `deduplicator` only compares rows
  in the same `(memory_type, category)` bucket for the same
  `user_id`. The dream never proposes a cross-actor merge.
- **LLM proposals are validated** before staging — invalid
  `row_id`s, missing `proposed_replacement` for `merge`, missing
  `proposed_superseded_by_id` for `supersede`, all get dropped.

## Pitfalls

- **Don't run too often.** Overhead is real. Biweekly is a good
  default; weekly is fine if memory grows fast; daily is wasteful.
- **The first run on a small store produces few proposals.** With
  <100 rows and a sparse retrieval history, the LLM mostly emits
  `keep`. That's correct — not noise, just no signal.
- **Always review the diff before adopting.** A bad `--focus`
  prompt can cause the LLM to drop non-negotiable rules. The
  diff is the last line of defense.
- **`superseded_by` is set, not deleted.** The loser row stays in
  `memory.typed_memory` with `superseded_by` pointing at the
  winner. `parser.parse_typed_memory()` skips these by default.
  The TTL cleanup will eventually remove them (if you have one);
  otherwise they're cheap storage.
- **`memory_edges` accumulates `supersedes` edges.** This is
  intended — the graph layer (Phase 6 of the runtime) uses these
  edges to expand retrieval. Don't delete them; old `supersedes`
  edges are useful for "what was the previous version of this fact?"
  questions.
- **The LLM paraphrase-merge case is hard.** A "merge" of two
  semantically-similar but lexically-different rows requires the
  LLM to recognize they're the same fact. The pgvector semantic
  dedup pass at threshold 0.92 catches most of these, but a small
  fraction will need the LLM. Trust the diff.

## Differences from hermes-dream

| | hermes-dream (Hermes) | memory-dream (agent-architecture) |
|---|---|---|
| Input | `~/.hermes/memories/MEMORY.md` + `USER.md` | `memory.typed_memory` rows |
| "Past activity" source | `~/.hermes/sessions/*.jsonl` | `event_store.events` + `memory.retrieval_logs` + `memory.trace_events` |
| Memory types | Two (memory / user) | Three (episodic / semantic / procedural) |
| Staging | Files in `~/.hermes/memories/.staging/` | Two Postgres tables: `memory.dream_runs` + `memory.dream_proposals` |
| Dedup mechanism | Hash + substring + prefix | Hash + substring + prefix + **pgvector cosine** |
| Adopt action | Replace memory files | Mutate `memory.typed_memory` (UPDATE / FK) in a single txn |
| Rollback | Backup of original files | N/A (no original files; transactions + audit log) |
| LLM choice | NVIDIA integrate → DeepSeek v4 Pro | Runtime's `LLMClient` (default DeepSeek v4 Flash) |
| Output review | Console / `cat diff.md` | Same + `dream.py diff <run_id>` renders to stdout |
| Notifications | None | `notify_review.py` can include "pending dream proposals" alongside SkillLoop proposals |
| Privacy | Local files | Postgres — same threat model as the rest of the runtime |

## License

MIT.
