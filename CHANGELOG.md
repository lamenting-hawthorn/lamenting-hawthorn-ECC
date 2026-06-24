# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## 2026-06-25 — SkillLoop integration

- Add SkillLoop hourly controller (`scripts/connect_skillloop.py`) — ingests approved proposals into Postgres typed_memory.
- Add vault bridge (`scripts/bridge_vault_and_sessions.py`) — imports Obsidian markdown and Hermes SQLite sessions into typed_memory.
- Add SkillLoop connector with idempotency guards and audit logging.
- Add Telegram notifier (`scripts/notify_review.py`) — hourly digest of pending proposals and recent imports.
- Add PII redaction module (`src/redaction.py`) — agendex-style pseudonymizer for emails, phones, URLs, and tokens.
- Add event_worker bug fixes (datetime type handling, poison-pill cooldown).
- Add 6 design docs in `.hermes/` (4 public + 2 internal).
- Add Mermaid architecture diagrams to README.
- Add `install.sh` for one-command setup.
- Add `ARCHITECTURE.md` high-level overview.
- Add `TESTING.md` test runner guide.

## 2026-06-22 — Runtime tracing and adapters

- Add trace event export (`src/trace_export.py`) with redaction and hashing.
- Add WhatsApp and Telegram adapters (`src/adapters/`).
- Add hybrid retrieval (`src/hybrid_retrieval.py`) — FTS + pgvector + RRF.
- Add graph memory expansion (`src/graph_memory.py`).
- Add durable memory write-behind (`src/durable_memory.py`).
- Add LangGraph checkpoint support (`src/checkpoints.py`).

## 2026-05-21 — Initial scaffold

- Postgres schema with typed_memory, event_store, retrieval_logs, audit_log.
- LangGraph 5-node workflow: resolve → retrieve → call_model → salience → write_memory.
- Local embeddings via sentence-transformers (all-MiniLM-L6-v2).
- Salience gate and memory classification heuristics.
- Row-level security schema design.
