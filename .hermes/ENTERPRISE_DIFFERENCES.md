# Enterprise Differences: Local MVP vs Multi-Tenant Production

**Scope:** What changes when the agent_architecture memory system moves from a local single-user MVP to an enterprise multi-tenant deployment.

**Audience:** Senior engineer evaluating the gap between the current scaffold and a sellable product.

**Voice:** Plain English. Concrete examples. Real table names from `init_schema.sql`.

---

## 1. TL;DR

- **Same:** Three-layer memory model (episodic/semantic/procedural), hybrid retrieval (FTS + vector + RRF), local embeddings via sentence-transformers, Postgres + pgvector as the canonical store, LangGraph for orchestration.
- **Different:** Identity becomes verified (not `demo_actor`), every row needs `org_id`, RLS policies become load-bearing, the event worker must actually run, PII must be redacted before storage, and audit logs are non-optional.
- **Cost implication:** A 50-person org doing ~1,000 turns/day will spend ~$200-400/month on LLM calls, ~$0 on embeddings if local, ~$50-100 on managed Postgres, and ~$20-50 on messaging provider fees. The local embedding model saves roughly $1,500-3,000/year vs API embeddings at that scale.
- **Biggest risk:** The current `MEMORY_BACKEND=fake` default and the `processing_attempts=5` deadlock in `event_worker.py` mean memory silently stops working in production.
- **Biggest lift:** RLS + identity + the admin UI for memory viewer/approval queue.
- **First customer milestone:** One org, 1-3 users, in-memory only, with PII redaction and verified identity.

---

## 2. Tenancy Model

### Single-user → multi-tenant

The current scaffold uses `user_id = 'demo_actor'` and `org_id = 'demo_org'` as hardcoded defaults in `src/durable_memory.py`. In enterprise, every row in every table is scoped to a real `org_id`.

### Per-org isolation via `org_id` on every row

The schema already has `org_id` on:
- `event_store.events`
- `memory.typed_memory`
- `memory.retrieval_logs`
- `memory.audit_log`
- `memory.trace_events`
- `memory.diagnostic_reports`

What changes: `org_id` can no longer be `NULL` for business memory. It must be set at write time by the gateway or adapter, not defaulted in Python.

### Row-level security (RLS) policies

The `init_schema.sql` already defines RLS policies, but they are scaffold-level. In production they become load-bearing:

```sql
-- Current policy (scaffold)
create policy memory_select on memory.typed_memory
    for select
    using (
        user_id = memory.get_current_user_id()
        or (
            visibility in ('team', 'org')
            and org_id is not null
            and org_id = memory.get_current_org_id()
        )
        or visibility = 'public'
    );
```

Enterprise changes:
- `owner_only` memory must be invisible to everyone except the owner, even within the same org. The current policy does NOT enforce this — it lets same-org team members see `team`/`org` memory but does not block them from `owner_only` memory via other paths.
- `reader` role must see ONLY `org`-visibility memory. The current `reader_select` policy is correct in principle but must be the ONLY path for readers.
- Cross-org queries must return zero rows. Full stop.

### The "service role" split

The schema already defines `memory.is_service_role()` and a `service_all` policy. In production this split is critical:

| DB user | RLS | Used by |
|---------|-----|---------|
| `app_requester` | RLS ON | Fast-path chat requests, retrieval, memory writes |
| `app_service` | RLS OFF (but audited) | Event worker, migrations, cleanup jobs, backup |

The request-path DB connection must always set:
```sql
SET LOCAL app.current_user = 'owner:15550000001';
SET LOCAL app.current_org = 'org_acme_2026';
SET LOCAL app.current_role = 'owner';
```

Using `SET LOCAL` (not `SET`) is required for PgBouncer transaction pooling.

### Concrete RLS example

For a memory row with `user_id='alice@acme.com'`, `org_id='org_acme_2026'`, `visibility='team'`:

| Actor | Role | Org | `memory_select` result |
|-------|------|-----|------------------------|
| `alice@acme.com` | `owner` | `org_acme_2026` | ✅ Visible (owner match) |
| `bob@acme.com` | `team` | `org_acme_2026` | ✅ Visible (same org, team visibility) |
| `carol@acme.com` | `reader` | `org_acme_2026` | ❌ Hidden (reader_select only sees `org` visibility) |
| `dave@rival.com` | `admin` | `org_rival_2026` | ❌ Hidden (cross-org) |

