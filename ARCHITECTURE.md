# Agent Architecture

## Overview

This repository is a Python-first reference architecture for a governed agent
runtime. It combines a LangGraph request workflow, Postgres-backed typed memory,
hybrid retrieval, graph expansion, trace diagnostics, and a read-only trace
export boundary for SkillLoop governance.

The system separates live runtime behavior from post-run learning:

```text
adapter -> graph.invoke -> retrieve -> model -> salience -> memory write
                                      |
                                      v
                              trace_export JSONL
                                      |
                                      v
                           SkillLoop evaluation/review
```

SkillLoop is not the canonical memory store in this design. It ingests exported
runtime traces, evaluates them, and proposes reviewed learning artifacts.

## The Three Layers

| Layer | What | Tables / Files |
|-------|------|----------------|
| **Layer 1 — Raw Event Store** | Append-only log of everything that happens. Source of truth. | `event_store.events` (time-partitioned) |
| **Layer 2 — Canonical Typed Memory** | Structured, retrievable, permissioned memory used by the agent at runtime. | `memory.typed_memory`, `memory.memory_edges`, `memory.retrieval_logs`, `memory.audit_log` |
| **Layer 3 — Retrieval + Workflow** | LangGraph state machine that orchestrates reasoning, tool use, and memory. | `src/graph.py`, `src/hybrid_retrieval.py`, `langgraph_deep_path.py` |

Data flows upward: events are written to Layer 1, a background worker drains
them through a salience gate into Layer 2, and Layer 3 retrieves from Layer 2
when answering user queries.

There is also a **Hermes native memory** layer (SQLite `state.db`) used for fast
session-local continuity, and an **Obsidian vault** used for owner-curated
knowledge. Both are bridged into Layer 2 by hourly jobs, not by the runtime
request path.

## The Three Hourly Jobs

The system runs three stateless, idempotent background jobs every hour:

| Job | Script | Minute | What it does |
|-----|--------|--------|--------------|
| **SkillLoop Controller** | `scripts/connect_skillloop.py` | :00 | Reads approved SkillLoop proposals and writes them into `typed_memory`. |
| **Vault Bridge** | `scripts/bridge_vault_and_sessions.py` | :05 | Imports Obsidian vault facts and Hermes session evidence into `typed_memory`. |
| **Notifier** | `scripts/notify_review.py` | :10 | Sends a Telegram digest of pending proposals and recent imports. |

These are designed to run via `launchd` on macOS or `cron` on Linux. See
`README.md` for full plist and cron examples.

## Three-Layer Memory Model

Postgres `typed_memory` stores three kinds of memory:

| Type | What | Retention | Examples |
|------|------|-----------|----------|
| **Episodic** | Session context, conversation history | 30 days (summarized after 7) | "User asked about server cost at 3pm" |
| **Semantic** | Facts, preferences, knowledge | Permanent (or until contradicted) | "User prefers Hetzner for hosting" |
| **Procedural** | Skills, workflows, playbooks | Permanent (versioned) | "Deploy process: build → test → push → restart" |

### How Rows Are Routed

- **Runtime path**: user message → `event_store.events` → background worker →
  `salience_gate()` → classify → `typed_memory`.
- **Vault path**: Obsidian markdown / Hermes SQLite → bridge script →
  `pseudonymize_payload()` → `typed_memory` with `source='knowledge_base_import'`
  or `source='hermes_import'`.
- **SkillLoop path**: approved JSONL proposals → connector script →
  `typed_memory` with `source='skillloop_proposal'` and idempotency checks.

Visibility is enforced through `owner_only` / `team` / `org` / `public` labels
plus RLS-ready schema design. Graph expansion must never broaden access beyond
the initial actor scope.

## PII Redaction Strategy

Before any content leaves the runtime boundary or is stored in typed memory,
it passes through a redaction layer adapted from the agendex risk core:

- **In-flight**: `src/redaction.py` pseudonymizes emails, phone numbers, URLs,
  API keys, and tokens. A reverse mapping is kept locally for debugging.
- **Trace export**: `src/trace_export.py` redacts actor metadata, message
  content, and tool arguments before producing SkillLoop JSONL.
- **Bridge scripts**: both `bridge_vault_and_sessions.py` and
  `connect_skillloop.py` call `pseudonymize_payload()` before inserting into
  Postgres.

No PII should reach SkillLoop or be stored in `typed_memory` without redaction.

## Runtime Path

1. Adapters normalize user input and resolve an explicit actor identity.
2. `src.graph` invokes the LangGraph workflow.
3. Retrieval runs Hermes-native memory and Postgres retrieval in parallel.
4. Postgres retrieval uses FTS plus optional vector search and RRF fusion.
5. Graph expansion is best-effort and permission-filtered by actor/org scope.
6. The model answers with retrieved context.
7. Salient user facts can be written to typed memory.
8. Trace events can be exported as SkillLoop-compatible JSONL.

Missing actor identity is a runtime error. Demo identity values are limited to
tests and examples.

## Embeddings

Local embeddings are the default:

```text
EMBEDDING_PROVIDER=local
LOCAL_EMBEDDING_MODEL=all-MiniLM-L6-v2
```

API embeddings are optional:

```text
EMBEDDING_PROVIDER=openai
EMBEDDING_API_KEY=...
EMBEDDING_MODEL=text-embedding-3-small
```

Do not mix vector spaces in the same table. If the provider or embedding model
changes, regenerate stored embeddings.

## SkillLoop Boundary

`src.trace_export` builds SkillLoop-compatible `AgentTrace` JSONL records from a
completed runtime turn. The export includes:

- stable trace IDs and timestamps
- actor/org metadata
- retrieved-context provenance
- runtime trace events represented as tool-call-like spans
- redacted message content and metadata
- normalized trace hashes

SkillLoop can ingest the result with:

```bash
skillloop --path /path/to/project ingest agent-architecture trace.jsonl
```

The v1 contract is offline/local governance over exported traces. SkillLoop does
not write directly back into runtime memory, prompts, skills, or model state.

## Design Docs

For full details, see the design documents in `.hermes/`:

- `.hermes/BRIDGE_DESIGN.md` — Vault-first bridge design
- `.hermes/SKILLOOP_CONNECTOR_DESIGN.md` — SkillLoop connector design
- `.hermes/ENTERPRISE_DIFFERENCES.md` — MVP vs multi-tenant gap analysis
- `.hermes/PERSONAL_USE.md` — Single-user build scope

## Production Status

This is a public reference implementation, not a turnkey hosted service. Before
production use, add deployment-specific identity, secrets management,
observability, backup/restore, RLS regression tests, and operational monitoring.
