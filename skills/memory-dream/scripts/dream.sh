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

# If a .env file exists at the repo root, source it. We look for
# LLM_API_KEY specifically (the only key the dream pipeline needs
# at runtime; the rest of the runtime's config comes from the
# launchd plist's EnvironmentVariables).
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
fi

# Add the repo root to PYTHONPATH so the dream scripts can import
# the runtime's src/llm_client.py (and any other src/ modules they
# need). This makes the launchd-plist invocation self-contained
# without requiring the user to venv-activate the repo first.
export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"

PYTHON_BIN="${PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" "$SCRIPT_DIR/dream.py" "$@"
