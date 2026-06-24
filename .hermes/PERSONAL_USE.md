# Personal-Use Build Scope

## What this is

This is a **single-user, single-laptop, no-network** deployment of the
agent_architecture memory system. The user is <USER>, the host is their
laptop, and the only API caller is their own Hermes Agent runtime.

## What is NOT this

This is **not** the enterprise/multi-tenant build. There is:

- No multi-tenant isolation
- No cross-org RLS
- No admin/owner/team/reader role matrix
- No service-role vs request-role split
- No per-channel rate limiting
- No two-factor for admin actions
- No client data residency, GDPR, or compliance
- No production deploy runbook
- No webhook signature verification

The `ENTERPRISE_DIFFERENCES.md` doc is a future-state reference for when
this gets sold to a customer. **It is not the current build plan.**

## Current actual scope

| What | Status |
|---|---|
| Postgres + pgvector as canonical memory store | live |
| Three-layer model (semantic / episodic / procedural) | schema + worker; episodic now populated |
| Local SQLite Hermes native cache (FTS5) | live |
| Local sentence-transformers embeddings | live, $0 cost |
| Salience gate (regex) | working; LLM-based scoring deferred |
| PII redaction on write (agendex pseudonymizer pattern) | live |
| Event worker (drains event_store → typed_memory) | live, 71/71 events drained |
| Visibility model (owner_only / team / org / public) | live; only "owner_only" actively used |
| Memory viewer / edit / supersede controls | not built; use psql |
| Backup + restore rehearsal | not built; see "What to do next" below |

## What was deliberately NOT built

These were listed in the enterprise doc but **deferred** for personal use:

- Per-tenant RLS policies (one user, one org)
- Owner/admin approval queue for `org` promotion
- Audit log retention policies
- Per-region data residency
- Webhook signature verification
- Langfuse/Langfuse trace adapter
- Obsidian wiki sync
- n8n integration
- Admin UI

## What to do next (in priority order)

### 1. Bridge from real Hermes runtime to Postgres

The real Hermes Agent runtime stores all chats in `<HERMES_HOME>/sessions.db`
(SQLite). The agent_architecture scaffold has the schema, salience, and
worker in Postgres but is not currently wired to that runtime.

Build: `scripts/import_sessions.py` — reads `<HERMES_HOME>/sessions.db`,
extracts user/assistant message pairs, and pushes them into
`event_store.events` + `memory.typed_memory` with proper
`actor_id` / `org_id` / `role` / `visibility`.

After the bridge, the event worker (now working) will pick them up and
write them to typed_memory. From that point on, every chat the user has
with Hermes lives in Postgres, queryable with SQL.

### 2. Backfill or skip?

Two options:

- **Backfill once**: run the bridge on the existing `sessions.db`,
  importing all historical messages. Do this on a snapshot first.
- **Forward only**: import only new messages going forward, leave
  history in the SQLite file.

Backfill is the obvious choice if you want the full history in Postgres.
The risk is that the bridge miscategorizes something, so do a dry-run
that prints what it would write, then a real run.

### 3. Backup and restore

Personal use does not need enterprise-grade DR, but you do need
"if my laptop dies I don't lose my chats." Minimum viable:

```bash
# Nightly, cron or launchd
pg_dump -Fc agent_memory > ~/backups/agent_memory-$(date +%Y%m%d).dump

# Monthly, manual rehearsal
createdb agent_memory_restore
pg_restore -d agent_memory_restore ~/backups/agent_memory-latest.dump
# Spot-check: row counts in memory.typed_memory match expected
```

The `scripts/` folder is the right home for this.

### 4. Snapshot the live Hermes SQLite before bridging

Before running the bridge on real data:

```bash
# Stop Hermes
launchctl unload ~/Library/LaunchAgents/ai.hermes.runner.plist  # or whatever

# Snapshot
cp <HERMES_HOME>/sessions.db <HERMES_HOME>/sessions.db.pre-bridge-$(date +%Y%m%d)
sqlite3 <HERMES_HOME>/sessions.db ".backup <HERMES_HOME>/sessions.db.snapshot"

# Restart Hermes
launchctl load ~/Library/LaunchAgents/ai.hermes.runner.plist
```

The bridge should accept either a snapshot path or the live path, with
a `--dry-run` flag that prints intended writes without committing.

## What is just a personal-use nice-to-have

- A `psql` saved-queries file for common memory questions
- A small `tail -f` style viewer for the most recent N memory rows
- A weekly "what did I tell the agent this week" digest

None of these need to be built before the bridge.

## Summary

The current build is a working memory scaffold: Postgres schema, three
layers with episodic now populated, local SQLite cache, local embeddings,
redaction on write, and a working event worker. The next concrete step
is the SQLite-to-Postgres bridge so your real chats land in the schema.
Everything else (RLS, admin UI, compliance, multi-tenant) is enterprise
scope and is documented separately.
