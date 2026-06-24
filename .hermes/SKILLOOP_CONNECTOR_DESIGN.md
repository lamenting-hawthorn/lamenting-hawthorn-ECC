# SkillLoop-to-Postgres Connector Design

## 1. Goal & Non-Goals

### Goal
Design a single-file connector (`scripts/connect_skillloop.py`) that reads approved memory proposals from SkillLoop's filesystem export and writes them into the `agent_architecture` Postgres `memory.typed_memory` schema. The connector is the durable-memory ingestion path for the human-reviewed SkillLoop pipeline.

### Non-Goals
- **No code is written** by this design document. Implementation is a follow-up task.
- The connector does **not** read from the SkillLoop SQLite review queue (`skillloop.db`). It only processes the `approved/` directory artifacts.
- The connector does **not** handle skill proposals (`.skillloop/approved/skill/*.md`). Those are out of scope.
- The connector does **not** generate embeddings. The `embedding` column is left `NULL`; the existing `event_worker.py` or a background job can backfill.
- The connector does **not** auto-approve proposals. Human review happens in SkillLoop; this connector only imports post-review artifacts.

## 2. Source Format

### 2.1 Current Approved Memory File Format

SkillLoop writes approved memory proposals via `skillloop.review.queue.write_approved_files()` (source: `<HOME>/skillloop/skillloop/review/queue.py`, lines 32-58).

Current behavior:
- Files land at `<project_root>/.skillloop/approved/memory/<proposal_id>.md`
- The file body is **plain text**: `proposal.content` written directly with no frontmatter.
- Example (`<HOME>/skillloop/.skillloop/approved/memory/ce7ad0a12413420d95ecf93f0db61272.md`):
  ```
  i prefer concise terminal summaries
  ```
- The filename stem (`ce7ad0a12413420d95ecf93f0db61272`) **is** the proposal `id`.

### 2.2 Metadata Gap & Recommended Enhancement

The current file contains **only** the distilled content. It lacks:
- `trace_id` (source trace reference)
- `score` (evaluator score 0-100)
- `evidence` (evaluator notes / tags)
- `memory_type` and `category` suggestions
- `evaluator_name` and `evaluator_version`

**Recommended design**: enhance `write_approved_files()` to emit YAML frontmatter before the content body. The connector design assumes this enhancement will be made. Example:

```markdown
---
proposal_id: ce7ad0a12413420d95ecf93f0db61272
trace_id: "7418ccc31697a136"
score: 85
evaluator: rubric
evaluator_version: "1.0"
evidence:
  - "User stated a durable preference"
  - "No secret patterns detected"
tags:
  - preference
  - terminal
suggested_memory_type: semantic
suggested_category: preference
---
i prefer concise terminal summaries
```

**Fallback if frontmatter is missing**: The connector parses the plain text as `content`, uses the filename stem as `proposal_id`, and queries the SkillLoop SQLite database (`<project_root>/.skillloop/skillloop.db`) to backfill missing metadata. This makes the connector resilient to the current format.

## 3. Target Schema

### 3.1 `memory.typed_memory`

| Column | Value / Derivation |
|--------|-------------------|
| `id` | `gen_random_uuid()` (auto) |
| `memory_type` | From frontmatter `suggested_memory_type`, or classified (see §4) |
| `category` | From frontmatter `suggested_category`, or classified (see §4) |
| `content` | PII-redacted body (see §4) |
| `summary` | `NULL` (the content is already distilled by SkillLoop) |
| `user_id` | `"owner:<USER>"` |
| `session_id` | `trace_id` from frontmatter, or `"skillloop_import"` if unavailable |
| `org_id` | `"personal"` |
| `role` | `"owner"` |
| `visibility` | `"owner_only"` |
| `confidence` | `min(score / 100.0, 0.95)` (see §5) |
| `source` | `"skillloop_proposal"` — **must be added to `init_schema.sql`** |
| `embedding` | `NULL` (backfilled later) |
| `metadata` | JSONB with SkillLoop provenance (see below) |
| `expires_at` | `NULL` |
| `superseded_by` | `NULL` |
| `source_event_id` | `NULL` |
| `created_at` | `NOW()` |
| `updated_at` | `NOW()` |

**Metadata JSONB shape**:
```json
{
  "skillloop_proposal_id": "ce7ad0a12413420d95ecf93f0db61272",
  "skillloop_evaluator": "rubric",
  "skillloop_evaluator_version": "1.0",
  "skillloop_score": 85,
  "skillloop_evidence": ["User stated a durable preference"],
  "skillloop_tags": ["preference", "terminal"],
  "skillloop_trace_id": "7418ccc31697a136",
  "skillloop_idempotency_key": "ce7ad0a12413420d95ecf93f0db61272",
  "redacted": true,
  "redacted_by": "connect_skillloop.py"
}
```

