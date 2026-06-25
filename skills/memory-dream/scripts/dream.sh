#!/usr/bin/env bash
# dream.sh — wrapper for the memory-dream skill.
#
# Convenience entrypoint used by the launchd plist and by hand.
# Reads DATABASE_URL and ACTOR_ID from the environment, falling
# back to the same defaults the plist uses.
#
# Usage:
#   ./dream.sh run
#   ./dream.sh run --focus "Conservative: only merge exact duplicates."
#   ./dream.sh status
#   ./dream.sh adopt <run_id>
#   ./dream.sh discard <run_id>
#
# Or set environment overrides:
#   DATABASE_URL=postgresql:///foo ACTOR_ID=u_owner ./dream.sh run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

export DATABASE_URL="${DATABASE_URL:-postgresql:///agent_memory}"
export ACTOR_ID="${ACTOR_ID:-u_owner}"
export PYTHONUNBUFFERED=1

# If a .env file exists at the repo root, parse it. We deliberately
# avoid `source` (which executes arbitrary shell) and instead read
# KEY=VALUE lines whose key matches a strict identifier pattern.
# Only LLM_API_KEY-style variables are exported; comments, blank
# lines, and non-matching lines are silently ignored.
if [[ -f "$REPO_ROOT/.env" ]]; then
    while IFS='=' read -r key value; do
        # Skip blank lines and comments.
        [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
        # Only allow known LLM env vars, identified by name only.
        case "$key" in
            LLM_API_KEY|OPENAI_API_KEY|NVIDIA_API_KEY|ANTHROPIC_API_KEY|LLM_BASE_URL|LLM_MODEL)
                # Strip optional surrounding single or double quotes.
                value="${value%\"}"
                value="${value#\"}"
                value="${value%\'}"
                value="${value#\'}"
                export "$key=$value"
                ;;
        esac
    done < "$REPO_ROOT/.env"
fi

# Add the repo root to PYTHONPATH so the dream scripts can import
# the runtime's src/llm_client.py (and any other src/ modules they
# need). This makes the launchd-plist invocation self-contained
# without requiring the user to venv-activate the repo first.
export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" "$SCRIPT_DIR/dream.py" "$@"