For a row with `visibility='owner_only'`:

| Actor | Role | Org | Result |
|-------|------|-----|--------|
| `alice@acme.com` | `owner` | `org_acme_2026` | ✅ Visible |
| `bob@acme.com` | `team` | `org_acme_2026` | ❌ Hidden (must be blocked explicitly) |

The current `memory_select` policy does NOT block `owner_only` from same-org team members. That is a production gap.

---

## 3. Identity and Access

### Replace demo identity fallback

Current code in `src/durable_memory.py`:
```python
@dataclass(frozen=True)
class MemoryInput:
    user_id: str = "demo_actor"
    org_id: str = "demo_org"
    role: str = "owner"
```

In enterprise, there is no default. If the gateway cannot verify identity, the request fails closed with HTTP 401. The `resolve_identity` node in `src/graph.py` already raises `ValueError("actor is required")` but the adapter layer must enforce this before the graph is invoked.

### Per-channel identity

| Channel | Identity format | Example |
|---------|----------------|---------|
| WhatsApp | `wa:<phone_number>` | `wa:15550000001` |
| Telegram | `tg:<user_id>` | `tg:123456789` |
| CLI | `cli:<os_username>` | `cli:<USER>` |
| API | `api:<client_id>` | `api:svc_billing` |

Each channel adapter maps its native identity to the internal `actor_id`. The gateway maintains a mapping table: `channel_native_id → actor_id → org_id → role`.

### Role matrix

| Role | Can read | Can write | Can promote to org | Can admin |
|------|----------|-----------|-------------------|-----------|
| `owner` | All memory in org + own `owner_only` | Yes | Yes | Yes |
| `admin` | All memory in org | Yes | Yes | Yes |
| `team` | `team`, `org`, `public` | Yes | No | No |
| `reader` | `org`, `public` only | No | No | No |
| `service` | All (bypass RLS) | Yes (worker) | No | No |

### Two-factor for org_admin actions

Any action that changes org-wide state requires re-verification:
- Promoting memory from `team` to `org`
- Approving `pending_org_approvals`
- Changing default visibility policy
- Bulk import from wiki/Obsidian

Implementation: the admin UI holds the action in a short-lived token (5-minute expiry) and requests an OTP or second-factor confirmation before executing.

### Owner approval queue

The `memory.pending_org_approvals` table already exists:
```sql
create table memory.pending_org_approvals (
    id              uuid not null default gen_random_uuid(),
    memory_id       uuid not null references memory.typed_memory(id) on delete cascade,
    proposed_by     text not null,
    proposed_at     timestamptz not null default now(),
    reviewed_by     text,
    reviewed_at     timestamptz,
    status          text not null default 'pending'
                    check (status in ('pending', 'approved', 'rejected')),
    review_notes    text,
    constraint pending_org_approvals_pk primary key (id)
);
```

In enterprise, NOTHING gets `visibility='org'` without a row in this table with `status='approved'` and `reviewed_by` set to an `owner` or `admin`. The worker that auto-classifies memory must default to `team` visibility and queue the promotion separately.

---

## 4. The Database Split

### Local single Postgres → primary + read replica

The current `.env.example` uses `DATABASE_URL=postgresql:///agent_memory` (local socket). Enterprise needs:
- **Primary:** Writes, event worker, admin mutations.
- **Read replica:** Lag-tolerant reads only — analytics dashboards, audit log
  queries, and chat-path retrieval that is happy to be a few seconds stale.
  The application must keep **read-after-write and approval flows** on the
  primary: typed_memory inserts, audit log writes, the SkillLoop connector,
  the vault bridge, and the event worker. Routing every `SELECT` to the
  replica would silently break "the user just approved this memory, is it in
  the system?" assertions.

### Shared schema with RLS vs per-customer schema isolation

| Approach | Pros | Cons |
|----------|------|------|
| **Shared schema + RLS** (recommended) | One migration path, simpler ops, easier cross-org analytics | RLS must be perfect; one bug leaks data |
| **Per-customer schema** | Stronger isolation, easier data export | Migration hell, connection pool fragmentation, harder to scale |
| **Per-customer database** | Strongest isolation | Operational nightmare, backup/restore complexity |

