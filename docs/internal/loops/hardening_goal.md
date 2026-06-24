# Goal loop prompt — agent_architecture hardening

## Goal condition
All of these commands must exit 0:

```bash
cd <HOME>/agent_architecture
/usr/bin/python3 smoke_test.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_hybrid_retrieval.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_graph_memory.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_adapters.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_hermes_memory_flow.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_all_phases.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_diagnostics.py
DATABASE_URL=postgresql:///agent_memory <PYTHON_PATH> src/test_guardrails.py
```

## Work to do (priority order)
1. **Hermes native memory visibility scoping** (privacy) — `src/hermes_native_memory.py:search()` uses `actor_id` only for direct-match short-circuit; cross-org `team`/`org`/`public` records from OTHER actors are returned if the org_id matches. Test "What is the capital of France?" returns stored fact from a previous turn. Fix: ensure visibility filtering is correct, AND make the test pass a unique thread_id/actor so cross-test pollution doesn't break the assertion. (See also `src/graph.py:retrieve_memory` which calls search with `actor_id` and `org_id`.)
2. **Stale smoke_test.py assertions** — test still expects API-embedding text in EMBEDDING_STRATEGY.md even though we switched to local. Update the assertions to match current state OR update EMBEDDING_STRATEGY.md to keep both historical context and the current stance. The right answer: keep EMBEDDING_STRATEGY.md saying local is current, update smoke_test.py to assert on actual current state.
3. **Stale "SKIP: No EMBEDDING_API_KEY" log in hybrid_retrieval.py** — API code path removed in June 2026 but log line still prints. Remove it.
4. **Graph memory test caller bug** — `src/test_graph_memory.py:test_get_related_memories` calls `gm.get_related_memories(mid, depth=1, limit=5)` without `user_id`; the safety check in `src/graph_memory.py:275` correctly fails closed. Fix: pass `user_id` in the test.
5. **WhatsApp adapter role resolution** — `test_all_phases.py:phase5_adapter` expects "owner" but gets "team". The adapter (`src/adapters/whatsapp.py`) needs the right precedence order.

## State file
`<HOME>/agent_architecture/.hermes/STATE.md`

## Each tick:

### 1. Read state
Read STATE.md to understand current progress and what happened last run.

### 2. Check the goal (CHECKER subagent — DIFFERENT model)
Delegate to a checker with goal: "Run all 9 commands in the Goal condition block of <HOME>/agent_architecture/.hermes/loops/hardening_goal.md. Return PASS/FAIL with the failing command and exit code for each FAIL."

### 3. If goal is MET:
- Update STATE.md "Stop conditions met since last review" with timestamp
- Final response: list the commands and their outputs
- Self-cancel: find this job by name, then `cronjob(action='remove', job_id=...)`

### 4. If goal is NOT met:
- Delegate to a worker subagent with goal: "Make progress on the next unfished item in priority order from <HOME>/agent_architecture/.hermes/loops/hardening_goal.md 'Work to do' section. State file at .hermes/STATE.md. Constraint: prefer minimal, surgical changes. Do not refactor. Do not add features not in the list. Update STATE.md before returning."
- After worker returns, run the smoke test inline. If it passes, dispatch the checker on all 9 commands.

### 5. Update STATE.md
- "## Last run" — timestamp + summary
- "## In progress" — what got worked on
- "## Completed today" — what finished
- "## Lessons learned" — anything future runs need
- "## Efficiency" — bump counters

## Hard stops
- Worker: max 3 attempts per tick before escalating to humans in STATE.md
- Total: self-cancel after goal met, or after 5 ticks of no progress

## Never do
- Touch init_schema.sql, ARCHITECTURE.md phase status, or CLIENT_HANDOFF.md (those are documentation contracts)
- Add new dependencies
- Add new tests beyond fixing the existing ones
- Refactor working code
