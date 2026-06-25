"""
telemetry.cli — Command-line entry point for telemetry operations.

Provides a single ``report`` subcommand so the slash command can shell
out to python without dragging in a JS report implementation.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from telemetry import SqliteEventStore
from telemetry.reports import (
    TelemetryReport,
    build_report,
    render_text_report,
)

_LOGGER = logging.getLogger("ecc.telemetry.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m telemetry.cli",
        description="ECC telemetry CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="Render a telemetry report")
    report.add_argument(
        "--db",
        type=Path,
        default=Path("~/.ecc/telemetry.db").expanduser(),
        help="Path to the telemetry SQLite database",
    )
    report.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format",
    )
    report.add_argument(
        "--top",
        type=int,
        default=20,
        help="Top-N cap for the per-name table",
    )
    return parser


def _render_json(report: TelemetryReport) -> str:
    """Serialize the report as JSON.

    The ``kind`` enum is expanded to its string value so the
    output is friendly to non-Python consumers; everything else
    uses the dataclass field name directly.
    """
    payload = {
        "total_invocations": report.total_invocations,
        "total_successes": report.total_successes,
        "total_failures": report.total_failures,
        "success_rate": report.success_rate,
        "by_name": [
            {
                "name": stats.name,
                "kind": stats.kind.value,
                "invocations": stats.invocations,
                "successes": stats.successes,
                "failures": stats.failures,
                "avg_duration_ms": stats.avg_duration_ms,
                "total_tokens_in": stats.total_tokens_in,
                "total_tokens_out": stats.total_tokens_out,
                "last_error": stats.last_error,
            }
            for stats in report.by_name
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _cmd_report(args: argparse.Namespace) -> int:
    db_path: Path = args.db
    if not db_path.exists():
        print(
            f"Telemetry not set up: {db_path} not found. "
            "No hooks have recorded invocations yet.",
        )
        return 0
    store = SqliteEventStore(db_path)
    try:
        report = build_report(store.iter_all())
    finally:
        store.close()
    if args.format == "json":
        print(_render_json(report))
    else:
        print(render_text_report(report, top_n=args.top))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "report":
        return _cmd_report(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
