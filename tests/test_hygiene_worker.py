"""Tests for the memory-hygiene-worker script.

Covers argparse, output formatting, signal-aware sleep, and the
record-pass-metrics path that integrates with the telemetry SQLite
store. The actual database call (memory.run_hygiene_pass) is not
exercised here because it requires a live Postgres connection; the
worker is tested up to the boundary and the SQL functions are
verified by the existing init_schema.sql diff.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SRC_DIR = REPO_ROOT / "src"

# Make the worker importable as a module. We do this by inserting
# both dirs onto sys.path so the script's own sys.path manipulation
# (which appends src/) is not required for import.
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))


def _import_worker():
    """Import the worker fresh.

    Uses importlib to avoid the Python 3.13 dataclass / __future__
    annotations edge case when exec_module runs outside __main__.
    """
    import importlib
    import importlib.util
    if "hygiene_worker" in sys.modules:
        del sys.modules["hygiene_worker"]
    spec = importlib.util.spec_from_file_location(
        "hygiene_worker", str(SCRIPTS_DIR / "hygiene-worker.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hygiene_worker"] = module
    spec.loader.exec_module(module)
    return module


class TestArgparse(unittest.TestCase):
    def test_default_args(self):
        mod = _import_worker()
        args = mod._parse_args([])
        self.assertFalse(args.once)
        self.assertEqual(args.interval, mod.DEFAULT_INTERVAL_SECONDS)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.database_url)
        self.assertEqual(args.telemetry_db, mod.DEFAULT_TELEMETRY_DB)
        self.assertEqual(args.log_level, "INFO")

    def test_once_and_dry_run(self):
        mod = _import_worker()
        args = mod._parse_args(["--once", "--dry-run"])
        self.assertTrue(args.once)
        self.assertTrue(args.dry_run)

    def test_custom_interval(self):
        mod = _import_worker()
        args = mod._parse_args(["--interval", "1800"])
        self.assertEqual(args.interval, 1800)

    def test_database_url_override(self):
        mod = _import_worker()
        args = mod._parse_args(["--database-url", "postgresql:///x"])
        self.assertEqual(args.database_url, "postgresql:///x")

    def test_log_level_choices(self):
        mod = _import_worker()
        with self.assertRaises(SystemExit):
            mod._parse_args(["--log-level", "NOPE"])


class TestResolveUrls(unittest.TestCase):
    def test_resolve_database_url_from_args(self):
        mod = _import_worker()
        args = mod._parse_args(["--database-url", "postgresql:///a"])
        self.assertEqual(mod._resolve_database_url(args), "postgresql:///a")

    def test_resolve_database_url_from_env(self, monkeypatch=None):
        mod = _import_worker()
        args = mod._parse_args([])
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "postgresql:///env"
        try:
            self.assertEqual(mod._resolve_database_url(args), "postgresql:///env")
        finally:
            if old is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old

    def test_resolve_database_url_default(self):
        mod = _import_worker()
        args = mod._parse_args([])
        old = os.environ.pop("DATABASE_URL", None)
        try:
            self.assertEqual(mod._resolve_database_url(args),
                             mod.DEFAULT_DATABASE_URL)
        finally:
            if old is not None:
                os.environ["DATABASE_URL"] = old

    def test_resolve_telemetry_path_expands_user(self):
        mod = _import_worker()
        args = mod._parse_args(["--telemetry-db", "~/my.db"])
        path = mod._resolve_telemetry_path(args)
        self.assertEqual(str(path), str(Path("~/my.db").expanduser()))


class TestFormatPassSummary(unittest.TestCase):
    def test_with_results(self):
        mod = _import_worker()
        results = [
            mod.OperationResult(operation="orphan_edges", deleted_count=5),
            mod.OperationResult(operation="expired_memory", deleted_count=3),
        ]
        text = mod._format_pass_summary(results, 1.23)
        self.assertIn("1.23s", text)
        self.assertIn("orphan_edges=5", text)
        self.assertIn("expired_memory=3", text)

    def test_with_no_results(self):
        mod = _import_worker()
        text = mod._format_pass_summary([], 0.5)
        self.assertIn("no work", text)


class TestSleepWithShutdown(unittest.TestCase):
    def test_exits_early_on_shutdown(self):
        mod = _import_worker()
        # Force the shutdown flag on, then sleep should return immediately.
        mod._shutdown_requested = True
        t0 = time.monotonic()
        mod._sleep_with_shutdown_check(60)  # would normally take 60s
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        mod._shutdown_requested = False  # reset

    def test_runs_full_duration_when_no_shutdown(self):
        mod = _import_worker()
        mod._shutdown_requested = False
        t0 = time.monotonic()
        mod._sleep_with_shutdown_check(2)  # 2 second sleep
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 3.0)


class TestRecordPassMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "telemetry.db"

    def test_records_one_event_per_operation(self):
        mod = _import_worker()
        results = [
            mod.OperationResult(operation="orphan_edges", deleted_count=5),
            mod.OperationResult(operation="expired_memory", deleted_count=3),
        ]
        mod._record_pass_metrics(self._path, results, duration_ms=200)
        # Verify by reading the events back through the same store.
        from telemetry import SqliteEventStore
        store = SqliteEventStore(self._path)
        try:
            events = list(store.iter_all())
        finally:
            store.close()
        self.assertEqual(len(events), 2)
        names = {e.name for e in events}
        self.assertEqual(names, {"cleanup_orphan_edges", "cleanup_expired_memory"})
        for event in events:
            self.assertEqual(event.kind.value, "agent")
            self.assertEqual(event.actor_id, "hygiene-worker")

    def test_no_results_does_nothing(self):
        mod = _import_worker()
        mod._record_pass_metrics(self._path, [], duration_ms=10)
        # File should not be created when there's nothing to record.
        self.assertFalse(self._path.exists())

    def test_records_all_six_operations(self):
        # After the subagent review (deleg_6dc17f25) we expanded the
        # pass to cover audit_log, pending_approvals, and
        # old_dream_runs. The worker must record all six.
        mod = _import_worker()
        results = [
            mod.OperationResult(operation="retrieval_logs", deleted_count=12),
            mod.OperationResult(operation="expired_memory", deleted_count=0),
            mod.OperationResult(operation="orphan_edges", deleted_count=4),
            mod.OperationResult(operation="audit_log", deleted_count=50),
            mod.OperationResult(operation="pending_approvals", deleted_count=2),
            mod.OperationResult(operation="old_dream_runs", deleted_count=1),
        ]
        mod._record_pass_metrics(self._path, results, duration_ms=300)
        from telemetry import SqliteEventStore
        store = SqliteEventStore(self._path)
        try:
            events = list(store.iter_all())
        finally:
            store.close()
        self.assertEqual(len(events), 6)
        names = {e.name for e in events}
        self.assertEqual(names, {
            "cleanup_retrieval_logs",
            "cleanup_expired_memory",
            "cleanup_orphan_edges",
            "cleanup_audit_log",
            "cleanup_pending_approvals",
            "cleanup_old_dream_runs",
        })


class TestEndToEndDryRun(unittest.TestCase):
    """Smoke test: invoke the script as a subprocess to verify
    --help, --once --dry-run, and default args."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "hygiene-worker.py"), *args],
            cwd=REPO_ROOT, capture_output=True, text=True,
            env={**os.environ, "DATABASE_URL": ""},  # force fallback path
        )

    def test_help_exits_zero(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ECC memory hygiene worker", r.stdout)

    def test_once_dry_run_completes(self):
        r = self._run("--once", "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertIn("hygiene pass", r.stderr)
        self.assertIn("no work", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
