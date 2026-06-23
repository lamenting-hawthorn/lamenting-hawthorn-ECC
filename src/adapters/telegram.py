"""Telegram adapter for Mo Memory.

Normalizes Telegram user IDs to actor identities.

Usage:
    from src.adapters.telegram import TelegramAdapter
    adapter = TelegramAdapter()
    result = adapter.handle(incoming_message)
"""

from __future__ import annotations

import os
from typing import Any

try:
    from .base import ActorIdentity, BaseAdapter, IncomingMessage, resolve_runtime_org_id
except ImportError:
    from base import ActorIdentity, BaseAdapter, IncomingMessage, resolve_runtime_org_id

# Owner Telegram user IDs — configure via env var OWNER_TELEGRAM_IDS (comma-separated)
OWNER_TELEGRAM_IDS_ENV = os.environ.get("OWNER_TELEGRAM_IDS", "")
OWNER_TELEGRAM_IDS = {uid.strip() for uid in OWNER_TELEGRAM_IDS_ENV.split(",") if uid.strip()}


class TelegramAdapter(BaseAdapter):
    """Telegram channel adapter."""

    def resolve_actor(self, msg: IncomingMessage) -> ActorIdentity:
        tg_id = str(msg.sender_id)
        role = "owner" if tg_id in OWNER_TELEGRAM_IDS else "team"
        return ActorIdentity(
            actor_id=f"tg:{tg_id}",
            org_id=resolve_runtime_org_id(),
            role=role,
        )

    def handle(self, msg: IncomingMessage) -> dict[str, Any]:
        """Override to add Telegram-specific metadata."""
        if msg.metadata is None:
            object.__setattr__(msg, "metadata", {})
        msg.metadata["channel"] = "telegram"
        msg.metadata["telegram_user_id"] = str(msg.sender_id)
        return super().handle(msg)
