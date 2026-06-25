# Schema mapping: hermes-dream → memory-dream

This document maps the data structures in hermes-dream (Hermes's
flat-file memory curator) to the data structures in memory-dream
(the agent-architecture equivalent). If you are familiar with
hermes-dream, this is the diff.

## High-level

| hermes-dream | memory-dream |
|---|---|
| `~/.hermes/memories/MEMORY.md` | `memory.typed_memory` rows (filtered to `memory_type='semantic'` for the agent's notes) |
| `~/.hermes/memories/USER.md` | `memory.typed_memory` rows filtered to `memory_type='episodic'` (user-specific context) |
| `~/.hermes/sessions/*.jsonl` | `event_store.events` + `memory.retrieval_logs` + `memory.trace_events` |
| `~/.hermes/memories/.staging/MEMORY.md` | one row per (run, row_id) in `memory.dream_proposals` |
| `~/.hermes/memories/.staging/diff.md` | `python dream.py diff <run_id>` renders to stdout / `--out file.md` |
| `~/.hermes/memories/.staging/meta.json` | one row per run in `memory.dream_runs` |
| `~/.hermes/memories/.backups/<ts>/` | N/A (no files to back up; transactions + audit log are the safety net) |

## MemoryEntry

```python
# hermes-dream
@dataclass
class MemoryEntry:
    text: str
    source: str       # "memory" | "user"
    index: int        # position in the file
    hash: str         # sha256[:12] for exact-match dedup
    char_count: int
```

```python
# memory-dream
@dataclass
class MemoryEntry:
    row_id: str                  # memory.typed_memory.id (uuid)
    text: str                    # coalesce(summary, content)
    memory_type: str             # episodic | semantic | procedural
    category: str                # fact | preference | procedure | …
    confidence: float            # 0.0 – 1.0
    source: str                  # user_utterance | hermes_import | …
    visibility: str              # owner_only | team | org | public
    user_id: str                 # actor scope
    created_at: str              # ISO 8601
    index: int                   # position in the result set
    hash: str                    # sha256[:12] of normalized text
    superseded_by: str | None    # memory.typed_memory.superseded_by
```

The `memory_type` and `category` fields are new. They let the
deduplicator skip cross-bucket comparisons (a "fact" and a
"procedure" should never be merged just because they have
similar text) and let the curator emit per-type action plans.

## Proposal actions

| hermes-dream | memory-dream |
|---|---|
| `keep` (in `memory[]` / `user[]` arrays) | `keep` (one row in `memory.dream_proposals` with `action='keep'`) |
| `merge` (implicit: text appears once in the new array) | `merge` (`proposed_replacement` text + `UPDATE content = ...`) |
| `replace` (old entry removed, new text added) | `supersede` (`superseded_by` on loser, edge in `memory.memory_edges`) |
| `add` (new text in the new array) | `flag_for_review` (LLM does not auto-insert; human approves) |
| (no equivalent) | `archive` (`expires_at = now() + 30d`) |

The biggest behavioural change: hermes-dream could **add** new
memory entries during synthesis. memory-dream does not — the
LLM is restricted to reorganizing existing rows. New facts must
flow through the runtime's normal write path (event worker,
SkillLoop connector, vault bridge) so they get the standard
audit + provenance treatment. This is a deliberate choice
documented in `SKILL.md`.

## What stays the same

- The conservative posture: when in doubt, `keep`.
- The pre-LLM dedup pass (exact / substring / prefix).
- The `review-then-adopt` gate (no automatic writeback).
- The cron cadence (biweekly ≈ `$2/year`).
- The fallback to a no-LLM `dedup-only` mode.

## What is new

- **pgvector semantic dedup** (threshold 0.92, configurable).
  hermes-dream's substring / prefix detection is the closest
  analogue, but it has no concept of semantic similarity.
- **Permission filtering by actor scope.** Every dedup and
  every adopt is scoped to one `user_id`; cross-actor merges
  are impossible by construction.
- **Audit log on every action.** `memory.audit_log` records
  the dream run id, the action, the row id, and a text preview
  of the change. The runtime's `verify_grounding()` and
  `build_diagnostic_report()` can pull from this to explain
  "why did this row get merged?".
- **Schema-level `superseded_by` and `contradicts` activation.**
  The receiving repo's `init_schema.sql` defined these fields
  (memory.typed_memory.superseded_by, memory.memory_edges.edge_type
  IN ('supersedes', 'contradicts')) but no code wrote to them.
  dream is the first code path that actually populates them.