### 3.2 `memory.audit_log`

For every successful `typed_memory` insert, write:

| Column | Value |
|--------|-------|
| `event_type` | `"memory_written"` |
| `user_id` | `"owner:<USER>"` |
| `session_id` | `trace_id` or `"skillloop_import"` |
| `target_id` | The UUID of the newly inserted `typed_memory` row |
| `details` | `{"skillloop_proposal_id": "...", "source": "skillloop", "memory_type": "...", "category": "..."}` |

### 3.3 Schema Change Required

In `<HOME>/agent_architecture/init_schema.sql`, line 79-83, the `source` CHECK constraint must include `'skillloop_proposal'`:

```sql
source text not null default 'agent_inference'
check (source in (
    'user_utterance', 'tool_result', 'agent_inference',
    'knowledge_base_import', 'hermes_import',
    'system_generated', 'skillloop_proposal'
)),
```

## 4. Parsing Rules

### 4.1 File Discovery

Scan `<project_root>/.skillloop/approved/memory/*.md`. Skip:
- Hidden files (`.*`)
- Non-`.md` extensions
- Files whose stem is not a valid hex string (proposal IDs are UUID-ish hex)

### 4.2 Frontmatter Parsing

If the file starts with `---\n`, parse as YAML frontmatter using a safe loader (e.g., `yaml.safe_load`). Extract:
- `proposal_id` → fallback to filename stem
- `trace_id` → fallback to `"skillloop_import"`
- `score` → fallback to `0` (will be rejected by min_score filter)
- `evaluator` → fallback to `"unknown"`
- `evaluator_version` → fallback to `"0"`
- `evidence` → fallback to `[]`
- `tags` → fallback to `[]`
- `suggested_memory_type` → fallback to `None`
- `suggested_category` → fallback to `None`

Everything after the closing `---` is the `content` body.

If no frontmatter is present:
- `content` = entire file body
- `proposal_id` = filename stem
- All other fields = fallback values above
- The connector **optionally** queries `skillloop.db` (`proposals` table) to enrich metadata.

### 4.3 PII Redaction

Before writing to Postgres, run `pseudonymize_payload(content)` from `<HOME>/agent_architecture/src/redaction.py`. The redacted string becomes `content`. The reverse mapping is discarded (same pattern as the vault bridge).

### 4.4 Classification Fallback Chain

SkillLoop's `memory` distiller (`<HOME>/skillloop/skillloop/distill/memory.py`) currently produces proposals with `kind="memory"` but does **not** set `memory_type` or `category` on the `Proposal` object. The connector must classify.

**Layer 1 — Use frontmatter suggestions if valid:**
- `memory_type` must be one of `('episodic', 'semantic', 'procedural')`
- `category` must be one of `('fact', 'preference', 'interaction', 'action_item', 'correction', 'procedure', 'knowledge_base', 'org_approved')`

**Layer 2 — Heuristic classification from content:**

| Signal | `memory_type` | `category` |
|--------|--------------|------------|
| Content contains "i prefer", "i like", "i want", "always", "never" | `semantic` | `preference` |
| Content contains "when ... then ...", "first ... next ...", "steps", "workflow" | `procedural` | `procedure` |
| Content describes a past event / session / interaction | `episodic` | `interaction` |
| Content is a factual statement ("X is Y", "The API does Z") | `semantic` | `fact` |
| Content corrects a previous mistake | `semantic` | `correction` |
| Content is an action item / todo | `semantic` | `action_item` |
| Default (no signal) | `semantic` | `fact` |

**Layer 3 — Override by tag:**
If SkillLoop tags include `"preference"` → force `semantic` + `preference`.
If tags include `"workflow"` or `"procedure"` → force `procedural` + `procedure`.

### 4.5 Score Filtering

Proposals with `score < 70` (the SkillLoop `min_score` threshold from `policy.json`) are logged and skipped. This prevents low-confidence distillations from entering durable memory.

## 5. Score → Confidence Mapping

SkillLoop scores are integers `0-100`. Postgres `confidence` is `real` `0.0-1.0`.

```python
confidence = min(score / 100.0, 0.95)
```

| Score | Confidence | Rationale |
|-------|-----------|-----------|
| 100 | 0.95 | Hard cap; no memory is 100% certain |
| 95 | 0.95 | At cap |
| 90 | 0.90 | Direct mapping |
| 85 | 0.85 | Direct mapping |
| 70 | 0.70 | Minimum threshold |
| 50 | 0.50 | Would be filtered out (score < 70) |

