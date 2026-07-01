"""
diff.py — Generate a human-readable diff report for a dream run.

The :func:`generate_diff_markdown` helper produces the same shape as
file-staged memory diff: a markdown summary of what the curator proposed,
grouped by action (keep / merge / supersede / archive / flag_for_review),
with the original row text, the proposed replacement (for merges), the
target row (for supersedes), and the model's confidence + rationale.

Unlike flat-file staging, this diff is generated against live Postgres rows
(in ``memory.dream_proposals`` joined to ``memory.typed_memory``), not
against a flat-file staging directory. The file is written to a path
the caller passes in (or to stdout if path is None).
"""

from __future__ import annotations

import os

from controller import list_proposals

DEFAULT_DATABASE_URL = "postgresql:///agent_memory"


def _connect(database_url: str | None = None):
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(url, row_factory=dict_row)


def _truncate(text: str | None, n: int = 200) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[:n] + "…"


def generate_diff_markdown(
    run_id: str,
    *,
    database_url: str | None = None,
) -> str:
    """
    Return a markdown report for the given dream run. Sections:

      1. Header (run metadata: when, model, summary)
      2. Per-action breakdown with row previews
      3. Counts summary
      4. Adopt / discard footer
    """
    with _connect(database_url) as conn:
        run = conn.execute(
            """
            select run_id, started_at, finished_at, model, status,
                   rows_scanned, proposals_count, adopted_count,
                   rejected_count, summary, instructions
            from memory.dream_runs
            where run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            return f"# Memory Dream — Diff\n\n**Run not found:** `{run_id}`\n"

    proposals = list_proposals(run_id, include_reviewed=True, database_url=database_url)

    lines: list[str] = ["# Memory Dream — Diff Report\n"]
    lines.append(f"**Run:** `{run['run_id']}`")
    lines.append(f"**Started:** {run['started_at']}")
    if run.get("finished_at"):
        lines.append(f"**Finished:** {run['finished_at']}")
    lines.append(f"**Status:** {run['status']}")
    if run.get("model"):
        lines.append(f"**Model:** {run['model']}")
    if run.get("rows_scanned"):
        lines.append(f"**Rows scanned:** {run['rows_scanned']}")
    if run.get("summary"):
        lines.append(f"\n**Summary:** {run['summary']}\n")
    else:
        lines.append("")

    # Group by action.
    by_action: dict[str, list[dict]] = {
        "merge": [], "supersede": [], "archive": [],
        "flag_for_review": [], "keep": [],
    }
    for p in proposals:
        by_action.setdefault(p["action"], []).append(p)

    # Render each section. "keep" is mostly noise; show it briefly.
    section_titles = {
        "merge": "Proposed merges",
        "supersede": "Proposed supersedes",
        "archive": "Proposed archives",
        "flag_for_review": "Flagged for human review",
        "keep": "Entries the LLM confirmed (keep)",
    }
    section_order = ["merge", "supersede", "archive", "flag_for_review", "keep"]

    for action in section_order:
        items = by_action[action]
        if not items:
            continue
        lines.append(f"\n## {section_titles[action]} ({len(items)})\n")
        for p in items:
            confidence = float(p.get("confidence") or 0.0)
            row_id = p.get("row_id", "")
            memory_type = p.get("memory_type", "?")
            category = p.get("category", "?")
            old_text = p.get("content", "")
            rationale = p.get("rationale") or ""
            reviewer = p.get("reviewer_action") or "pending"

            lines.append(
                f"### {action} — c={confidence:.2f}  "
                f"[{memory_type}/{category}]  (review: {reviewer})"
            )
            lines.append(f"- **row_id:** `{row_id}`")
            if old_text:
                lines.append(f"- **current text:** {_truncate(old_text)}")
            if action == "merge" and p.get("proposed_replacement"):
                lines.append(f"- **proposed text:** {_truncate(p['proposed_replacement'])}")
            if action == "supersede" and p.get("proposed_superseded_by_id"):
                winner = next(
                    (q for q in proposals if q.get("row_id") == p["proposed_superseded_by_id"]),
                    None,
                )
                if winner:
                    lines.append(
                        f"- **superseded by:** `{p['proposed_superseded_by_id']}` — "
                        f"{_truncate(winner.get('content', ''))}"
                    )
                else:
                    lines.append(f"- **superseded by:** `{p['proposed_superseded_by_id']}`")
            if rationale:
                lines.append(f"- **rationale:** {rationale}")
            lines.append("")

    # Counts summary.
    lines.append("\n## Counts\n")
    lines.append(
        f"- Total proposals: {len(proposals)}  "
        f"(kept: {len(by_action['keep'])}, "
        f"merge: {len(by_action['merge'])}, "
        f"supersede: {len(by_action['supersede'])}, "
        f"archive: {len(by_action['archive'])}, "
        f"flag: {len(by_action['flag_for_review'])})"
    )
    adopted = sum(1 for p in proposals if p.get("reviewer_action") == "adopted")
    rejected = sum(1 for p in proposals if p.get("reviewer_action") == "rejected")
    pending = sum(1 for p in proposals if not p.get("reviewer_action"))
    lines.append(
        f"- Reviewer state: adopted={adopted}, rejected={rejected}, pending={pending}"
    )

    # Footer.
    if pending > 0:
        lines.append("\n---\n")
        lines.append("**To adopt pending proposals:** `dream.py adopt <run_id>`")
        lines.append("**To reject pending proposals:** `dream.py discard <run_id>`")
    else:
        lines.append("\n---\n**All proposals reviewed. Run is final.**")

    return "\n".join(lines) + "\n"


def write_diff(
    run_id: str,
    path: str,
    *,
    database_url: str | None = None,
) -> int:
    """Write the diff markdown to ``path``. Returns the byte count."""
    md = generate_diff_markdown(run_id, database_url=database_url)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return len(md.encode("utf-8"))


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        last = __import__("controller").latest_run()
        if not last:
            print("no dream runs yet")
            sys.exit(0)
        target = last["run_id"]
    print(generate_diff_markdown(target))