Recommendation: shared schema with RLS + `org_id` partitioning. The schema already has `force row level security` on all tables. Keep it.

### The "fail closed" principle

If `app.current_user`, `app.current_org`, or `app.current_role` is not set, the RLS policy returns no rows. This is correct. The current `memory.get_current_user_id()` uses `current_setting('app.current_user', true)` — the `true` means it returns NULL if unset, which causes the equality check to fail. That is the correct fail-closed behavior.

What must NOT happen: defaulting to `demo_actor` or `service` role when identity is missing. Remove all `user_id = "demo_actor"` defaults from the Python layer.

### Why `MEMORY_BACKEND=fake` is malpractice in production

`.env.example` line 7:
```
MEMORY_BACKEND=postgres
```

But `EMBEDDING_STRATEGY.md` line 85 says:
```
MEMORY_BACKEND=fake
```

And `src/graph.py` line 302:
```python
if os.environ.get("MEMORY_BACKEND", "postgres").lower() not in ("postgres", "durable"):
    return {"written_memory_ids": []}
```

If `MEMORY_BACKEND` is unset or `fake`, the system silently skips durable memory writes. In enterprise, this must be a startup fatal error, not a silent fallback.

---

## 5. Memory Layer Differences

### Same three-layer model

Episodic, semantic, procedural — unchanged. The `memory_type` check constraint in `init_schema.sql` already enforces this:
```sql
check (memory_type in ('episodic', 'semantic', 'procedural'))
```

### Episodic MUST have a TTL

The schema has `expires_at timestamptz` on `memory.typed_memory`, and `init_schema.sql` includes:
```sql
create or replace function memory.cleanup_expired_memory()
returns integer as $$
declare
    deleted_count integer;
begin
    delete from memory.typed_memory
    where expires_at is not null
      and expires_at < now();
    get diagnostics deleted_count = row_count;
    return deleted_count;
end;
$$ language plpgsql;
```

But in the current scaffold, `calculate_expiry()` in `event_worker.py` returns `NULL` for semantic/procedural and 30 days for episodic — yet the cleanup function is not scheduled. In enterprise:
- Episodic memory gets `expires_at = now() + interval '30 days'`
- A pg_cron job or application worker runs `cleanup_expired_memory()` every minute
- Expired episodic memory is either deleted or archived to cold storage (Parquet/S3)

### Visibility model becomes load-bearing

Current `src/graph.py` hardcodes `visibility="owner_only"` on all salience-gate writes. In enterprise, visibility is determined by the actor's role and the org's default policy:

| Actor role | Default visibility | Can override to |
|------------|-------------------|-----------------|
| `owner` | `owner_only` | `team`, `org`, `public` |
| `admin` | `team` | `org`, `public` |
| `team` | `team` | None |
| `reader` | N/A (cannot write) | N/A |

### Promotion workflow

A `team` memory cannot become `org` without:
1. Insert into `memory.pending_org_approvals`
2. Owner/admin reviews in admin UI
3. On approve: update `memory.typed_memory.visibility = 'org'`
4. Audit log entry: `event_type='memory_updated'`, details include `previous_visibility`, `new_visibility`, `approved_by`

### Audit log: every event

The `memory.audit_log` table already exists:
```sql
create table memory.audit_log (
    id              uuid not null default gen_random_uuid(),
    event_type      text not null
                    check (event_type in (
                        'memory_written', 'memory_updated', 'memory_deleted',
                        'memory_read', 'permission_denied', 'system_lint',
                        'verification_failed', 'fail_closed', 'checkpoint_cleaned'
                    )),
    user_id         text not null,
    session_id      text,
    target_id       uuid,
    details         jsonb not null default '{}',
    created_at      timestamptz not null default now(),
    constraint audit_log_pk primary key (id)
);
```

Enterprise additions:
- Add `org_id` to `audit_log` (missing in current schema)
- Log every `memory_read` with `details->>'query'` and `details->>'results_count'`
- Log every `permission_denied` with `details->>'attempted_action'` and `details->>'reason'`
- Retention: 7 years or per-client legal requirement

### The "no silent mutation" rule

