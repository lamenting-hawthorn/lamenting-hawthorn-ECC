#!/usr/bin/env bash
# install-hygiene-worker.sh — Install the ECC hygiene worker launchd plist.
#
# Substitutes <HOME> and <PYTHON_PATH> placeholders in the repo plist,
# writes the result to ~/Library/LaunchAgents/com.ecc.hygiene.plist,
# and optionally loads it via launchctl.
#
# Usage:
#   bash scripts/install-hygiene-worker.sh [--load]
#
# The script discovers PYTHON_PATH from ``which python3`` and HOME from
# ``$HOME``. Pass --load to invoke ``launchctl load`` automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_PLIST="${REPO_ROOT}/docs/launchd/com.ecc.hygiene.plist"
DEST_PLIST="${HOME}/Library/LaunchAgents/com.ecc.hygiene.plist"

PYTHON_PATH="$(command -v python3 || true)"
if [[ -z "${PYTHON_PATH}" ]]; then
    echo "ERROR: python3 not found on PATH." >&2
    exit 1
fi

if [[ ! -f "${SRC_PLIST}" ]]; then
    echo "ERROR: source plist not found: ${SRC_PLIST}" >&2
    exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents"

sed -e "s|<HOME>|${HOME}|g" \
    -e "s|<PYTHON_PATH>|${PYTHON_PATH}|g" \
    "${SRC_PLIST}" > "${DEST_PLIST}"

echo "Wrote ${DEST_PLIST}"

if [[ "${1:-}" == "--load" ]]; then
    if launchctl list com.ecc.hygiene &>/dev/null; then
        launchctl unload "${DEST_PLIST}" 2>/dev/null || true
    fi
    launchctl load "${DEST_PLIST}"
    echo "Loaded com.ecc.hygiene via launchctl."
else
    echo "To load the agent now, run:"
    echo "  launchctl load ${DEST_PLIST}"
fi
