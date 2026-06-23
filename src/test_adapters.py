#!/usr/bin/env python3
"""Phase 7 test: Messaging adapters (WhatsApp + Telegram)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.adapters.base import ActorIdentity, BaseAdapter, IncomingMessage
from src.adapters.whatsapp import WhatsAppAdapter, _normalize_jid
from src.adapters.telegram import TelegramAdapter


def test_normalize_jid() -> None:
    assert _normalize_jid("15550000001@s.whatsapp.net") == "15550000001"
    assert _normalize_jid("15550000001") == "15550000001"
    assert _normalize_jid("abc") == "abc"
    print("OK: JID normalization")


def test_whatsapp_resolve_actor() -> None:
    # Must set env before importing since module reads at import time
    os.environ["OWNER_WHATSAPP_PHONES"] = "15550000001"
    os.environ["AGENT_ORG_ID"] = "test_org"
    # Force re-import by clearing cache
    if "src.adapters.whatsapp" in sys.modules:
        del sys.modules["src.adapters.whatsapp"]
    from src.adapters.whatsapp import WhatsAppAdapter, _normalize_jid
    adapter = WhatsAppAdapter()
    owner_msg = IncomingMessage(
        channel="whatsapp",
        sender_id="15550000001@s.whatsapp.net",
        sender_name="Owner",
        text="hello",
        thread_id="wa-owner-thread",
    )
    actor = adapter.resolve_actor(owner_msg)
    assert actor.role == "owner"
    assert actor.actor_id == "wa:15550000001"
    assert actor.org_id == "test_org"

    team_msg = IncomingMessage(
        channel="whatsapp",
        sender_id="15550000002@s.whatsapp.net",
        sender_name="Team",
        text="hello",
        thread_id="wa-team-thread",
    )
    actor2 = adapter.resolve_actor(team_msg)
    assert actor2.role == "team"
    print("OK: WhatsApp actor resolution")


def test_telegram_resolve_actor() -> None:
    os.environ["OWNER_TELEGRAM_IDS"] = "123456789"
    os.environ["AGENT_ORG_ID"] = "test_org"
    if "src.adapters.telegram" in sys.modules:
        del sys.modules["src.adapters.telegram"]
    from src.adapters.telegram import TelegramAdapter
    adapter = TelegramAdapter()
    owner_msg = IncomingMessage(
        channel="telegram",
        sender_id="123456789",
        sender_name="Owner",
        text="hello",
        thread_id="tg-owner-thread",
    )
    actor = adapter.resolve_actor(owner_msg)
    assert actor.role == "owner"
    assert actor.actor_id == "tg:123456789"
    assert actor.org_id == "test_org"

    team_msg = IncomingMessage(
        channel="telegram",
        sender_id="987654321",
        sender_name="Team",
        text="hello",
        thread_id="tg-team-thread",
    )
    actor2 = adapter.resolve_actor(team_msg)
    assert actor2.role == "team"
    print("OK: Telegram actor resolution")


def test_store_event() -> None:
    os.environ["AGENT_ORG_ID"] = "test_org"
    adapter = WhatsAppAdapter()
    msg = IncomingMessage(
        channel="whatsapp",
        sender_id="15550000001@s.whatsapp.net",
        sender_name="Test",
        text="Remember this: test event storage.",
        thread_id="test-event-thread",
    )
    event_id = adapter.store_event(msg)
    assert event_id is None or isinstance(event_id, str)
    print(f"OK: event storage is non-blocking (event_id={event_id})")


def test_end_to_end() -> None:
    os.environ["OWNER_WHATSAPP_PHONES"] = "15550000001"
    os.environ["AGENT_ORG_ID"] = "test_org"
    os.environ["MEMORY_BACKEND"] = "fake"
    os.environ["CHECKPOINTER"] = "memory"
    adapter = WhatsAppAdapter()
    msg = IncomingMessage(
        channel="whatsapp",
        sender_id="15550000001@s.whatsapp.net",
        sender_name="Owner",
        text="Remember this: Mo Memory Phase 7 end-to-end test.",
        thread_id="test-phase7-thread",
    )
    result = adapter.handle(msg)
    assert "assistant_response" in result
    print(f"OK: end-to-end response: {result['assistant_response'][:80]}...")
    print(f"   memory_writes: {len(result['memory_writes'])}")
    print(f"   written_memory_ids: {len(result['written_memory_ids'])}")
    assert result["written_memory_ids"] == []


def main() -> None:
    print("=== Phase 7: Adapter Test ===\n")

    test_normalize_jid()
    print()

    test_whatsapp_resolve_actor()
    print()

    test_telegram_resolve_actor()
    print()

    test_store_event()
    print()

    test_end_to_end()
    print()

    print("=== Phase 7: PASSED ===")


if __name__ == "__main__":
    main()
