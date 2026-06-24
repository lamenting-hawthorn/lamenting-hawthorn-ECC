#!/usr/bin/env python3
"""Public repo smoke checks.

These checks avoid live services. They verify that the public repository shape,
core Python files, and public documentation are internally consistent.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def assert_contains(path: str, needle: str) -> None:
    content = (ROOT / path).read_text(encoding="utf-8")
    if needle not in content:
        raise AssertionError(f"{path} missing expected text: {needle}")


def assert_not_contains(path: str, pattern: str) -> None:
    content = (ROOT / path).read_text(encoding="utf-8")
    if re.search(pattern, content):
        raise AssertionError(f"{path} contains blocked pattern: {pattern}")


def main() -> None:
    for path in [
        "event_worker.py",
        "sync_wiki.py",
        "langgraph_deep_path.py",
        *[str(p.relative_to(ROOT)) for p in (ROOT / "src").glob("*.py")],
        *[str(p.relative_to(ROOT)) for p in (ROOT / "src" / "adapters").glob("*.py")],
        "examples/export_skillloop_trace.py",
    ]:
        source = (ROOT / path).read_text(encoding="utf-8")
        compile(source, str(ROOT / path), "exec")

    blocked_paths = [
        "client_handoff",
        "local_testing_workspace",
        "__pycache__",
        ".pytest_cache",
    ]
    for blocked in blocked_paths:
        if (ROOT / blocked).exists():
            raise AssertionError(f"public repo should not contain {blocked}")

    generated = list(ROOT.rglob("*.pyc")) + list(ROOT.rglob("*.tar.gz")) + list(ROOT.rglob(".DS_Store"))
    # Exclude generated directories/files that should not be committed
    skip_names = {".venv", ".ruff_cache", "__pycache__", ".pytest_cache", ".git", ".DS_Store"}
    generated = [p for p in generated if not any(part in skip_names for part in p.parts)]
    if generated:
        raise AssertionError(f"generated/private artifacts found: {generated[:5]}")

    assert_contains("README.md", "SkillLoop-compatible")
    assert_contains("ARCHITECTURE.md", "Missing actor identity is a runtime error")
    assert_contains("EMBEDDING_STRATEGY.md", "default embedding provider is local")
    assert_contains("docs/RELEASE_CHECKLIST.md", "No real tokens")
    assert_contains(".gitignore", "client_handoff/")
    assert_contains(".env.example", "EMBEDDING_PROVIDER=local")
    assert_contains("src/graph.py", "actor is required for runtime graph invocation")
    assert_contains("src/trace_export.py", "SkillLoop-compatible")
    assert_contains("src/adapters/base.py", "AGENT_ORG_ID is required")

    for path in ["README.md", "ARCHITECTURE.md", "EMBEDDING_STRATEGY.md", "docs/RELEASE_CHECKLIST.md"]:
        assert_not_contains(path, r"/Users/[A-Za-z0-9._-]+")
        assert_not_contains(path, r"CLIENT_HANDOFF|CLIENT_BUILD_GUIDE|START_HERE|instructions\.md")

    print("public smoke checks passed")


if __name__ == "__main__":
    main()
