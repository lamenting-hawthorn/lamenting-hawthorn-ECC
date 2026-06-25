---
description: Generate a skill/command invocation report from the ECC telemetry database.
argument-hint: [json] [--top N] [--db PATH]
---

# Telemetry Report

Aggregate the local telemetry database (`~/.ecc/telemetry.db` by default)
into a per-name report showing invocation counts, failure rates, average
duration, and token usage. Useful for finding dead-weight skills, slow
commands, and skills that are failing often.

The data comes from `src/telemetry/TelemetryCollector` (Python) which is
written to by future hooks and skill dispatchers. The report itself is
rendered by `src/telemetry/reports.render_text_report` so the on-disk
format and the displayed format stay in sync.

## Where the data lives

`~/.ecc/telemetry.db` is a SQLite database with one table,
`telemetry_events`. Each row is one skill/command/agent invocation:

| column          | type      | meaning                                        |
|-----------------|-----------|------------------------------------------------|
| `name`          | TEXT      | skill/command/agent name                       |
| `kind`          | TEXT      | one of `skill`, `command`, `agent`             |
| `started_at`    | REAL      | unix timestamp                                 |
| `duration_ms`   | INTEGER   | wall time                                      |
| `success`       | INTEGER   | 1 = ok, 0 = failed                             |
| `actor_id`      | TEXT      | optional, who triggered it                     |
| `error_type`    | TEXT      | optional, exception class name                 |
| `error_message` | TEXT      | optional, truncated to 8 KiB                   |
| `tokens_in`     | INTEGER   | optional, sum of input tokens                  |
| `tokens_out`    | INTEGER   | optional, sum of output tokens                 |

Override the DB path with `ECC_TELEMETRY_DB=/custom/path.db /telemetry-report`.

## What this command does

1. Verify the DB file exists. If it does not, tell the user the
   collector has not been wired into any hook yet.
2. Read every event in insertion order.
3. Aggregate by `(kind, name)`: invocations, failures, average
   duration, total tokens.
4. Print a fixed-width text report with two tables: top-N by
   invocation count, then a Top-failing section.

`node` is used so this works identically on macOS, Linux, and Windows
without a python-on-PATH dependency at the call site. Python is invoked
subprocess-style to do the actual aggregation.

## Report (default)

```bash
node -e '
const { spawnSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const db = process.env.ECC_TELEMETRY_DB
  || path.join(process.env.HOME || process.env.USERPROFILE, ".ecc", "telemetry.db");
if (!fs.existsSync(db)) { console.log("Telemetry not set up: " + db + " not found. No hooks have recorded invocations yet."); process.exit(0); }
const args = process.argv.slice(2);
const format = args.includes("json") ? "json" : "text";
const topIdx = args.indexOf("--top");
const top = topIdx >= 0 ? parseInt(args[topIdx + 1], 10) || 20 : 20;
const py = spawnSync("python3", [
  "-m", "telemetry.cli", "report",
  "--db", db, "--format", format, "--top", String(top),
], { encoding: "utf8" });
if (py.status !== 0) { console.error(py.stderr || "telemetry cli failed"); process.exit(py.status || 1); }
process.stdout.write(py.stdout);
'
```

## JSON output (`/telemetry-report json`)

Same command but with `json` as the first positional argument. Emits a
single JSON object with `total_invocations`, `total_successes`,
`total_failures`, and `by_name: [InvocationStats, ...]` so other
tools can pipe it into a dashboard.

## CLI

The report is also available as a standalone CLI for scripting:

```bash
python3 -m telemetry.cli report --db ~/.ecc/telemetry.db --format text --top 20
python3 -m telemetry.cli report --db ~/.ecc/telemetry.db --format json
```

## Report format

1. Header: total invocations, successes, failures, success rate.
2. Top N by invocations: kind, name, calls, fails, avg_ms, tok_in,
   tok_out — fixed-width so it is easy to skim in a terminal.
3. Top failing (only if any failures exist): the failing names with
   their most recent error message, truncated to 60 characters.

## Why this exists

The catalog has 273 skills. Without this report, you have no way to
know which are invoked, which are slow, which are silently broken, or
which can be retired. This is the data plane for that decision.
