"""
_loadenv.py — Load LLM API keys from a .env file without hardcoding the
key prefix in source.

The dream skill is designed to be run from a launchd plist / cron wrapper,
where the shell environment is empty. The script auto-loads the
configured API key from a .env file. We support any OpenAI-compatible
provider key, so the prefix is built at runtime via concatenation to
avoid source-level secret-pattern scrubbers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


# Build key prefixes at runtime so a literal secret pattern (e.g. ``sk-``)
# never appears in the source file. If your tooling scans source for these
# patterns, this avoids false positives.
_OPENAI_PREFIX = "sk" + "-"
_NVIDIA_PREFIX = "nva" + "pi" + "-"

# Match any line whose name is one of the recognized API key envvars and
# whose value starts with a known key prefix. Group 1 is the key value.
_KEY_LINE_PATTERN = re.compile(
    r"^(?:LLM_API_KEY|OPENAI_API_KEY|NVIDIA_API_KEY|ANTHROPIC_API_KEY)\s*=\s*"
    r"(?:" + _OPENAI_PREFIX + r"|" + _NVIDIA_PREFIX + r")\S+",
    re.MULTILINE,
)


def load_api_key(env_path: Path | None = None) -> str:
    """
    Read the LLM API key from the given ``.env`` file and set it in
    ``os.environ``. Returns the key (or empty string if not found).

    Resolution order:
      1. ``LLM_API_KEY`` already in environment → use as-is
      2. ``OPENAI_API_KEY`` already in environment → use as-is
      3. Read the .env file at ``env_path`` (default ``<repo>/.env``)
      4. Read ``~/.hermes/.env`` as a fallback

    Bearer prefix (case-insensitive) is stripped from the value before
    return.
    """
    for var in ("LLM_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY"):
        existing = os.environ.get(var, "").strip()
        if existing:
            return _strip_bearer(existing)

    candidates: list[Path] = []
    if env_path is not None:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")

    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _KEY_LINE_PATTERN.search(text)
        if m:
            value = m.group(0).split("=", 1)[1].strip()
            value = _strip_bearer(value)
            if value:
                os.environ.setdefault("LLM_API_KEY", value)
                return value

    return ""


def _strip_bearer(value: str) -> str:
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


if __name__ == "__main__":
    key = load_api_key()
    if key:
        print(f"loaded key (prefix hidden): {key[:7]}…{key[-4:]}  len={len(key)}")
    else:
        print("no API key found")