The cap at `0.95` is a policy choice: even a perfect SkillLoop score does not guarantee ground truth. Human review raises confidence but does not make it absolute.

## 6. Idempotency Strategy

### 6.1 Idempotency Key

The proposal ID (filename stem) is the natural idempotency key. Store it in `metadata->>'skillloop_idempotency_key'`.

### 6.2 Unique Index

Create a unique partial index on `typed_memory`, following the vault bridge pattern (`<HOME>/agent_architecture/scripts/bridge_vault_and_sessions.py`, lines 477-480):

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_typed_memory_skillloop_idempotency
ON memory.typed_memory ((metadata->>'skillloop_idempotency_key'))
WHERE source = 'skillloop_proposal';
```

### 6.3 Upsert Logic

Use `ON CONFLICT` with `DO NOTHING`, identical to the vault bridge (`bridge_vault_and_sessions.py`, lines 607-617):

```sql
INSERT INTO memory.typed_memory
    (memory_type, category, content, summary, user_id, session_id,
     org_id, role, visibility, confidence, source, metadata)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT ((metadata->>'skillloop_idempotency_key'))
WHERE source = 'skillloop_proposal'
DO NOTHING
RETURNING id
```

If `RETURNING id` yields nothing, the row already exists; log as skipped.

### 6.4 Verify Mode

A `--mode=verify` run executes the connector twice and asserts:
1. Row counts are stable across both runs.
2. No duplicate `skillloop_idempotency_key` values exist for `source = 'skillloop_proposal'`.

## 7. Failure Handling

### 7.1 Malformed Markdown

- **Missing frontmatter**: Treat as plain text, use filename as proposal_id, query SQLite for metadata (if available), or use fallback values.
- **Invalid YAML frontmatter**: Log error with filename, skip file, continue.
- **Empty content body**: Log warning, skip file, continue.
- **Non-hex filename**: Log warning, skip file, continue.

### 7.2 Postgres Unavailable

- Wrap each batch in a transaction.
- If `psycopg.OperationalError`, log fatal error, exit with code `3`.
- Do not partially commit a batch.

### 7.3 Missing Trace Reference

If `trace_id` in metadata does not exist in `event_store.events`, this is **not** a hard failure. The `trace_id` is stored in `session_id` and `metadata->>'skillloop_trace_id'` for provenance, but there is no foreign key constraint. The connector proceeds.

### 7.4 Duplicate Detection (Pre-Insert)

Before attempting insert, the connector can short-circuit by querying:
```sql
SELECT id FROM memory.typed_memory
WHERE metadata->>'skillloop_idempotency_key' = %s AND source = 'skillloop_proposal'
```
This avoids unnecessary redaction work on re-runs.

## 8. Output Contract (CLI Modes, Logging)

### 8.1 CLI Interface

```bash
python scripts/connect_skillloop.py \
  --mode {full,incremental,dry-run,verify} \
  --project-root <HOME>/agent_architecture \
  --database-url postgresql:///agent_memory \
  --min-score 70 \
  --verbose