Never auto-delete memory. If the user says "forget that", create a `superseded_by` relationship instead:
```sql
update memory.typed_memory
set superseded_by = <new_memory_id>,
    updated_at = now()
where id = <old_memory_id>;
```

Hard-delete only when:
- Client privacy policy requires it (GDPR right to erasure)
- A court order or legal hold requires it
- The data is PII that was stored in error

Even then, log the deletion in `audit_log` with `event_type='memory_deleted'`.

---

## 6. Salience and Ingestion

### Current salience gate is regex

`src/memory.py`:
```python
def should_propose_memory_write(user_text: str) -> bool:
    lowered = user_text.lower()
    triggers = ("remember this", "save this", "note that")
    identity_fact_patterns = (
        r"\bi am an?\s+[\w -]{3,80}",
        ...
    )
```

This is fine for MVP but misses nuance. In enterprise, the salience gate should use a small LLM or classifier that scores salience 0.0-1.0. Threshold: 0.6 to write, 0.3-0.6 to queue for review, <0.3 to drop.

### Async ingestion queue

Current `event_worker.py` is designed to run as a background worker but:
- It is not running in the current deployment (events pile up)
- It is synchronous per-event within the batch
- It blocks on embedding generation

Enterprise:
- Event worker runs as a systemd service (service file already in `event_worker.py` comments)
- Chat path writes to `event_store.events` immediately and returns
- Worker drains asynchronously
- Embedding generation happens in the worker, not the request path

### Background worker that actually runs

`event_worker.py` has `processing_attempts < 5` with a 15-minute stale lock:
```sql
AND (
    processing_started_at IS NULL
    OR processing_started_at < now() - interval '15 minutes'
)
AND processing_attempts < 5
```

The problem: if an event fails 5 times, it is silently abandoned forever. In enterprise:
- After 5 attempts, move to a `event_store.dead_letter_events` table
- Alert on dead-letter accumulation
- Do NOT leave the event in the main table with `processing_attempts = 5`

### Dead-letter handling for poison-pill events

Create a dead-letter table:
```sql
create table event_store.dead_letter_events (
    id uuid not null,
    event_type text not null,
    payload jsonb not null,
    processing_error text,
    processing_attempts integer not null,
    created_at timestamptz not null,
    moved_at timestamptz not null default now()
);
```

When `processing_attempts >= 5`, move the event here and alert via webhook/email.

### Duplicate detection

Current dedup is exact-match on `source_event_id`. Enterprise needs semantic dedup:
- Before inserting semantic memory, vector-search existing memory for the same user
- If cosine similarity > 0.92, merge/update instead of insert
- Log the merge in `audit_log`

---

## 7. PII and Secrets

### Redact before storage, not after

`src/graph.py` already imports `pseudonymize_payload`:
```python
from src.redaction import pseudonymize_payload
redacted = pseudonymize_payload({"content": text})
text = redacted.payload["content"]
```

But `.env.example` does not have a `REDACT_SECRETS=true` flag, and the current behavior depends on `MEMORY_RAW_WRITE_OK` being unset. In enterprise:
- Redaction is ON by default
- `MEMORY_RAW_WRITE_OK=true` requires owner explicit approval per-session
- Redaction runs BEFORE the memory write path, not as an afterthought

### Use the agendex pseudonymizer pattern

`src/redaction.py` is already adapted from agendex risk_core. It produces:
- Forward mapping: `user@example.com` → `EMAIL_1`
- Reverse mapping: `EMAIL_1` → `user@example.com`

The reverse mapping should be stored in a separate table (or encrypted vault) so that the owner can recover the original if needed, but the memory store only sees placeholders.

### Detect API keys, tokens, passwords

Add patterns to `src/redaction.py`:
- AWS keys: `AKIA...`, `ASIA...`
- GitHub tokens: `ghp_...`, `gho_...`
- OpenAI keys: `sk-...`
- Generic secrets: `-----BEGIN PRIVATE KEY-----`, `Bearer eyJ...`
- Database URLs in logs: redact password component

### Allow owner-approved exceptions

Some orgs WANT to store API keys in memory (e.g., "our staging DB URL is postgres://..."). The flow:
1. Redaction flags the secret
2. Memory is written with `confidence=0.3` and `visibility='owner_only'`
3. Admin UI shows it in the approval queue
4. Owner can approve storage of the raw value with `visibility='team'`
5. Audit log records the exception

