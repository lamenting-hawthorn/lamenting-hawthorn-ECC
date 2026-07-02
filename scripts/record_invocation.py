#!/usr/bin/env python3
"""record_invocation — Write one telemetry event to the local database.

Usage:
    record_invocation.py --name NAME --kind {skill,command,agent} \
                         --duration-ms N --success [0|1]

The script opens the default telemetry store (``~/.ecc/telemetry.db``,
overridable via ``ECC_TELEMETRY_DB``) and records a single event.
It is meant to be invoked from shell-based skill/command wrappers so
that hot paths do not need to embed Python logic.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telemetry import EventKind, TelemetryCollector, open_default_store


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record one telemetry event")
    parser.add_argument("--name", required=True, help="Skill/command/agent name")
    parser.add_argument(
        "--kind", required=True, choices=[k.value for k in EventKind],
        help="Event kind",
    )
    parser.add_argument(
        "--duration-ms", type=int, required=True,
        help="Wall duration in milliseconds",
    )
    parser.add_argument(
        "--success", type=int, required=True, choices=(0, 1),
        help="1 if the invocation succeeded, 0 otherwise",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = open_default_store()
    collector = TelemetryCollector(store=store)
    try:
        collector.record_invocation(
            name=args.name,
            kind=EventKind(args.kind),
            duration_ms=args.duration_ms,
            success=bool(args.success),
        )
        collector.flush()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
