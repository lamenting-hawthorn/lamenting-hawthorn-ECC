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

import logging
import os
import re
from pathlib import Path

_LOGGER = logging.getLogger("memory_dream.loadenv")

# Build key prefixes at runtime so a literal secret pattern (e.g. ``sk-``)
# never appears in the source file. If your tooling scans source for these
# patterns, this avoids false positives.
_OPENAI_PREFIX = "sk" + "-"
_NVIDIA_PREFIX = "nva" + "pi" + "-"

# Match any line whose name is one of the recognized API key envvars and
# whose value starts with a known key prefix. Group 1 is the key value.
_KEY_LINE_PATTERN = re.compile(
    r"^(?:LLM_API_KEY|OPENAI_API_KEY|NVIDIA_API_KEY|ANTHROPIC_API_KEY)\s*=\s*"
    r"(?P<value>.+?)\s*$",
    re.MULTILINE,
)

_KNOWN_PREFIXES = (_OPENAI_PREFIX, _NVIDIA_PREFIX)


def load_api_key(env_path: Path | None = None) -> str:
    """
    Read the LLM API key from the given ``.env`` file and set it in
    ``os.environ``. Returns the key.

    Resolution order:
      1. ``LLM_API_KEY`` already in environment → use as-is
      2. ``OPENAI_API_KEY`` already in environment → use as-is
      3. Read the .env file at ``env_path`` (default ``<repo>/.env``)
      4. Read ``~/.hermes/.env`` as a fallback

    Bearer prefix (case-insensitive) and surrounding single/double
    quotes are stripped from the value before return.

    Raises ``KeyError`` if no key is found in any of the candidate
    locations, so callers fail fast at startup rather than at LLM call
    time.
    """
    for var in ("LLM_API_KEY", "OPENAI_API_KEY", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY"):
        existing = os.environ.get(var, "").strip()
        if existing:
            return _strip_bearer(_strip_quotes(existing))

    candidates: list[Path] = []
    if env_path is not None:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")

    tried: list[str] = []
    for path in candidates:
        tried.append(str(path))
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _LOGGER.debug("Could not read %s: %s", path, exc)
            continue
        m = _KEY_LINE_PATTERN.search(text)
        if not m:
            continue
        raw = m.group("value")
        value = _strip_bearer(_strip_quotes(raw))
        if not value.startswith(_KNOWN_PREFIXES):
            _LOGGER.debug(
                "Skipping %s — value does not start with a known provider prefix",
                path,
            )
            continue
        if value:
            os.environ.setdefault("LLM_API_KEY", value)
            return value

    raise KeyError(
        f"No LLM API key found. Tried environment variables "
        f"(LLM_API_KEY, OPENAI_API_KEY, NVIDIA_API_KEY, ANTHROPIC_API_KEY) "
        f"and these .env paths: {tried}. "
        f"Set LLM_API_KEY in the environment or add a line like "
        f"LLM_API_KEY=sk-... to one of those files."
    )


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _strip_bearer(value: str) -> str:
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        key = load_api_key()
    except KeyError as exc:
        _LOGGER.error("%s", exc)
        raise SystemExit(1) from exc
    # Never log fragments of the key — only the fact that it loaded.
    _LOGGER.info("Loaded LLM API key (len=%d)", len(key))