### Redact in logs too

The `event_worker.py` `redact_db_url()` function is a start:
```python
def redact_db_url(db_url: str) -> str:
    if "@" not in db_url:
        return db_url
    scheme, rest = db_url.split("://", 1)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***:***@{host}"
```

Extend this to:
- LLM API keys in trace events
- Raw message payloads in error logs
- Phone numbers and emails in retrieval logs

---

## 8. Retrieval and Quality

### Same hybrid retrieval

FTS + vector + RRF is unchanged. `src/hybrid_retrieval.py` implements this correctly.

### Visibility filtering MUST be enforced at SQL level

Current `src/hybrid_retrieval.py` does filter in SQL:
```sql
WHERE user_id = %s
  AND (%s::text IS NULL OR org_id = %s::text OR visibility = 'owner_only')
```

But this is Python-constructed SQL, not RLS. In enterprise, the retrieval function must set RLS context and let the policy enforce it:
```sql
SET LOCAL app.current_user = %s;
SET LOCAL app.current_org = %s;
SET LOCAL app.current_role = %s;
-- then query without user_id/org_id in WHERE clause
```

This ensures that even if the Python layer has a bug, Postgres blocks unauthorized rows.

### Graph expansion MUST respect permissions

`src/graph.py` lines 175-188:
```python
try:
    from src.graph_memory import GraphMemory
    gm = GraphMemory()
    seed_ids = [str(r["id"]) for r in postgres_results]
    related = gm.expand_graph(seed_ids, depth=1, limit=3,
                              user_id=actor["actor_id"],
                              org_id=actor.get("org_id"))
    postgres_results = postgres_results + related
except Exception:
    pass  # Graph expansion is best-effort
```

