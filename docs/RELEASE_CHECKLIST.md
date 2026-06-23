# Release Checklist

Run this before uploading the public repo to GitHub.

## Hygiene

- No `__pycache__`, `*.pyc`, `.DS_Store`, `.pytest_cache`, archives, local DBs, or `.env` files.
- No `client_handoff/` or `local_testing_workspace/` folders.
- No absolute personal paths or machine-specific workspace paths.
- No real tokens, API keys, webhook secrets, phone numbers, private DB URLs, or client data.

## Docs

- `README.md` describes the public project, not a client packet.
- `ARCHITECTURE.md` matches the current runtime path.
- `EMBEDDING_STRATEGY.md` documents local-first embeddings and optional API embeddings.
- SkillLoop is documented as a trace-ingestion governance sidecar, not the live memory store.

## Tests

```bash
python -B -m py_compile event_worker.py sync_wiki.py langgraph_deep_path.py src/*.py src/adapters/*.py
python -B -m pytest -p no:cacheprovider src/test_trace_export.py src/test_adapters.py -q
python -B smoke_test.py
```

Run Postgres integration tests only after creating and migrating a local
`agent_memory` database.

## Boundaries

- The private/source workspace remains read-only source material.
- Public work happens only in the public repository copy.
- Generated SkillLoop exports stay under ignored local output paths unless they are intentionally committed as small examples.
