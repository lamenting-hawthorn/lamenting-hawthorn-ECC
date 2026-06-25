"""
synthesizer.py — LLM curation pass.

Takes the current typed_memory (as a :class:`ParsedStore`), the
recent runtime activity (from :mod:`collector`), and an optional
focus string, calls the LLM, and returns a structured proposal set.

The proposal shape is the diff between the current store and a
reorganized version. Each proposal is one of:

  - ``keep``              — entry is still correct, leave as-is.
  - ``merge``             — two or more entries should be one;
                            ``proposed_replacement`` is the new text.
  - ``supersede``         — this entry is wrong/stale; another entry
                            (``proposed_superseded_by_id``) is the
                            replacement; set ``superseded_by`` on
                            the loser.
  - ``archive``           — entry is no longer relevant; leave the
                            row but mark for archival review.
  - ``flag_for_review``   — human should look at this manually.

The LLM is asked to produce JSON; we tolerate markdown code fences
and minor whitespace. The response is never trusted directly —
:mod:`controller` validates every proposal against the live store
before writing back.

We default to the receiving repo's :class:`src.llm_client.LLMClient`
when it can be imported. That keeps model choice, base URL, and
auth consistent with the runtime that produced the memory in the
first place. The :func:`call_llm_compat` fallback is for environments
where ``src/`` is not on the path (e.g. running dream from a launchd
plist with a stripped PYTHONPATH).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from parser import MemoryEntry, ParsedStore


PROMPT_TEMPLATE = """You are a memory curator for a long-running AI agent. Your job is to reorganize the agent's typed memory store based on recent runtime activity.

## Current memory store ({mem_count} entries, {mem_chars} chars)

These are typed_memory rows the agent has previously committed. Each entry has a memory_type (``episodic`` / ``semantic`` / ``procedural``) and a category. ``superseded_by IS NULL`` rows are the live ones; the rest are already retired.

### semantic rows (facts, preferences, knowledge)
{semantic_entries}

### procedural rows (workflows, playbooks)
{procedural_entries}

### episodic rows (recent interactions)
{episodic_entries}

## Recent runtime activity ({sess_count} sessions, {sess_chars} chars)

Below are excerpts from recent event_store, retrieval_logs, and trace_events. Look for:
  - Topics the user raised repeatedly but aren't in memory yet
  - User preferences that emerged through corrections
  - Environment details / tool quirks discovered
  - Workflows the user has consistently used
  - Memory rows the agent keeps retrieving but that turn out to be unhelpful (candidates for archive)

{session_excerpts}

## Your task

Produce a REORGANIZED memory store. You may emit one proposal PER existing row, or leave a row out (which is the same as ``keep``). The possible actions are:

  1. ``keep`` — entry is still accurate, leave as-is.
  2. ``merge`` — this entry should be combined with one or more others into a tighter single entry. Provide ``proposed_replacement`` text.
  3. ``supersede`` — this entry is wrong or stale; another existing row (``proposed_superseded_by_id``) is the replacement.
  4. ``archive`` — entry is no longer relevant; mark for archival review.
  5. ``flag_for_review`` — human should look at this manually; do not auto-apply.

Constraints:
  - Be CONSERVATIVE. When in doubt, ``keep``. Loss of memory is worse than duplicates.
  - Do not propose to ``merge`` two rows whose ``memory_type`` or ``category`` differ.
  - ``proposed_superseded_by_id`` must reference an existing ``row_id`` from the current store.
  - ``proposed_replacement`` text must preserve the original meaning; do not reword the user's intent.
  - Do not add rows for topics only mentioned once.
  - Skip rows whose ``superseded_by`` is already non-null; they're already retired.

{user_instructions}

## Output format

Respond with ONLY valid JSON, no markdown, no preamble. The JSON shape:

{{
  "proposals": [
    {{
      "row_id": "<uuid of the existing row>",
      "action": "keep|merge|supersede|archive|flag_for_review",
      "proposed_replacement": "<text, only when action=merge>",
      "proposed_superseded_by_id": "<uuid of replacement row, only when action=supersede>",
      "confidence": <0.0-1.0, how confident are you in this proposal>,
      "rationale": "<one short sentence explaining why>"
    }},
    ...
  ],
  "summary": "1-2 sentence description of what changed"
}}
"""


@dataclass
class Proposal:
    row_id: str
    action: str  # keep|merge|supersede|archive|flag_for_review
    proposed_replacement: Optional[str] = None
    proposed_superseded_by_id: Optional[str] = None
    confidence: float = 0.5
    rationale: str = ""


@dataclass
class SynthesisResult:
    proposals: List[Proposal] = field(default_factory=list)
    summary: str = ""
    raw_response: str = ""
    prompt_chars: int = 0
    model: str = ""


def _format_entries(entries: List[MemoryEntry]) -> str:
    if not entries:
        return "  (none)"
    return "\n\n".join(
        f"[row_id={e.row_id} c={e.confidence:.2f}] {e.text}"
        for e in entries
    )


def build_prompt(
    store: ParsedStore,
    session_excerpts: str,
    sess_count: int,
    sess_chars: int,
    instructions: Optional[str] = None,
) -> str:
    """Build the LLM prompt. ``store`` is the live typed_memory read."""
    user_instr = ""
    if instructions and instructions.strip():
        user_instr = (
            "\n## User's specific guidance for this curation pass\n\n"
            f"{instructions.strip()}\n"
        )

    by_type: dict[str, list[MemoryEntry]] = {"semantic": [], "procedural": [], "episodic": []}
    for e in store.entries:
        by_type.setdefault(e.memory_type, []).append(e)

    return PROMPT_TEMPLATE.format(
        mem_count=store.entry_count,
        mem_chars=store.char_count,
        semantic_entries=_format_entries(by_type.get("semantic", [])),
        procedural_entries=_format_entries(by_type.get("procedural", [])),
        episodic_entries=_format_entries(by_type.get("episodic", [])),
        sess_count=sess_count,
        sess_chars=sess_chars,
        session_excerpts=session_excerpts or "(no sessions)",
        user_instructions=user_instr,
    )


def call_llm(prompt: str, *, model: Optional[str] = None,
              api_key: Optional[str] = None,
              max_tokens: int = 8000,
              temperature: float = 0.2) -> str:
    """
    Call the LLM. Tries (in order):

      1. ``src.llm_client.LLMClient`` — the runtime's own client.
      2. ``httpx`` direct POST to ``$LLM_BASE_URL/chat/completions``
         using ``$LLM_API_KEY`` and ``$LLM_MODEL``.

    Returns the assistant message text. Raises ``RuntimeError`` if no
    path can complete.
    """
    # 1. Try the runtime's own client first.
    try:
        # Make ``src/`` importable regardless of CWD.
        repo_root = _find_repo_root()
        if repo_root and str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from src.llm_client import LLMClient  # type: ignore[import-not-found]

        client = LLMClient(model=model) if model else LLMClient()
        return client.chat(
            user_message=prompt,
            system_prompt=(
                "You are a memory curator. Always respond with valid JSON only."
            ),
            retrieved_context="",
        )
    except Exception:
        pass  # fall through to direct httpx

    # 2. Direct httpx fallback.
    import httpx

    api_key = api_key or os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    model = model or os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    if not api_key:
        raise RuntimeError(
            "No API key found. Set LLM_API_KEY in the environment, "
            "or run from a directory where src/llm_client.py is importable."
        )

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a memory curator. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc


def _find_repo_root() -> "Path | None":
    """
    Walk up from this file looking for ``init_schema.sql`` (the
    signature file of the agent-architecture repo). Returns ``None``
    if not found.
    """
    from pathlib import Path
    here = Path(__file__).resolve().parent
    for _ in range(6):
        if (here / "init_schema.sql").exists():
            return here
        if here.parent == here:
            return None
        here = here.parent
    return None


def parse_response(response: str) -> SynthesisResult:
    """Parse the LLM's JSON into a SynthesisResult.

    Tolerates markdown code fences (drops the first and last line if
    either is a triple-backtick fence) and minor whitespace.
    """
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        debug_path = "/tmp/dream_parse_error.txt"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(response)
        raise RuntimeError(
            f"LLM returned invalid JSON: {exc}. Response saved to {debug_path}."
        ) from exc

    proposals: list[Proposal] = []
    for item in data.get("proposals", []):
        action = (item.get("action") or "").strip()
        if action not in ("keep", "merge", "supersede", "archive", "flag_for_review"):
            continue
        row_id = (item.get("row_id") or "").strip()
        if not row_id:
            continue
        proposals.append(
            Proposal(
                row_id=row_id,
                action=action,
                proposed_replacement=(item.get("proposed_replacement") or "").strip() or None,
                proposed_superseded_by_id=(item.get("proposed_superseded_by_id") or "").strip() or None,
                confidence=float(item.get("confidence") or 0.5),
                rationale=(item.get("rationale") or "").strip(),
            )
        )

    return SynthesisResult(
        proposals=proposals,
        summary=(data.get("summary") or "").strip(),
        raw_response=response,
    )


def synthesize(
    store: ParsedStore,
    session_excerpts: str,
    sess_count: int,
    sess_chars: int,
    *,
    instructions: Optional[str] = None,
    model: Optional[str] = None,
) -> SynthesisResult:
    """Run the full synthesis pass."""
    prompt = build_prompt(store, session_excerpts, sess_count, sess_chars, instructions)
    response = call_llm(prompt, model=model)
    result = parse_response(response)
    result.prompt_chars = len(prompt)
    result.model = model or os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    return result


if __name__ == "__main__":
    import sys
    from parser import parse_typed_memory

    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ACTOR_ID", "u_owner")
    store = parse_typed_memory(user_id=target)
    prompt = build_prompt(store, "(no sessions)", 0, 0)
    print(f"prompt built: {len(prompt)} chars (~{len(prompt)//4} tokens) for {store.entry_count} rows")
    print(prompt[:400] + "\n…")