```

### 8.2 Modes

| Mode | Behavior |
|------|----------|
| `full` | Process all `approved/memory/*.md` files. Ignore checkpoint state. |
| `incremental` | Only process files newer than the last run checkpoint. Uses `memory.bridge_ingest_state` with `source = 'skillloop_proposal'` or a dedicated `memory.skillloop_imports` table. |
| `dry-run` | Parse all files, print intended writes as JSON lines, no database commit. |
| `verify` | Run `full` twice, assert idempotency (see §6.4). |

### 8.3 Checkpoint Table (Incremental Mode)

Reuse `memory.bridge_ingest_state` (already used by the vault bridge) with `source = 'skillloop_proposal'`:

```sql
INSERT INTO memory.bridge_ingest_state (source, last_run_at)
VALUES ('skillloop_proposal', NOW())
ON CONFLICT (source) DO UPDATE SET last_run_at = EXCLUDED.last_run_at;
```

For file-level incremental tracking, also store the max `mtime` of processed files or a dedicated table:

```sql
CREATE TABLE IF NOT EXISTS memory.skillloop_imports (
    proposal_id TEXT PRIMARY KEY,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    memory_id UUID REFERENCES memory.typed_memory(id)
);
```

### 8.4 Logging Format

All output is structured JSON (one line per event), consistent with the vault bridge:

```json
{"event": "connector_start", "mode": "full", "files_found": 12}
{"event": "processing_file", "proposal_id": "ce7ad0a12413420d95ecf93f0db61272", "trace_id": "7418ccc31697a136"}
{"event": "memory_written", "proposal_id": "ce7ad0a12413420d95ecf93f0db61272", "memory_id": "a1b2c3d4-...", "memory_type": "semantic", "category": "preference"}
{"event": "memory_skipped", "proposal_id": "abc123...", "reason": "already_exists"}
{"event": "memory_skipped", "proposal_id": "def456...", "reason": "score_below_threshold", "score": 45}
{"event": "connector_complete", "inserted": 5, "skipped": 7, "errored": 0, "runtime_seconds": 1.23}
```

### 8.5 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success (all files processed, no errors) |
| 1 | Fatal error (Postgres unavailable, schema mismatch) |
| 2 | Partial success (some files errored, but at least one inserted) |
| 3 | Idempotency verify failed |

## 9. File Outline

Proposed path: `<HOME>/agent_architecture/scripts/connect_skillloop.py`

Structure mirrors the vault bridge (`bridge_vault_and_sessions.py`) but is simpler because there is no vault/SQLite matching step.

```
connect_skillloop.py
├── Constants
│   ├── DEFAULT_PROJECT_ROOT = "<HOME>/agent_architecture"
│   ├── DEFAULT_DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
│   ├── ACTOR_ID = "owner:<USER>"
│   ├── ORG_ID = "personal"
│   ├── ROLE = "owner"
│   ├── VISIBILITY = "owner_only"
│   └── MIN_SCORE = 70
├── Dataclasses
│   ├── ConnectorConfig
│   └── ParsedProposal
├── SkillLoopScanner
│   ├── __init__(project_root: Path)
│   ├── iter_proposals() -> Iterator[ParsedProposal]
│   └── _parse_frontmatter(text: str) -> dict
├── Redactor (thin wrapper around pseudonymize_payload)
├── PostgresWriter
│   ├── __init__(url: str, dry_run: bool)
│   ├── _connect() -> psycopg.Connection
│   ├── ensure_schema() -> None
│   │   ├── CREATE UNIQUE INDEX idx_typed_memory_skillloop_idempotency
│   │   └── ALTER TABLE ... ADD 'skillloop_proposal' to CHECK (if needed)
│   ├── get_checkpoint(source: str) -> dict | None
│   ├── save_checkpoint(source: str) -> None
│   ├── write_memory(proposal: ParsedProposal, redacted: str) -> str | None
│   │   ├── INSERT INTO typed_memory ... ON CONFLICT DO NOTHING
│   │   └── INSERT INTO audit_log ...
│   ├── get_counts() -> dict
│   └── close()
├── ConnectorRunner
│   ├── __init__(config: ConnectorConfig)
│   ├── run() -> int
│   │   ├── Scan approved/memory/*.md
│   │   ├── Filter by checkpoint / mtime (incremental)
│   │   ├── Filter by score >= min_score
│   │   ├── Redact content
│   │   ├── Classify memory_type / category
│   │   ├── Write to Postgres
│   │   ├── Save checkpoint
│   │   └── Print stats
│   └── _report() -> None
├── run_verify(config: ConnectorConfig) -> int
└── main() -> int
```

## 10. Verification Plan

### 10.1 Unit Tests (to be written)

1. **Frontmatter parsing**: Given a markdown string with YAML frontmatter, return correct fields. Given plain text, return fallback values.
2. **Classification heuristics**: Given sample content strings, assert correct `memory_type` + `category`.
3. **Score mapping**: Given scores `[0, 50, 70, 85, 95, 100]`, assert confidence values `[0.0, 0.5, 0.7, 0.85, 0.95, 0.95]`.
4. **Redaction**: Given content with an email, assert `pseudonymize_payload` replaces it with `EMAIL_1`.
5. **Idempotency**: Mock Postgres; insert same proposal twice; assert second returns `None` (skipped).

### 10.2 Integration Tests

1. **Dry-run smoke test**: Run `--mode=dry-run --verbose` against an empty database. Verify JSON output contains expected rows.
2. **Full import**: Run `--mode=full` against a test database. Verify row counts match file counts.
3. **Incremental import**: Add a new approved file, run `--mode=incremental`. Verify only the new file is processed.
4. **Verify mode**: Run `--mode=verify`. Assert exit code 0 and stable counts.
5. **Audit log**: After import, query `memory.audit_log` where `event_type = 'memory_written'`. Count must equal inserted rows.

### 10.3 Schema Compatibility Check

Before any write, the connector runs:
```sql
SELECT 1 FROM pg_constraint
WHERE conname = 'typed_memory_source_check'
AND pg_get_constraintdef(oid) LIKE '%skillloop_proposal%'
```
If missing, log a clear error telling the user to update `init_schema.sql`.

## 11. Worked Examples

### Example 1: Preference Memory

**Approved file**: `.skillloop/approved/memory/ce7ad0a12413420d95ecf93f0db61272.md`
```markdown
---
proposal_id: ce7ad0a12413420d95ecf93f0db61272
trace_id: "7418ccc31697a136"
score: 85
evaluator: rubric
tags:
  - preference
  - terminal
suggested_memory_type: semantic
suggested_category: preference
---
i prefer concise terminal summaries
```

**Resulting `typed_memory` row**:
| Column | Value |
|--------|-------|
| `memory_type` | `semantic` |
| `category` | `preference` |
| `content` | `i prefer concise terminal summaries` |
| `confidence` | `0.85` |
| `source` | `skillloop_proposal` |
| `metadata` | `{"skillloop_proposal_id": "ce7ad0a12413420d95ecf93f0db61272", "skillloop_evaluator": "rubric", "skillloop_score": 85, "skillloop_tags": ["preference", "terminal"], "skillloop_trace_id": "7418ccc31697a136", "skillloop_idempotency_key": "ce7ad0a12413420d95ecf93f0db61272"}` |

### Example 2: Procedural Memory (Workflow)

**Approved file**: `.skillloop/approved/memory/a1b2c3d4e5f6789012345678abcdef01.md`
```markdown
---
proposal_id: a1b2c3d4e5f6789012345678abcdef01
trace_id: "abc123"
score: 92
evaluator: rubric
tags:
  - workflow
suggested_memory_type: procedural
suggested_category: procedure
---
when deploying to staging, first run tests, then build the docker image, then push to registry
```

**Resulting `typed_memory` row**:
| Column | Value |
|--------|-------|
| `memory_type` | `procedural` |
| `category` | `procedure` |
| `content` | `when deploying to staging, first run tests, then build the docker image, then push to registry` |
| `confidence` | `0.92` |
| `source` | `skillloop_proposal` |
| `session_id` | `abc123` |

### Example 3: Plain Text Fallback (No Frontmatter)

**Approved file**: `.skillloop/approved/memory/feedfacedeadbeef0011223344556677.md`
```
the api rate limit is 100 requests per minute
```

**Connector behavior**:
- `proposal_id` = `feedfacedeadbeef0011223344556677` (from filename)
- No frontmatter → query `skillloop.db` for metadata. If found: score=78, trace_id="session_001", evaluator="rubric".
- No `suggested_memory_type` → heuristic detects "api rate limit is ..." → `semantic` + `fact`.
- Content redacted (no PII found).

**Resulting `typed_memory` row**:
| Column | Value |
|--------|-------|
| `memory_type` | `semantic` |
| `category` | `fact` |
| `content` | `the api rate limit is 100 requests per minute` |
| `confidence` | `0.78` |
| `session_id` | `session_001` |

### Example 4: Score Below Threshold (Skipped)

**Approved file**: `.skillloop/approved/memory/lowscore0011223344556677.md`
```markdown
---
score: 45
---
some vague observation
```

**Connector behavior**: Logs `{"event": "memory_skipped", "reason": "score_below_threshold", "score": 45}`. No database write.

## 12. Open Questions for the Owner

1. **Should the connector also write `memory.memory_edges`?** For example, if a proposal references a known entity (e.g., "the API rate limit" links to an existing `semantic` fact), should we create a `derived_from` or `related_to` edge? Or keep the connector strictly 1:1 (one file → one `typed_memory` row)?

2. **Should `skillloop_proposal` be added to `init_schema.sql` before coding begins?** The design assumes yes, but this requires a schema migration (or at least an `ALTER` statement) on any existing database.

3. **Should some `skill` proposals map to `procedural` memory?** SkillLoop writes `.skillloop/approved/skill/*.md` for reusable workflows. A "deploy to staging" skill and a "deploy to staging" procedural memory are semantically identical. Should the connector eventually handle `skill/*.md` as `procedural` memory, or should skills remain a separate filesystem artifact?

4. **What is the launchd schedule?** The SkillLoop controller runs hourly (3600s). Should the connector share the same hourly cadence in a separate plist, or run daily? A separate plist with a 5-minute offset after SkillLoop is recommended.

5. **Should the connector auto-enhance the approved file format?** The current `write_approved_files()` writes plain text. Should SkillLoop be updated to emit YAML frontmatter (as recommended in §2.2), or should the connector rely on querying `skillloop.db` for metadata? The frontmatter approach is self-contained and avoids SQLite coupling; the SQLite approach works with the current format.
