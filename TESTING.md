# Testing Guide

This document shows how to run the test suite and what each test covers.

## Quick Start

```bash
# Fast smoke test (no Postgres required)
python -B smoke_test.py

# Full test suite (requires Postgres with schema applied)
export DATABASE_URL=postgresql:///agent_memory
python -B -m pytest -p no:cacheprovider src -q
```

## The 9 Core Tests

| Test File | What It Covers | Postgres Required |
|-----------|---------------|-------------------|
| `src/test.py` | Phase 1–4: LangGraph workflow, durable memory insert, two-turn query. | Optional (uses fake backend if `DATABASE_URL` is unset) |
| `src/test_postgres_checkpoint.py` | Phase 2: LangGraph checkpoint persistence in Postgres. | Yes |
| `src/test_durable_memory.py` | Phase 3: Direct typed_memory insert and retrieval. | Yes |
| `src/test_graph_durable_memory.py` | Phase 4: Graph workflow wired to durable memory write-behind. | Yes |
| `src/test_hybrid_retrieval.py` | Phase 5: Hybrid search (FTS + pgvector + RRF fusion). | Yes |
| `src/test_graph_memory.py` | Phase 6: Entity extraction, edge creation, related-memory expansion. | Yes |
| `src/test_adapters.py` | Phase 7: WhatsApp JID normalization, Telegram actor resolution, adapter end-to-end. | Yes |
| `src/test_diagnostics.py` | Trace events, diagnostic reports, and approval-gated diagnostics. | Yes |
| `src/test_guardrails.py` | Tool failure budget (5-stop) and context compaction guardrails. | No |

### Running Individual Tests

```bash
export DATABASE_URL=postgresql:///agent_memory

# Phase 1–4
python src/test.py

# Phase 2 checkpointing
python src/test_postgres_checkpoint.py

# Phase 3 durable memory
python src/test_durable_memory.py

# Phase 4 graph + durable memory
python src/test_graph_durable_memory.py

# Phase 5 hybrid retrieval
python src/test_hybrid_retrieval.py

# Phase 6 graph memory
python src/test_graph_memory.py

# Phase 7 adapters
python src/test_adapters.py

# Diagnostics / tracing
python src/test_diagnostics.py

# Guardrails (no DB)
python src/test_guardrails.py
```

## Expected Output for a Clean Run

A successful run looks like this:

```text
$ python -B -m pytest -p no:cacheprovider src -q
............
12 passed in 4.32s
```

If Postgres is unavailable, tests that require it are skipped:

```text
$ python -B -m pytest -p no:cacheprovider src -q
s..s....s..
9 passed, 3 skipped in 1.10s
```

## How to Add a New Test

1. Create `src/test_<feature>.py`.
2. Use the standard `pytest` style or a `main()` entrypoint.
3. If the test needs Postgres, guard it with a helper:

   ```python
   import os
   import psycopg

   def _postgres_available() -> bool:
       try:
           with psycopg.connect(os.environ.get("DATABASE_URL", "postgresql:///agent_memory")):
               return True
       except Exception:
           return False
   ```

4. Import the project root into `sys.path` so `import src` works:

   ```python
   from pathlib import Path
   import sys
   ROOT = Path(__file__).resolve().parents[1]
   if str(ROOT) not in sys.path:
       sys.path.insert(0, str(ROOT))
   ```

5. Run it standalone before adding to the suite:

   ```bash
   DATABASE_URL=postgresql:///agent_memory python src/test_<feature>.py
   ```

## CI Pipeline

There is no hosted CI configured in this repository. The recommended local
validation before pushing is:

```bash
python -B -m pytest -p no:cacheprovider src -q
python -B smoke_test.py
```

For a full release checklist, see `docs/RELEASE_CHECKLIST.md`.
