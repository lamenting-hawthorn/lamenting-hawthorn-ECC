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
function resolveEccRoot(){
  const home = process.env.HOME || process.env.USERPROFILE;
  const base = path.join(home, ".claude");
  const marker = path.join("src", "telemetry", "cli.py");
  const candidates = [
    process.env.CLAUDE_PLUGIN_ROOT,
    process.env.ECC_ROOT,
    base,
    path.join(base, "plugins", "ecc"),
    path.join(base, "plugins", "ecc@ecc"),
    path.join(base, "plugins", "everything-claude-code"),
    path.join(base, "plugins", "everything-claude-code@everything-claude-code"),
    path.join(base, "plugins", "marketplaces", "ecc"),
    path.join(base, "plugins", "marketplaces", "everything-claude-code"),
  ].filter(Boolean);
  for (const c of candidates) if (fs.existsSync(path.join(c, marker))) return c;
  for (const name of ["ecc", "everything-claude-code"]) {
    const cache = path.join(base, "plugins", "cache", name);
    try {
      for (const a of fs.readdirSync(cache, { withFileTypes: true })) {
        if (!a.isDirectory()) continue;
        for (const b of fs.readdirSync(path.join(cache, a.name), { withFileTypes: true })) {
          if (!b.isDirectory()) continue;
          const c = path.join(cache, a.name, b.name);
          if (fs.existsSync(path.join(c, marker))) return c;
        }
      }
    } catch {}
  }
  return base;
}
const db = process.env.ECC_TELEMETRY_DB
  || path.join(process.env.HOME || process.env.USERPROFILE, ".ecc", "telemetry.db");
const args = process.argv.slice(1).filter(arg => arg !== "--");
const format = args.includes("json") ? "json" : "text";
const dbIdx = args.indexOf("--db");
const resolvedDb = dbIdx >= 0 && args[dbIdx + 1] ? args[dbIdx + 1] : db;
if (!fs.existsSync(resolvedDb)) { console.log("Telemetry not set up: " + resolvedDb + " not found. No hooks have recorded invocations yet."); process.exit(0); }
const topIdx = args.indexOf("--top");
let top = 20;
if (topIdx >= 0) {
  top = Number.parseInt(args[topIdx + 1], 10);
  if (!Number.isInteger(top) || top <= 0) {
    console.error("--top must be a positive integer");
    process.exit(1);
  }
}
const srcPath = path.join(resolveEccRoot(), "src");
const pyEnv = {
  ...process.env,
  PYTHONPATH: [srcPath, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
};
function choosePython(env) {
  const probes = [
    { command: "python3", prefix: [] },
    { command: "python", prefix: [] },
    { command: "py", prefix: ["-3"] },
  ];
  for (const probe of probes) {
    const result = spawnSync(probe.command, [
      ...probe.prefix, "-c", "import telemetry.cli",
    ], { encoding: "utf8", env });
    if (result.status === 0) return probe;
  }
  return null;
}
const launcher = choosePython(pyEnv);
if (!launcher) { console.error("telemetry cli failed: no Python launcher could import telemetry.cli"); process.exit(1); }
const py = spawnSync(launcher.command, [
  ...launcher.prefix,
  "-m", "telemetry.cli", "report",
  "--db", resolvedDb, "--format", format, "--top", String(top),
], { encoding: "utf8", env: pyEnv });
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

The catalog has 279 skills. Without this report, you have no way to
know which are invoked, which are slow, which are silently broken, or
which can be retired. This is the data plane for that decision.
