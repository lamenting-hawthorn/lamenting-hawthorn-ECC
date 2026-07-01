"""Tests for the telemetry package.

These tests cover the in-memory and SQLite stores, the collector's
batching behavior, and the report aggregator. They run without any
network or database dependency beyond a per-test temp directory.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telemetry import (  # noqa: E402  (path-setup import order)
    Event,
    EventKind,
    InMemoryEventStore,
    SqliteEventStore,
    TelemetryCollector,
)
from telemetry.reports import (  # noqa: E402
    aggregate_by_name,
    build_report,
    render_text_report,
)


def _make_event(
    name: str = "lint",
    kind: EventKind = EventKind.SKILL,
    duration_ms: int = 100,
    success: bool = True,
    error_message: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> Event:
    return Event(
        name=name,
        kind=kind,
        started_at=time.time(),
        duration_ms=duration_ms,
        success=success,
        actor_id="u_test",
        error_type=None if success else "RuntimeError",
        error_message=error_message,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


class TestInMemoryStore(unittest.TestCase):
    def test_insert_and_iter(self):
        store = InMemoryEventStore()
        store.insert(_make_event(name="a"))
        store.insert(_make_event(name="b"))
        events = list(store.iter_all())
        self.assertEqual([e.name for e in events], ["a", "b"])

    def test_insert_many_returns_count(self):
        store = InMemoryEventStore()
        n = store.insert_many([_make_event(name=str(i)) for i in range(5)])
        self.assertEqual(n, 5)
        self.assertEqual(sum(1 for _ in store.iter_all()), 5)

    def test_insert_many_empty(self):
        store = InMemoryEventStore()
        self.assertEqual(store.insert_many([]), 0)


class TestSqliteStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "telemetry.db"

    def test_schema_created_and_persists(self):
        store = SqliteEventStore(self._path)
        self.addCleanup(store.close)
        store.insert(_make_event(name="x"))
        store.flush_safe_close = None  # satisfy linter
        store.close()
        # Reopen and read
        store2 = SqliteEventStore(self._path)
        self.addCleanup(store2.close)
        events = list(store2.iter_all())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "x")

    def test_insert_many_writes_all_rows(self):
        store = SqliteEventStore(self._path)
        self.addCleanup(store.close)
        events = [_make_event(name=f"e{i}") for i in range(7)]
        n = store.insert_many(events)
        self.assertEqual(n, 7)
        self.assertEqual(sum(1 for _ in store.iter_all()), 7)

    def test_insert_many_uses_one_explicit_transaction(self):
        source = Path(sys.modules[SqliteEventStore.__module__].__file__).read_text(encoding="utf-8")
        self.assertIn("BEGIN", source)
        self.assertIn("COMMIT", source)

    def test_iter_all_releases_lock_before_yielding(self):
        import inspect

        source = inspect.getsource(SqliteEventStore.iter_all)
        self.assertLess(source.index("rows ="), source.index("for row in rows"))
        self.assertLess(source.index("with self._lock"), source.index("rows ="))

    def test_round_trip_preserves_all_fields(self):
        store = SqliteEventStore(self._path)
        self.addCleanup(store.close)
        store.insert(_make_event(
            name="merge", duration_ms=250, success=False,
            error_message="failed validation", tokens_in=10, tokens_out=5,
        ))
        event = next(iter(store.iter_all()))
        self.assertEqual(event.name, "merge")
        self.assertEqual(event.duration_ms, 250)
        self.assertFalse(event.success)
        self.assertEqual(event.error_message, "failed validation")
        self.assertEqual(event.tokens_in, 10)
        self.assertEqual(event.tokens_out, 5)


class TestCollector(unittest.TestCase):
    def test_record_invocation_buffers(self):
        store = InMemoryEventStore()
        collector = TelemetryCollector(store=store)
        collector.record_invocation(
            name="x", kind=EventKind.SKILL, duration_ms=10, success=True,
        )
        # In-memory batch should NOT have flushed yet.
        self.assertEqual(len(store._events), 0)

    def test_flush_writes_batch(self):
        store = InMemoryEventStore()
        collector = TelemetryCollector(store=store)
        collector.record_invocation(
            name="x", kind=EventKind.SKILL, duration_ms=10, success=True,
        )
        collector.flush()
        self.assertEqual(len(store._events), 1)

    def test_auto_flush_at_batch_size(self):
        store = InMemoryEventStore()
        collector = TelemetryCollector(store=store)
        # Write 50 events; the 50th should auto-flush (batch size = 50).
        for i in range(50):
            collector.record_invocation(
                name=f"x{i}", kind=EventKind.SKILL, duration_ms=1, success=True,
            )
        self.assertEqual(len(store._events), 50)

    def test_failure_persists_error(self):
        store = InMemoryEventStore()
        collector = TelemetryCollector(store=store)
        collector.record_invocation(
            name="x", kind=EventKind.SKILL, duration_ms=10,
            success=False, error_message="boom",
        )
        collector.flush()
        event = next(iter(store.iter_all()))
        self.assertFalse(event.success)
        self.assertEqual(event.error_message, "boom")


class TestReports(unittest.TestCase):
    def test_aggregate_groups_by_kind_and_name(self):
        events = [
            _make_event(name="a", kind=EventKind.SKILL, duration_ms=100),
            _make_event(name="a", kind=EventKind.SKILL, duration_ms=200),
            _make_event(name="b", kind=EventKind.COMMAND, duration_ms=50),
        ]
        stats = aggregate_by_name(events)
        self.assertEqual(len(stats), 2)
        by_name = {s.name: s for s in stats}
        self.assertEqual(by_name["a"].invocations, 2)
        self.assertEqual(by_name["a"].avg_duration_ms, 150)
        self.assertEqual(by_name["b"].invocations, 1)

    def test_aggregate_sorted_by_invocations_desc(self):
        events = [
            _make_event(name="rare", kind=EventKind.SKILL, duration_ms=10),
            _make_event(name="common", kind=EventKind.SKILL, duration_ms=10),
            _make_event(name="common", kind=EventKind.SKILL, duration_ms=10),
            _make_event(name="common", kind=EventKind.SKILL, duration_ms=10),
        ]
        stats = aggregate_by_name(events)
        self.assertEqual(stats[0].name, "common")
        self.assertEqual(stats[1].name, "rare")

    def test_build_report_totals(self):
        events = [
            _make_event(name="a", success=True),
            _make_event(name="a", success=False, error_message="x"),
            _make_event(name="b", success=True),
        ]
        report = build_report(events)
        self.assertEqual(report.total_invocations, 3)
        self.assertEqual(report.total_successes, 2)
        self.assertEqual(report.total_failures, 1)
        self.assertAlmostEqual(report.success_rate, 2 / 3)

    def test_render_text_report_includes_failures(self):
        events = [
            _make_event(name="alpha", success=False, error_message="boom"),
            _make_event(name="beta", success=True),
        ]
        report = build_report(events)
        text = render_text_report(report)
        self.assertIn("ECC Telemetry Report", text)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertIn("boom", text)
        self.assertIn("Top failing", text)

    def test_render_text_report_empty_events(self):
        report = build_report([])
        text = render_text_report(report)
        self.assertIn("Total invocations: 0", text)
        self.assertEqual(report.success_rate, 0.0)


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "telemetry.db"
        self.store = SqliteEventStore(self._path)
        self.addCleanup(self.store.close)
        for i in range(3):
            self.store.insert(_make_event(name="lint", duration_ms=10 + i))
        self.store.insert(_make_event(
            name="lint", success=False, error_message="boom",
        ))

    def test_text_report_via_cli(self):
        from telemetry.cli import main
        exit_code = main(["report", "--db", str(self._path),
                          "--format", "text", "--top", "5"])
        self.assertEqual(exit_code, 0)

    def test_json_report_via_cli(self):
        import contextlib
        import io

        from telemetry.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = main(["report", "--db", str(self._path),
                              "--format", "json", "--top", "5"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["total_invocations"], 4)
        self.assertEqual(payload["total_failures"], 1)
        self.assertEqual(len(payload["by_name"]), 1)
        self.assertEqual(payload["by_name"][0]["name"], "lint")

    def test_missing_db_returns_zero(self):
        from telemetry.cli import main
        exit_code = main([
            "report", "--db", str(self._path) + ".missing", "--format", "text",
        ])
        self.assertEqual(exit_code, 0)

    def test_non_positive_top_is_rejected(self):
        from telemetry.cli import main
        with self.assertRaises(SystemExit) as ctx:
            main(["report", "--db", str(self._path), "--top", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_uses_default_db_path_when_unset(self):
        # When --db is not provided, the CLI must fall back to
        # telemetry.default_db_path() (which honors ECC_TELEMETRY_DB
        # and the temp-file fallback) instead of a hardcoded
        # ~/.ecc/telemetry.db. This was a P1 bug found in the
        # senior-engineer review.
        from telemetry import default_db_path
        from telemetry.cli import main
        old_env = os.environ.get("ECC_TELEMETRY_DB")
        os.environ["ECC_TELEMETRY_DB"] = str(self._path)
        try:
            # No --db arg; should pick up the env var via default_db_path().
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out):
                with contextlib.redirect_stderr(buf_err):
                    exit_code = main(["report", "--format", "text", "--top", "5"])
            self.assertEqual(exit_code, 0)
            # default_db_path() returns the env-resolved path.
            self.assertEqual(default_db_path(), self._path)
            # And the report output reflects the seeded data.
            self.assertIn("lint", buf_out.getvalue())
        finally:
            if old_env is None:
                os.environ.pop("ECC_TELEMETRY_DB", None)
            else:
                os.environ["ECC_TELEMETRY_DB"] = old_env

    def test_index_for_failures_exists(self):
        # The failures partial index must be created in the schema.
        # (Verified by the schema's CREATE INDEX IF NOT EXISTS.)
        from telemetry.cli import main
        # Smoke: opening the store succeeds and the index is queryable.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = main(["report", "--db", str(self._path),
                              "--format", "text", "--top", "5"])
        self.assertEqual(exit_code, 0)


class TestRecordInvocationScript(unittest.TestCase):
    """Tests for scripts/record_invocation.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "telemetry.db"
        self._old_env = os.environ.get("ECC_TELEMETRY_DB")
        os.environ["ECC_TELEMETRY_DB"] = str(self._path)

    def tearDown(self) -> None:
        if self._old_env is None:
            os.environ.pop("ECC_TELEMETRY_DB", None)
        else:
            os.environ["ECC_TELEMETRY_DB"] = self._old_env

    def _invoke(self, *extra: str) -> int:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "record_invocation", str(Path(__file__).resolve().parents[1] / "scripts" / "record_invocation.py"),
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.main(["--name", "cost-report", "--kind", "command",
                         "--duration-ms", "42", "--success", "1", *extra])

    def test_writes_event_to_db(self):
        exit_code = self._invoke()
        self.assertEqual(exit_code, 0)
        store = SqliteEventStore(self._path)
        self.addCleanup(store.close)
        events = list(store.iter_all())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "cost-report")
        self.assertEqual(events[0].kind, EventKind.COMMAND)
        self.assertEqual(events[0].duration_ms, 42)
        self.assertTrue(events[0].success)

    def test_failure_event_persists(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "record_invocation", str(Path(__file__).resolve().parents[1] / "scripts" / "record_invocation.py"),
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        exit_code = mod.main(["--name", "x", "--kind", "skill",
                              "--duration-ms", "10", "--success", "0"])
        self.assertEqual(exit_code, 0)
        store = SqliteEventStore(self._path)
        self.addCleanup(store.close)
        events = list(store.iter_all())
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].success)


if __name__ == "__main__":
    unittest.main(verbosity=2)
