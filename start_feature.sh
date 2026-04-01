#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(basename "$REPO_ROOT")" = "CodexKit" ]; then
  exec "$REPO_ROOT/install/start_feature.sh" --repo-root "$REPO_ROOT" "$@"
fi

if [ -x "$REPO_ROOT/../CodexKit/install/start_feature.sh" ]; then
  exec "$REPO_ROOT/../CodexKit/install/start_feature.sh" --repo-root "$REPO_ROOT" "$@"
fi

echo "CodexKit install/start_feature.sh not found relative to ${REPO_ROOT}" >&2
exit 1
