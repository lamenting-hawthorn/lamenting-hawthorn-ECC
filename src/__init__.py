"""src package — background TTL cleanup for expired memory rows.

Starts a daemon thread on import that periodically deletes rows from
memory.typed_memory whose expires_at timestamp has passed.
"""

from __future__ import annotations

import logging
import os
import threading
import time

_logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql:///agent_memory"
DEFAULT_CLEANUP_INTERVAL_SECONDS: float = 10.0

try:
    import psycopg

    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False


class TtlCleaner:
    """Background daemon thread that periodically deletes expired memory rows.

    The thread wakes every ``interval_seconds``, connects to Postgres, and
    runs ``DELETE FROM memory.typed_memory WHERE expires_at < NOW()``.

    Usage::

        cleaner = TtlCleaner()
        cleaner.start()
        # ... application runs ...
        cleaner.stop()   # optional — daemon threads die with the process
    """

    def __init__(
        self,
        database_url: str | None = None,
        interval_seconds: float | None = None,
    ):
        self._database_url = database_url or os.environ.get(
            "DATABASE_URL", DEFAULT_DATABASE_URL
        )
        self._interval = (
            interval_seconds
            if interval_seconds is not None
            else DEFAULT_CLEANUP_INTERVAL_SECONDS
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background cleanup thread (idempotent)."""
        if not _HAS_PSYCOPG:
            _logger.warning("TTL cleanup skipped — psycopg not installed")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ttl-cleanup"
        )
        self._thread.start()
        _logger.info(
            "TTL cleanup thread started (interval=%.1fs)", self._interval
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to stop and wait for it to finish."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            _logger.info("TTL cleanup thread stopped")

    def cleanup_once(self) -> int:
        """Perform a single cleanup pass.  Returns the number of deleted rows.

        Can be called directly in tests / scripts to force an immediate
        sweep without waiting for the timer.
        """
        if not _HAS_PSYCOPG:
            return 0
        return self._cleanup_once()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._cleanup_once()
            self._stop_event.wait(self._interval)

    def _cleanup_once(self) -> int:
        try:
            with psycopg.connect(self._database_url) as conn:
                conn.execute(
                    "SELECT set_config('app.current_role', 'service', false)"
                )
                result = conn.execute(
                    "DELETE FROM memory.typed_memory "
                    "WHERE expires_at IS NOT NULL AND expires_at < NOW()"
                )
                deleted = result.rowcount
                if deleted:
                    _logger.info(
                        "TTL cleanup: deleted %d expired memory row(s)", deleted
                    )
                return deleted
        except Exception:
            # Database might not be available yet; silently retry later
            return 0


# ------------------------------------------------------------------
# Global singleton — auto-started when the ``src`` package is imported.
# ------------------------------------------------------------------

_cleaner = TtlCleaner()
_cleaner.start()
