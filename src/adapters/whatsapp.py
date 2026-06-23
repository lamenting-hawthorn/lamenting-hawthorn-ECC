"""WhatsApp adapter for Mo Memory.

Normalizes WhatsApp JID-style sender IDs to actor identities.

Usage:
    from src.adapters.whatsapp import WhatsAppAdapter
    adapter = WhatsAppAdapter()
    result = adapter.handle(incoming_message)
"""

from __future__ import annotations

import os
import re
from typing import Any

try:
    from .base import ActorIdentity, BaseAdapter, IncomingMessage, resolve_runtime_org_id
except ImportError:
    from base import ActorIdentity, BaseAdapter, IncomingMessage, resolve_runtime_org_id

# Owner phone numbers (JID base) — configure via env or DB lookup
OWNER_PHONES_ENV = os.environ.get("OWNER_WHATSAPP_PHONES", "")
OWNER_PHONES = set(p.strip() for p in OWNER_PHONES_ENV.split(",") if p.strip())


def _normalize_jid(jid: str) -> str:
    """Extract bare phone from JID like '15550000001@s.whatsapp.net'."""
    m = re.match(r"(\d+)(?:@s\.whatsapp\.net)?", jid)
    return m.group(1) if m else jid


class WhatsAppAdapter(BaseAdapter):
    """WhatsApp channel adapter."""

    def resolve_actor(self, msg: IncomingMessage) -> ActorIdentity:
        phone = _normalize_jid(msg.sender_id)
        role = "owner" if phone in OWNER_PHONES else "team"
        return ActorIdentity(
            actor_id=f"wa:{phone}",
            org_id=resolve_runtime_org_id(),
            role=role,
        )

    def handle(self, msg: IncomingMessage) -> dict[str, Any]:
        """Override to add WhatsApp-specific metadata."""
        if msg.metadata is None:
            object.__setattr__(msg, "metadata", {})
        msg.metadata["channel"] = "whatsapp"
        msg.metadata["phone_normalized"] = _normalize_jid(msg.sender_id)
        return super().handle(msg)
