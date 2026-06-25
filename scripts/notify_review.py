#!/usr/bin/env python3
"""Notify the owner about SkillLoop proposals waiting for review.

Reads the SkillLoop review queue and sends a Telegram message
if there are pending memory proposals or recent Postgres imports.
"""

import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Defaults use ``$HOME`` (so ``os.path.expanduser`` resolves them on every
# platform) and let users override individual paths via environment variables.
# The previous literal ``<HOME>`` and ``<TELEGRAM_USER_ID>`` strings were
# passed straight through ``expanduser``, so the bot-token lookup and the
# SkillLoop DB detection silently failed.
SKILLLOOP_DB = os.path.expanduser(
    os.environ.get(
        "SKILLLOOP_DB",
        "~/agent_architecture/.skillloop/skillloop.db",
    )
)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///agent_memory")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
ENV_PATH = os.path.expanduser(
    os.environ.get("HERMES_ENV_PATH", "~/.hermes/.env")
)


def _log(msg: str) -> None:
    """Write a timestamped line to stdout (used for cron / launchd log scraping)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _load_bot_token() -> str | None:
    """Read the Telegram bot token from the user's ``.hermes/.env`` file.

    Returns the token string, or ``None`` if the file is missing, unreadable,
    or does not define ``TELEGRAM_BOT_TOKEN``.
    """
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception as e:
        _log(f"Failed to read .env: {e}")
    return None


def _count_pending_proposals() -> int:
    """Return the number of memory-kind SkillLoop proposals still in
    ``pending`` status, or 0 if the SkillLoop DB is missing or unreadable."""
    if not os.path.exists(SKILLLOOP_DB):
        _log("skillloop db not ready")
        return 0
    try:
        conn = sqlite3.connect(SKILLLOOP_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = 'pending' AND kind = 'memory'"
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        _log(f"SQLite error: {e}")
        return 0


def _count_recent_imports() -> int:
    """Return the number of ``source = 'skillloop_proposal'`` rows inserted
    into ``memory.typed_memory`` in the last hour, or 0 if Postgres is
    unavailable or the query fails."""
    try:
        import psycopg
    except ImportError:
        _log("psycopg not available, skipping Postgres check")
        return 0

    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM memory.typed_memory
            WHERE source = 'skillloop_proposal'
              AND created_at > NOW() - INTERVAL '1 hour'
            """
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        _log(f"Postgres error: {e}")
        return 0


def _send_message(token: str, text: str) -> bool:
    """POST ``text`` to the configured Telegram chat via the bot API.

    Returns ``True`` if Telegram accepted the message (``ok`` in the
    response body), ``False`` for transport or API errors. Network failures
    are caught and logged so the caller can decide whether to fail the
    overall run.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": TELEGRAM_USER_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            if data.get("ok"):
                _log("Message sent successfully")
                return True
            _log(f"Telegram API error: {data}")
            return False
    except Exception as e:
        _log(f"Failed to send Telegram message: {e}")
        return False


def main() -> int:
    """Build the digest text and deliver it to Telegram.

    Exit codes:

    * ``0`` — message delivered successfully.
    * ``1`` — Telegram rejected the message or the request failed.
    * ``2`` — required configuration is missing (``TELEGRAM_BOT_TOKEN`` or
      ``TELEGRAM_USER_ID``); the run cannot deliver anything.
    """
    token = _load_bot_token()
    if not token:
        _log("TELEGRAM_BOT_TOKEN not found")
        # Surface a missing token to schedulers (cron / launchd) instead of
        # pretending the run was healthy. A broken notification path must
        # show up in monitor output, not in a silent 0 exit.
        return 2
    if not TELEGRAM_USER_ID:
        _log("TELEGRAM_USER_ID not configured")
        return 2

    pending = _count_pending_proposals()
    recent = _count_recent_imports()

    now = datetime.now(timezone.utc).strftime("%H:%M")

    if pending == 0 and recent == 0:
        text = f"SkillLoop status ({now}): all clear, nothing to review."
    else:
        lines = [f"SkillLoop status ({now}):"]
        if pending > 0:
            noun = "proposals" if pending != 1 else "proposal"
            lines.append(f"  • {pending} memory {noun} waiting for review")
        if recent > 0:
            noun = "imported" if recent != 1 else "imported"
            lines.append(f"  • {recent} {noun} to Postgres in the last hour")
        lines.append("")
        lines.append("Run: skillloop --path ~/agent_workspace review list")
        text = "\n".join(lines)

    if not _send_message(token, text):
        # Telegram delivery itself failed — propagate that to the scheduler.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
