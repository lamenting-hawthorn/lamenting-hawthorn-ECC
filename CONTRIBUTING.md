# Contributing

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Default local verification:

```bash
python -B -m pytest -p no:cacheprovider src -q
python -B smoke_test.py
```

## Contribution Rules

- Keep actor identity explicit in runtime code. Do not add demo defaults in
  production paths.
- Preserve the SkillLoop boundary as read-only trace export. Do not add direct
  write-back from governance into runtime memory.
- Prefer local embeddings by default. If you add API embedding support, keep
  provider selection explicit and avoid mixed vector spaces.
- Keep public documentation aligned with actual runtime behavior and tests.
- Add or update focused tests for behavior changes, especially around actor/org
  scoping and retrieval permissions.

## Pull Requests

- Keep changes scoped and reviewable.
- Include validation commands in the PR description.
- Call out any environment assumptions, skipped tests, or database requirements.
