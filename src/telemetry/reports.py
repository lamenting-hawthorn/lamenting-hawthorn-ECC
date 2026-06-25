"""
telemetry.reports — Read-side aggregation over telemetry events.

Decoupled from the writer (``telemetry.TelemetryCollector``) so the
hot path stays minimal and the report layer can evolve independently.
Reports are pure functions over an ``Iterable[Event]`` — they never
touch the database directly, which keeps them easy to unit-test.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from telemetry import Event, EventKind


@dataclass(frozen=True)
class InvocationStats:
    """Per-name aggregated metrics for one skill/command/agent.

    Field names are short for compact report output. All counts are
    integers; durations are integer milliseconds.
    """

    name: str
    kind: EventKind
    invocations: int
    successes: int
    failures: int
    avg_duration_ms: int
    total_tokens_in: int
    total_tokens_out: int
    last_error: str | None = None


@dataclass(frozen=True)
class TelemetryReport:
    """Aggregate of all events, ready for human display."""

    total_invocations: int
    total_successes: int
    total_failures: int
    by_name: list[InvocationStats]

    @property
    def success_rate(self) -> float:
        if self.total_invocations == 0:
            return 0.0
        return self.total_successes / self.total_invocations


def aggregate_by_name(events: Iterable[Event]) -> list[InvocationStats]:
    """Aggregate events into one ``InvocationStats`` per (kind, name).

    Sorted by invocations descending so the report's first table
    shows the most-used skills/commands first. Failures and tokens are
    summed; average duration is the mean across the events for that
    name.
    """
    grouped: dict[tuple[EventKind, str], _MutableStats] = {}

    for event in events:
        key = (event.kind, event.name)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = _MutableStats(name=event.name, kind=event.kind)
            grouped[key] = bucket
        bucket.record(event)

    stats = [
        bucket.freeze()
        for bucket in sorted(
            grouped.values(),
            key=lambda b: (-b.invocations, b.kind.value, b.name),
        )
    ]
    return stats


def build_report(events: Iterable[Event]) -> TelemetryReport:
    """Build the top-level report from a stream of events."""
    materialized = list(events)
    total = len(materialized)
    successes = sum(1 for e in materialized if e.success)
    return TelemetryReport(
        total_invocations=total,
        total_successes=successes,
        total_failures=total - successes,
        by_name=aggregate_by_name(materialized),
    )


def render_text_report(report: TelemetryReport, *, top_n: int = 20) -> str:
    """Format a report as a human-readable text block.

    The report is intentionally fixed-width so it is easy to skim and
    easy to assert in tests. ``top_n`` caps the per-name table; the
    totals header is always shown.
    """
    lines: list[str] = []
    lines.append("ECC Telemetry Report")
    lines.append("=" * 60)
    lines.append(
        f"Total invocations: {report.total_invocations}  "
        f"successes: {report.total_successes}  "
        f"failures: {report.total_failures}  "
        f"success_rate: {report.success_rate:.1%}"
    )
    lines.append("")
    lines.append(f"Top {top_n} by invocation count:")
    lines.append(
        f"  {'kind':<10} {'name':<40} {'calls':>6} {'fails':>6} "
        f"{'avg_ms':>7} {'tok_in':>9} {'tok_out':>9}"
    )
    for stats in report.by_name[:top_n]:
        truncated = _truncate(stats.name, 40)
        lines.append(
            f"  {stats.kind.value:<10} {truncated:<40} "
            f"{stats.invocations:>6} {stats.failures:>6} "
            f"{stats.avg_duration_ms:>7} "
            f"{stats.total_tokens_in:>9} {stats.total_tokens_out:>9}"
        )
    failed = [s for s in report.by_name if s.failures > 0]
    if failed:
        lines.append("")
        lines.append("Top failing:")
        for stats in failed[:top_n]:
            truncated = _truncate(stats.name, 40)
            last = _truncate(stats.last_error or "", 60)
            lines.append(
                f"  {stats.kind.value:<10} {truncated:<40} "
                f"failures={stats.failures:>3}  last_error: {last}"
            )
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


class _MutableStats:
    """Mutable accumulator; promoted to a frozen ``InvocationStats`` on freeze.

    Lives in a small private class so the public ``InvocationStats``
    can be a true value object (frozen, hashable) while the aggregation
    loop avoids the cost of constructing a new frozen object per
    event.
    """

    __slots__ = (
        "name",
        "kind",
        "invocations",
        "successes",
        "failures",
        "_duration_total",
        "total_tokens_in",
        "total_tokens_out",
        "last_error",
    )

    def __init__(self, *, name: str, kind: EventKind) -> None:
        self.name = name
        self.kind = kind
        self.invocations = 0
        self.successes = 0
        self.failures = 0
        self._duration_total = 0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.last_error: str | None = None

    def record(self, event: Event) -> None:
        self.invocations += 1
        if event.success:
            self.successes += 1
        else:
            self.failures += 1
            if event.error_message:
                self.last_error = event.error_message
        self._duration_total += max(0, event.duration_ms)
        if event.tokens_in is not None:
            self.total_tokens_in += event.tokens_in
        if event.tokens_out is not None:
            self.total_tokens_out += event.tokens_out

    def freeze(self) -> InvocationStats:
        avg = (
            self._duration_total // self.invocations
            if self.invocations > 0
            else 0
        )
        return InvocationStats(
            name=self.name,
            kind=self.kind,
            invocations=self.invocations,
            successes=self.successes,
            failures=self.failures,
            avg_duration_ms=avg,
            total_tokens_in=self.total_tokens_in,
            total_tokens_out=self.total_tokens_out,
            last_error=self.last_error,
        )


__all__ = (
    "InvocationStats",
    "TelemetryReport",
    "aggregate_by_name",
    "build_report",
    "render_text_report",
)
