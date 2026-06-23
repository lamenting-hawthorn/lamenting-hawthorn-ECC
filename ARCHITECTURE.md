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

## Memory Model

Postgres is the source of truth for durable runtime memory:

- `event_store.events`: append-only raw event log
- `memory.typed_memory`: semantic, episodic, and procedural memory
- `memory.memory_edges`: lightweight relationships between memories
- `memory.retrieval_logs`: retrieval observability
- `memory.trace_events`: runtime diagnostic steps

Visibility is enforced through user/org filters and RLS-ready schema design.
Graph expansion must never broaden access beyond the initial actor scope.

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

## Production Status

This is a public reference implementation, not a turnkey hosted service. Before
production use, add deployment-specific identity, secrets management,
observability, backup/restore, RLS regression tests, and operational monitoring.
