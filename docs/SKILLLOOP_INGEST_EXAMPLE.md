# SkillLoop Ingest Example

This walkthrough proves the governance boundary from a real local runtime turn
to a SkillLoop ingest artifact.

The example is intentionally offline:

- `MEMORY_BACKEND=fake`
- `CHECKPOINTER=memory`
- `AGENT_ARCHITECTURE_DISABLE_TTL_CLEANER=1`
- `TRACE_STORE_DISABLED=1`
- local fallback model behavior when no LLM key is configured

The script temporarily suppresses inherited `DATABASE_URL`, `LLM_API_KEY`, and
`LLM_BASE_URL` values while the sample turn runs, then restores the caller's
environment.

## 1. Export A Runtime Turn

From this repository:

```bash
python -B examples/export_skillloop_trace.py
```

The command invokes the LangGraph runtime once, builds a SkillLoop-compatible
trace from the completed state, and writes:

```text
examples/out/sample_runtime_turn_trace.jsonl
```

Expected output:

```text
Wrote SkillLoop trace export: /path/to/agent_architecture-public/examples/out/sample_runtime_turn_trace.jsonl

Ingest from a SkillLoop checkout with:
  skillloop --path . ingest agent-architecture /path/to/agent_architecture-public/examples/out/sample_runtime_turn_trace.jsonl

Expected output:
  Ingested agent_architecture trace sample-runtime-turn-001 (2 messages)
```

## 2. Ingest Into SkillLoop

From a SkillLoop checkout:

```bash
skillloop --path . ingest agent-architecture /path/to/agent_architecture-public/examples/out/sample_runtime_turn_trace.jsonl
```

Expected output:

```text
Ingested agent_architecture trace sample-runtime-turn-001 (2 messages)
```

## 3. Inspect The Trace

```bash
skillloop --path . traces show sample-runtime-turn-001
```

The ingested trace should contain:

- source: `agent_architecture`
- adapter: `agent_architecture_trace_export`
- user message from the sample runtime turn
- assistant message from the local runtime response
- tool-call-like runtime events for retrieval, model answer, and salience gate

## Boundary Rule

This path is read-only from the runtime's point of view. SkillLoop ingests and
evaluates exported JSONL; it does not write directly into Agent Architecture
memory, prompts, skills, or model state.
