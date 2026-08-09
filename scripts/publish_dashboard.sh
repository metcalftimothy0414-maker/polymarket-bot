#!/usr/bin/env bash
# Regenerates the dashboard JSON snapshot and publishes it (+ the static
# page) to the gh-pages branch via a dedicated worktree at .worktrees/gh-pages,
# so the main branch's history stays clean of data-refresh commits.
#
# Usage:
#   scripts/publish_dashboard.sh --once     # single publish cycle, exit
#   scripts/publish_dashboard.sh            # loop forever, INTERVAL_SECONDS apart
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE="$REPO_ROOT/.worktrees/gh-pages"
INTERVAL_SECONDS="${DASHBOARD_PUBLISH_INTERVAL_SECONDS:-90}"

cd "$REPO_ROOT"

if [ ! -d "$WORKTREE" ]; then
  echo "error: $WORKTREE does not exist. Set it up once with:" >&2
  echo "  git worktree add -b gh-pages .worktrees/gh-pages" >&2
  echo "  (or --orphan if gh-pages doesn't exist yet on the remote)" >&2
  exit 1
fi

publish_once() {
  .venv/bin/python -m bot.cli dashboard-export --out dashboard-site/data.json
  cp dashboard-site/index.html dashboard-site/data.json "$WORKTREE/"

  ( cd "$WORKTREE"
    git add -A
    if git diff --cached --quiet; then
      echo "$(date -u +%FT%TZ) no change, skipping commit"
      return 0
    fi
    git commit -q -m "dashboard snapshot $(date -u +%FT%TZ)"
    git push -q origin gh-pages
    echo "$(date -u +%FT%TZ) published"
  )
}

if [ "${1:-}" = "--once" ]; then
  publish_once
  exit 0
fi

echo "Publishing dashboard every ${INTERVAL_SECONDS}s. Ctrl-C to stop."
while true; do
  publish_once || echo "$(date -u +%FT%TZ) publish cycle failed, will retry next interval" >&2
  sleep "$INTERVAL_SECONDS"
done
