# Embedding Strategy

## Default: Local First

The default embedding provider is local sentence-transformers:

```text
EMBEDDING_PROVIDER=local
LOCAL_EMBEDDING_MODEL=all-MiniLM-L6-v2
```

This path needs no API key. The default model returns 384-dimensional vectors,
which are zero-padded to 1536 dimensions for compatibility with the Postgres
`vector(1536)` schema.

## Optional: API Provider

API embeddings are supported as an explicit opt-in:

```text
EMBEDDING_PROVIDER=openai
EMBEDDING_API_URL=https://api.openai.com/v1/embeddings
EMBEDDING_API_KEY=...
EMBEDDING_MODEL=text-embedding-3-small
```

Use API embeddings when you need a hosted model, shared embedding quality across
systems, or do not want local model loading.

## Provider Rule

Do not mix embedding providers or models inside the same vector index. If you
change `EMBEDDING_PROVIDER`, `LOCAL_EMBEDDING_MODEL`, or `EMBEDDING_MODEL`,
regenerate existing stored embeddings before comparing vector scores.

## Query Routing

Retrieval uses tier routing:

- trivial queries: FTS only
- short queries: FTS first, vector if available
- full queries: FTS + vector + RRF fusion

Embeddings are cached by query hash to avoid repeated generation.

## Failure Behavior

If the configured embedding provider cannot generate a vector, retrieval falls
back to FTS-only search. Runtime should continue serving requests without an
embedding provider.
