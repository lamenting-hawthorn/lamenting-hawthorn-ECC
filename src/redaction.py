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
    payload: Any
    reverse_mapping: Dict[str, str] = field(default_factory=dict)


class _Pseudonymizer:
    def __init__(self) -> None:
        self.forward: Dict[str, str] = {}
        self.reverse: Dict[str, str] = {}
        self.counters: Dict[str, int] = {}

    def replace(self, value: str, prefix: str) -> str:
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
    pseudonymizer = _Pseudonymizer()
    sanitized = _sanitize_value(payload, pseudonymizer)
    return PseudonymizedPayload(payload=sanitized, reverse_mapping=pseudonymizer.reverse)


def _sanitize_value(value: Any, pseudonymizer: _Pseudonymizer) -> Any:
    if isinstance(value, str):
        return _sanitize_string(value, pseudonymizer)
    if isinstance(value, list):
        return [_sanitize_value(item, pseudonymizer) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item, pseudonymizer) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_value(key, pseudonymizer) if isinstance(key, str) else key: _sanitize_value(item, pseudonymizer)
            for key, item in value.items()
        }
    return value


def _sanitize_string(value: str, pseudonymizer: _Pseudonymizer) -> str:
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
    letters = []
    value = index
    while value:
        value -= 1
        letters.append(chr(ord("A") + (value % 26)))
        value //= 26
    return "".join(reversed(letters)) or "A"
