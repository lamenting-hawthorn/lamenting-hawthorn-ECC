# ECC Memory Hygiene Worker

`scripts/hygiene-worker.py` is a long-running cleanup daemon for the
ECC Postgres memory layer. Without it, the database grows
unbounded: expired `typed_memory` rows pile up, retrieval logs from
months ago accumulate, and `memory_edges` accumulates orphan
references to deleted rows.

## What it does

Every `interval` seconds (default: 3600 = 1 hour), the worker:

1. Calls `memory.run_hygiene_pass()` — a single SQL function in
   `init_schema.sql` that returns a `(operation, deleted_count)`
   row per cleanup operation. Currently six operations:
   - `retrieval_logs` — drops `memory.retrieval_logs` and
     `memory.trace_events` older than 30 days.
   - `expired_memory` — drops `memory.typed_memory` rows whose
     `expires_at` is in the past.
   - `orphan_edges` — drops `memory.memory_edges` whose
     `source_id` or `target_id` no longer matches a live
     `typed_memory.id` (added in this PR).
   - `audit_log` — drops `memory.audit_log` rows older than
     90 days (compliance-retention policy).
   - `pending_approvals` — drops `memory.pending_org_approvals`
     rows that have been `pending` for more than 30 days.
   - `old_dream_runs` — drops `memory.dream_runs` rows in
     terminal states (`completed`/`failed`/`discarded`) older
     than 30 days. Cascade-deletes the linked
     `memory.dream_proposals` via the foreign key.
2. Records one telemetry event per operation to the local telemetry
   database (`~/.ecc/telemetry.db` by default), with name
   `cleanup_<operation>`, kind `agent`, and actor `hygiene-worker`.
3. Logs a one-line pass summary to stderr in the form:
   ```
   hygiene pass (0.42s): retrieval_logs=12, expired_memory=0, orphan_edges=4, audit_log=50, pending_approvals=2, old_dream_runs=1
   ```

In dry-run mode, the worker prints what it would do without
touching the database.

## Operational modes

| Flag | Behavior |
|------|----------|
| `--once` | Run a single pass and exit. Used by CI and manual triggers. |
| `--interval N` | Seconds between passes in daemon mode. Default 3600. |
| `--dry-run` | Print what would run without touching the database. |
| `--database-url URL` | Override `$DATABASE_URL`. |
| `--telemetry-db PATH` | Override the telemetry database path. |
| `--log-level LEVEL` | DEBUG / INFO / WARNING / ERROR. Default INFO. |

## Examples

One-shot pass (CI / manual):

```bash
python scripts/hygiene-worker.py --once
```

Daemon with 30-minute interval:

```bash
python scripts/hygiene-worker.py --interval 1800
```

Dry run to validate the SQL without touching data:

```bash
python scripts/hygiene-worker.py --once --dry-run
```

## Launchd (macOS)

`docs/launchd/com.ecc.hygiene.plist` ships with the repo. To install:

```bash
bash scripts/install-hygiene-worker.sh --load
```

The script substitutes `<HOME>` and `<PYTHON_PATH>` placeholders with
absolute paths, writes the plist to
`~/Library/LaunchAgents/com.ecc.hygiene.plist`, and loads it via
`launchctl`. Omit `--load` to write the plist without loading.

To uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.ecc.hygiene.plist
rm ~/Library/LaunchAgents/com.ecc.hygiene.plist
```

The plist runs the worker hourly via `StartInterval` (matching the
worker's default `--interval`). It does not use `KeepAlive` because
the worker exits after each pass and launchd respawns it on the
schedule.

## Why a worker, not pg_cron

The schema in `init_schema.sql` also includes commented
`select cron.schedule(...)` calls so operators who already have
pg_cron installed can enable the same cleanups inside the database.
The worker is the application-side alternative for systems without
pg_cron, and it has the additional benefit of writing metrics to
the local telemetry database.

Either is fine. Pick one. If you run both, you'll just see
duplicate work — the cleanup operations are idempotent (deleting
the same row twice has no effect).

## Signal handling

The worker installs handlers for `SIGINT` and `SIGTERM`. When a
signal is received, the worker sets an internal flag, finishes the
current pass, and exits cleanly. The `_sleep_with_shutdown_check`
helper polls the flag in 1-second slices during daemon-mode sleep
so launchd's `Stop` signal is honored within ~1 second rather than
after the full interval.

## Error handling

A database error during a pass:
- In `--once` mode: the worker exits with code 2 so a CI step
  surfaces the failure.
- In daemon mode: the worker logs the error, sleeps for
  `min(interval, 60)` seconds, and retries on the next tick.

A telemetry error (telemetry DB unreachable, schema mismatch,
etc.):
- Logged as a warning and ignored. The cleanup pass still completed;
  the only loss is the metric.

This is deliberate: a clean database is more important than a
complete telemetry trace, and a telemetry outage must never block
the cleanups that keep the database healthy.

## Tests

`tests/test_hygiene_worker.py` covers:
- Argparse parsing (default, `--once`, `--interval`, `--database-url`,
  invalid `--log-level`)
- URL resolution (arg > env > default; telemetry path expansion)
- Output formatting (with and without results)
- Signal-aware sleep (exits immediately when shutdown flag is set)
- Metric recording (one event per operation, correct fields)
- End-to-end subprocess smoke test (`--help`, `--once --dry-run`)

The actual SQL functions (`memory.run_hygiene_pass()` and its
constituents) are exercised by integration tests against a live
Postgres instance. The worker is tested up to the database boundary.

## Why this is in `scripts/` and not a skill

The hygiene worker is operational infrastructure, not a Claude
Code skill. Skills are user-invoked and run on the hot path. The
hygiene worker runs on a schedule in the background, has no
user-facing command surface, and exists to keep the database
healthy. Putting it in `scripts/` makes its role clear and lets it
be auto-started by launchd without registering with the skill
catalog.
