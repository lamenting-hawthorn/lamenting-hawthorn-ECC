"""Redaction / pseudonymization for memory write path.

Adapted from agendex risk_core/redaction.py — stripped of Langfuse-specific
trace types so it works on plain dict/list/str payloads.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE_RE = re.compile(
    r"(?<![\w:.-])(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})(?![\w:.-])"
)
ACCOUNT_RE = re.compile(r"\b(?:acct|account|customer|cust|client|user)[_-]?[A-Z0-9]*\d[A-Z0-9]*\b", re.IGNORECASE)
LONG_ID_RE = re.compile(r"\b[A-F0-9]{12,}\b", re.IGNORECASE)
PERSON_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b")


@dataclass
class PseudonymizedPayload:
    """Result of running :func:`pseudonymize_payload`.

    ``payload`` is the redacted copy of the input, structurally identical
    (mapping, list, scalar) but with sensitive values replaced by stable
    placeholders (``EMAIL_1``, ``PHONE_2``). ``reverse_mapping`` lets callers
    that hold the original values resolve placeholders back to the source
    text — keep it private and never persist it.
    """

    payload: Any
    reverse_mapping: Dict[str, str] = field(default_factory=dict)


class _Pseudonymizer:
    """Incremental per-prefix counter that produces stable placeholders.

    The same input value always maps to the same placeholder within a
    single ``pseudonymize_payload`` call, so re-reading the redacted text
    doesn't churn placeholder IDs.
    """

    def __init__(self) -> None:
        """Initialize empty forward / reverse maps and per-prefix counters."""
        self.forward: Dict[str, str] = {}
        self.reverse: Dict[str, str] = {}
        self.counters: Dict[str, int] = {}

    def replace(self, value: str, prefix: str) -> str:
        """Return the placeholder for ``value`` (creating one on first sight).

        ``ENTITY`` uses letter suffixes (``ENTITY_A``, ``ENTITY_B``) for
        readability; every other prefix is numbered (``PHONE_1``,
        ``EMAIL_2``). The reverse map is updated whenever a new placeholder
        is minted so callers can rehydrate the original text.
        """
        if value in self.forward:
            return self.forward[value]
        count = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = count
        if prefix == "ENTITY":
            placeholder = f"ENTITY_{_letters(count)}"
        else:
            placeholder = f"{prefix}_{count}"
        self.forward[value] = placeholder
        self.reverse[placeholder] = value
        return placeholder


def pseudonymize_payload(payload: Any) -> PseudonymizedPayload:
    """Recursively redact sensitive values in ``payload``.

    Strings are scanned for emails, IP addresses, phone numbers, account
    IDs, long hex identifiers, and person-name patterns; dict keys are
    preserved (rewriting them would break callers' field lookups). Returns
    a :class:`PseudonymizedPayload` containing the redacted tree and the
    placeholder→original reverse map for rehydration.
    """
    pseudonymizer = _Pseudonymizer()
    sanitized = _sanitize_value(payload, pseudonymizer)
    return PseudonymizedPayload(payload=sanitized, reverse_mapping=pseudonymizer.reverse)


def _sanitize_value(value: Any, pseudonymizer: _Pseudonymizer) -> Any:
    """Recursively walk ``value`` and apply :func:`_sanitize_string` to strings.

    Lists, tuples, and dicts are traversed; dict keys are kept untouched so
    downstream code that looks up ``payload["text"]`` etc. keeps working.
    """
    if isinstance(value, str):
        return _sanitize_string(value, pseudonymizer)
    if isinstance(value, list):
        return [_sanitize_value(item, pseudonymizer) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item, pseudonymizer) for item in value)
    if isinstance(value, dict):
        # Keep dict keys untouched. Rewriting keys changes the payload shape
        # callers downstream depend on (e.g. ``payload['text']``), which is a
        # data-contract break even when the values are sanitized correctly.
        return {
            key: _sanitize_value(item, pseudonymizer)
            for key, item in value.items()
        }
    return value


def _sanitize_string(value: str, pseudonymizer: _Pseudonymizer) -> str:
    """Run every redaction regex over ``value`` and return the rewritten text.

    Order matters: account/ID patterns are intentionally run before the
    person-name pattern so we don't strip the digits out of an account ID
    by accident.
    """
    text = value
    for regex, prefix in (
        (EMAIL_RE, "EMAIL"),
        (IP_RE, "IP"),
        (PHONE_RE, "PHONE"),
        (ACCOUNT_RE, "ENTITY"),
        (LONG_ID_RE, "ID"),
        (PERSON_NAME_RE, "NAME"),
    ):
        text = regex.sub(lambda match: pseudonymizer.replace(match.group(0), prefix), text)
    return text


def _letters(index: int) -> str:
    """Convert a 1-based counter to spreadsheet-style letters (``1→A``)."""
    letters = []
    value = index
    while value:
        value -= 1
        letters.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(letters)) or "A"