The `except: pass` is dangerous. If graph expansion fails due to a permissions error, the system silently loses context. In enterprise:
- Graph expansion failures are logged as `trace_events` with `status='error'`
- If permissions fail, the retrieval continues WITHOUT graph expansion (don't crash the chat)
- But the failure is visible in diagnostics

### Retrieval-quality regression tests

Required test cases:
1. **Exact fact recall:** "What stack does Mo Memory use?" → returns the fact row
2. **Semantic recall:** "What technology powers the agent?" → returns the same fact row
3. **Visibility boundaries:** A `team` memory from org A must not appear in org B's retrieval
4. **No irrelevant context:** A query about "server costs" must not return "favorite color" memories
5. **No private graph leakage:** Graph expansion from a `team` memory must not pull in `owner_only` memories

### Eval layer

Add contradiction detection:
- When a new memory is written, check for existing memories with opposite meaning
- If found, lower confidence of both to 0.5 and flag for review

Add confidence recalibration:
- Track how often a memory appears in successful retrievals
- Boost confidence for frequently-used memories
- Lower confidence for never-retrieved memories

---

## 9. External Integrations

### WhatsApp / Telegram

Current adapters (`src/adapters/whatsapp.py`, `src/adapters/telegram.py`) map owner IDs from env vars. Enterprise requirements:
- **Webhook signature verification:** Verify WhatsApp Business API signature (`X-Hub-Signature-256`) and Telegram bot token on every webhook
- **Rate limits per channel:** Max 30 messages/minute per WhatsApp number, 20/minute per Telegram chat
- **Message dedup:** Use `platform_message_id` as idempotency key in `event_store.events.idempotency_key`

### n8n

n8n can trigger webhooks and receive events. Boundaries:
- n8n MAY trigger a memory retrieval via API
- n8n MAY receive a webhook when memory is updated
- n8n MUST NOT write directly to `memory.typed_memory`
- n8n MUST NOT bypass RLS
- n8n MUST NOT decide salience
- n8n MUST NOT own checkpointing

### LangSmith / Langfuse

Optional. Use for:
- Trace export and debugging
- Prompt regression tests
- Eval runs

LangSmith is NOT:
- The memory database
- The auth layer
- The permission system
- The source of truth for RLS

### Obsidian / wiki layer

`ARCHITECTURE.md` line 220 explicitly states:
> "Obsidian/wiki: Owner-only source and synthesis workspace. Not canonical DB and not default retrieval."

Keep this boundary. The wiki is an import source. After import, the memory lives in `memory.typed_memory` with `source='knowledge_base_import'` and `visibility='owner_only'`. The wiki file is not queried at runtime.

---

## 10. Observability and Operations

### Health check endpoint

Required checks:
1. DB reachable: `SELECT 1`
2. pgvector available: `SELECT * FROM pg_extension WHERE extname = 'vector'`
3. Checkpoint tables available: `SELECT COUNT(*) FROM langgraph.checkpoints`
4. Embedding provider reachable: local model loaded or API responds
5. Worker queue healthy: `SELECT COUNT(*) FROM event_store.events WHERE processed_at IS NULL` < threshold (e.g., 100)

Return JSON:
```json
{
  "status": "healthy",
  "checks": {
    "db": "ok",
    "pgvector": "ok",
    "checkpoints": "ok",
    "embeddings": "ok",
    "worker_queue": "ok",
    "unprocessed_events": 3
  }
}
```

### Request logging

Every request logs:
- `request_id` (UUID)
- `actor_id`
- `org_id`
- `channel` (whatsapp/telegram/cli/api)
- `latency_ms`
- `failure_reason` (if any)
- `memory_ids_retrieved` (array)

Redact secrets and PII before writing to logs. Use the same pseudonymizer as the memory write path.

### Monitoring metrics

| Metric | Alert threshold | Tool |
|--------|----------------|------|
| Error rate | > 1% of requests | Prometheus / Datadog |
| Retrieval latency p99 | > 500ms | Postgres `retrieval_logs.latency_ms` |
| Embedding latency p99 | > 50ms | Local: in-app timer; API: provider metric |
| Worker queue depth | > 500 unprocessed events | `event_store.events` count |
| Memory write rate | Drop to 0 for > 5 min | `audit_log` insert rate |
| Permission denied rate | > 0.5% of requests | `audit_log` where `event_type='permission_denied'` |

### Daily memory review

Automated job that:
1. Samples 50 recent memories from `memory.typed_memory`
2. Flags low-confidence (< 0.6) or unretrieved memories
3. Supersedes obviously bad memories (duplicates, contradictions)
4. Tunes salience threshold based on drop rate

### Weekly review

Human-driven (owner/admin):
1. Review `org_approved` knowledge for staleness
2. Check `pending_org_approvals` queue
3. Review retrieval zeros (`memory.retrieval_zeros` view)
4. Identify knowledge gaps (frequent queries with no results)

---

## 11. Backup, Retention, and Compliance

### Daily Postgres backup

Use `pg_dump` or managed backup (AWS RDS, Google Cloud SQL, Supabase). Backup must include:
- All schemas: `event_store`, `memory`, `langgraph`
- All RLS policies and roles
- All indexes (including pgvector HNSW)

### Restore rehearsal monthly

Current status: not tested. In enterprise:
- Monthly restore to a staging environment
- Verify RLS policies still enforce correctly after restore
- Verify embedding index quality (rebuild if needed)
- Document RTO (Recovery Time Objective) and RPO (Recovery Point Objective)

### Point-in-time recovery

If using managed Postgres, enable PITR. If self-hosted, use WAL archiving to S3.

### Retention by source and visibility

| Memory type | Visibility | Retention | Action after expiry |
|-------------|-----------|-----------|---------------------|
| Episodic | any | 30 days | Delete or archive to Parquet |
| Semantic | `owner_only` | Permanent until superseded | Keep |
| Semantic | `team` | Permanent until superseded | Keep |
| Semantic | `org` | Permanent until superseded | Keep |
| Procedural | any | Versioned | Keep all versions |
| Audit log | any | 7 years | Keep |
| Retrieval logs | any | 30 days | Delete |
| Trace events | any | 30 days | Delete |

### Audit log retention

Per client legal/business requirement. Common: 7 years for financial services, 3 years for SaaS, indefinite for healthcare (HIPAA).

### Client privacy policy language

Required disclosures:
- What is stored: conversation text, inferred facts, preferences, procedures
- Who can access: owner sees all; team sees team/org memory; readers see org memory only
- How deletion works: request via admin UI; tombstone within 24 hours; physical deletion per policy
- How export works: JSON/CSV export of all memory for a user/org
- Data residency: EU data stays in EU; US data stays in US

### Data residency

If selling in EU:
- Postgres instance in EU region
- LLM API calls to EU-hosted endpoints (if using API embeddings)
- Embedding generation stays local (no data leaves the server)
- Backup stored in EU S3 bucket

---

## 12. Cost Model

### Per-client cost breakdown (50 users, ~1,000 turns/day)

| Line item | Unit cost | Monthly volume | Monthly cost |
|-----------|-----------|----------------|-------------|
| LLM calls (DeepSeek-v4-flash) | ~$0.001/turn | 30,000 turns | ~$30 |
| LLM calls (retrieval context) | ~$0.002/turn | 30,000 turns | ~$60 |
| Embedding (local sentence-transformers) | $0 | 60,000 embeddings | $0 |
| Embedding (if API: OpenAI 3-small) | $0.02/1K | 60,000 | ~$120 |
| Database (managed Postgres, 100GB) | ~$75/mo | 1 instance | ~$75 |
| Worker compute (EC2 t3.medium) | ~$30/mo | 1 instance | ~$30 |
| Messaging (WhatsApp Business API) | ~$0.005/msg | 10,000 msgs | ~$50 |
| Observability (Datadog/CloudWatch) | ~$20/mo | 1 account | ~$20 |
| **Total (local embeddings)** | | | **~$265/mo** |
| **Total (API embeddings)** | | | **~$385/mo** |

### Local embedding savings

At 60,000 embeddings/month:
- OpenAI text-embedding-3-small: ~$120/mo
- Voyage voyage-4-lite: ~$180/mo
- Local all-MiniLM-L6-v2: $0 + ~$30/mo compute amortized

**Savings: ~$1,500-3,000/year per client** depending on API provider.

### Storage growth

| Memory type | Growth rate | 1-year size (50 users) |
|-------------|-------------|----------------------|
| Episodic | ~50 MB/month | ~600 MB |
| Semantic | ~20 MB/month | ~240 MB |
| Procedural | ~5 MB/month | ~60 MB |
| Event store | ~200 MB/month | ~2.4 GB |
| Audit log | ~10 MB/month | ~120 MB |
| **Total** | | **~3.4 GB** |

Well within a 100GB managed Postgres instance.

---

## 13. The Migration Plan

How to go from local MVP to first paying customer:

1. **Finish the scaffold**
   - Fix `event_worker.py` poison-pill handling (move to dead-letter after 5 attempts)
   - Implement episodic TTL enforcement (`cleanup_expired_memory` scheduled)
   - Populate `audit_log` on every read/write/supersede
   - Ensure worker runs as a systemd service

2. **Add per-tenant RLS policies**
   - Fix `owner_only` isolation (block same-org team members)
   - Enforce `reader` role to `org` visibility only
   - Add cross-org leak tests
   - Remove all `demo_actor` defaults

3. **Replace demo identity with verified gateway identity**
   - Adapter layer maps WhatsApp JID / Telegram ID / CLI user to `actor_id`
   - Gateway enforces: no identity → 401
   - `src/durable_memory.py` removes all defaults

4. **Add memory viewer + edit/delete/supersede controls (admin UI)**
   - Filter by user, org, visibility, source, type, category, confidence, date
   - Edit content with audit trail
   - Supersede with `superseded_by` link
   - Delete only with owner/admin approval + audit log

5. **Run the permission regression test suite**
   - Cross-org memory leak: FAIL if any row leaks
   - Reader boundary: FAIL if reader sees `team` memory
   - Graph expansion boundary: FAIL if graph pulls `owner_only` via team seed
   - Imported memory: FAIL if Hermes import bypasses RLS

6. **Pilot with one customer, 1-3 users, in-memory only mode**
   - Use `HermesNativeMemoryStore` (SQLite) for fast iteration
   - Verify identity flow end-to-end
   - Verify PII redaction
   - Do NOT use Postgres yet

7. **Add PII redaction before storage**
   - Enable `pseudonymize_payload` by default
   - Add API key detection patterns
   - Test redaction in logs too

8. **Move pilot to multi-tenant Postgres with RLS**
   - Apply `init_schema.sql` to managed Postgres
   - Set RLS context on every connection
   - Verify retrieval returns same results as SQLite pilot

9. **Onboard customer employees**
   - Import employee list with roles
   - Set default visibility policy
   - Train owner on approval queue

10. **Production rollout with backup/restore/recovery runbook**
    - Daily automated backup
    - Monthly restore rehearsal
    - Documented RTO/RPO
    - Health check endpoint monitored

11. **Add observability (health check, logging, rate limits)**
    - `/health` endpoint
    - Request logging with redaction
    - Rate limits: per user, per org, per channel
    - Alerting on worker queue depth and error rate

12. **Document the admin onboarding checklist**
    - Owner identity setup
    - Employee list + role mapping
    - Channel IDs (WhatsApp, Telegram)
    - Default visibility policy
    - Approval policy

13. **Add the eval layer (contradiction detection, confidence scoring)**
    - Contradiction detection on memory write
    - Confidence recalibration based on retrieval frequency
    - Low-confidence flagging for human review

---

## 14. What NOT to Do

- **Don't make n8n own memory writes.** n8n is glue. It triggers webhooks. It does not decide what gets remembered or who can see it.
- **Don't make LangSmith the memory DB.** LangSmith traces runs. It does not store `memory.typed_memory`. It does not enforce RLS.
- **Don't make Obsidian the canonical store.** Obsidian is an import source. Runtime retrieval queries `memory.typed_memory`, not markdown files.
- **Don't skip the audit log "to save space."** Audit logs are the compliance backbone. A 50-user org generates ~10 MB/month. Storage is cheap. Legal liability is not.
- **Don't add a UI before the backend is stable.** The admin memory viewer is Phase 2 of enterprise rollout. First make RLS, identity, and retrieval bulletproof.
- **Don't add a feature that bypasses RLS "just for admin."** If an admin needs to see cross-org data, use a separate read-replica connection with explicit audit logging. Never disable RLS on the request path.
- **Don't hard-delete memory unless client policy requires it.** The default is tombstone/supersede. Hard-delete is for GDPR erasure requests, not for "cleaning up."
- **Don't mix vector spaces.** If you switch from local 384-dim to API 1536-dim, create a new column or re-embed everything. Do not mix dimensions in the same `vector(1536)` column.
- **Don't leave `MEMORY_BACKEND=fake` as a silent fallback.** If Postgres is unreachable, fail loudly. Silent data loss is worse than downtime.
- **Don't ignore the worker queue.** If `event_store.events` has 500+ unprocessed rows for > 10 minutes, page someone. The worker is not optional.

---

## Appendix: Schema Quick Reference

Key tables from `init_schema.sql`:

| Table | Purpose | Enterprise critical |
|-------|---------|---------------------|
| `event_store.events` | Raw event log | Yes — source of truth |
| `memory.typed_memory` | Canonical memory | Yes — RLS enforced |
| `memory.memory_edges` | Graph relationships | Yes — permission-safe expansion |
| `memory.retrieval_logs` | Retrieval observability | Yes — quality monitoring |
| `memory.audit_log` | Compliance log | Yes — 7-year retention |
| `memory.pending_org_approvals` | Promotion queue | Yes — owner approval |
| `memory.trace_events` | Diagnostic traces | Yes — debugging |
| `memory.diagnostic_reports` | Human approval queue | Yes — remediation |

Key columns:

| Column | Tables | Purpose |
|--------|--------|---------|
| `org_id` | All | Tenant isolation |
| `user_id` | All | Owner identity |
| `role` | `events`, `typed_memory` | Creator's role at write time |
| `visibility` | `typed_memory` | `owner_only` / `team` / `org` / `public` |
| `confidence` | `typed_memory` | 0.0-1.0 quality score |
| `source` | `typed_memory` | `user_utterance`, `agent_inference`, `hermes_import`, etc. |
| `expires_at` | `typed_memory` | TTL for episodic memory |
| `superseded_by` | `typed_memory` | Tombstone link |
| `processing_attempts` | `events` | Worker retry counter (dead-letter at 5) |
| `idempotency_key` | `events` | Deduplication |

---

*Document generated from actual codebase: `init_schema.sql`, `src/graph.py`, `src/hybrid_retrieval.py`, `src/durable_memory.py`, `src/redaction.py`, `event_worker.py`, `ARCHITECTURE.md`, `EMBEDDING_STRATEGY.md`, `START_HERE.md`, `CLIENT_POST_DEPLOY_TODO.md`.*
