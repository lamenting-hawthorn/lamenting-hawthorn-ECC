# Architectural Contributions Roadmap

Beyond the skill catalog, the agent-architecture (ECC) runtime has
five cross-cutting infrastructure gaps. Each is a self-contained PR.

## The Five Contributions

### 1. Runtime Cost Governor with Model-Tier Fallback
**Problem:** Cost is post-hoc only. `/cost-report` reads a JSONL after
the fact. There is no per-session spend cap and no auto-downgrade
when the runtime is about to exceed budget.
**Solution:** A `CostGovernor` middleware wraps every
`LLMProvider.generate()` call. It tracks per-session tokens/dollars,
checks against `ECC_BUDGET_USD_PER_SESSION`, and silently swaps to
the next cheaper model tier when a threshold is crossed. Also emits
pre-flight cost estimates for tool-heavy prompts.
**Files:** `src/llm/governance/cost_governor.py`, `model_tiers.yaml`,
inject into `src/llm/providers/resolver.py`.
**Effort:** Medium.

### 2. Unified Resilience Middleware (Retry, Backoff, Circuit Breaker)
**Problem:** Each of the five LLM providers (claude, openai, astraflow,
atlas, ollama) independently greps for `"429"` in error messages.
No exponential backoff, no jitter, no circuit breaker, no
structured error classification — just duplicated ad-hoc checks.
**Solution:** A `ResilientProvider` decorator in
`src/llm/core/resilience.py` with retry (exp backoff + jitter),
circuit breaker (trip after N consecutive failures), and structured
error classification. Strip the duplicated 429 logic from each
provider and delegate to the middleware.
**Files:** `src/llm/core/resilience.py`, `RetryConfig` in
`interface.py`, refactor `src/llm/providers/*.py`.
**Effort:** Medium.

### 3. Skill Telemetry & Success-Rate Pipeline
**Problem:** 273 skills and 92 commands. Zero data on which are used,
which fail, which are slow, which are expensive. Skills are
"fire and forget."
**Solution:** A `TelemetryCollector` in `src/telemetry/` hooks skill
and command execution, records events to a local SQLite db
(`~/.ecc/telemetry.db`), and exposes a `/telemetry-report` command
showing top failures, biggest spenders, and dead weight. Hooks
into the LLM client to record token usage per call. Local-first by
design: no separate hosted service required.
**Files:** `src/telemetry/collector.py`, `reports.py`,
`commands/telemetry-report.md`, skill/command dispatch integration.
**Effort:** Medium.

### 4. LLM Call Cache (Deterministic Memoization)
**Problem:** Identical prompts (lint explanations, repeated doc
lookups) are re-sent to the API every time, burning tokens and
adding latency. No caching layer exists.
**Solution:** A `CachedProvider` wrapper in `src/llm/core/cache.py`.
Cache key = SHA-256 of normalized messages + model + temperature +
max_tokens. Pluggable backend: in-memory LRU (default) or SQLite.
Honors Anthropic `cache_control: {type: "ephemeral"}` headers.
Configurable via `ECC_LLM_CACHE_ENABLED` and `ECC_LLM_CACHE_TTL_SECONDS`.
**Files:** `src/telemetry/cache.py` (or `src/llm/core/cache.py`),
inject into `src/llm/providers/resolver.py`.
**Effort:** Small-Medium.

### 5. Memory Hygiene Worker (TTL Enforcement + Orphan Cleanup)
**Problem:** `init_schema.sql` defines `cleanup_expired_memory()` and
`cleanup_retrieval_logs()` but the `cron.schedule()` calls are
commented out. `memory.memory_edges` can accumulate orphaned edges
pointing to deleted rows. The database grows unbounded.
**Solution:** A standalone `scripts/hygiene-worker.py` daemon (NOT
a skill) that runs on a configurable interval, executes the
existing cleanup functions, adds a new `cleanup_orphan_edges()`
SQL function, and reports to the telemetry system (#3). Ships with
a launchd plist template.
**Files:** `scripts/hygiene-worker.py`, `init_schema.sql`,
`docs/hygiene.md`, launchd plist template.
**Effort:** Small.

## Recommended Build Order

1. **#3 (Telemetry)** — foundation that the others depend on.
   Cost Governor (#1) needs real usage data; Hygiene Worker (#5)
   should report metrics there.
2. **#4 (LLM Cache)** — immediate, measurable cost savings on day
   one. Self-contained.
3. **#5 (Hygiene Worker)** — small, prevents operational debt.
   Connects to telemetry from #3.
4. **#2 (Resilience)** — most architecturally impactful: deletes
   real duplicated code across 5 providers.
5. **#1 (Cost Governor)** — endpoint, but works best after #3 is
   feeding it real data.

## Dependencies Between Contributions

```
#3 Telemetry ─┬─> #1 Cost Governor (uses real usage data)
              ├─> #5 Hygiene Worker (reports metrics)
              └─> feeds /telemetry-report
#2 Resilience (independent)
#4 LLM Cache (independent)
```

The five are otherwise independent. #1 and #5 each gain value from
#3 but can land without it.
