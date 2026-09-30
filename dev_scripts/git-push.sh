#!/usr/bin/env bash
set -euo pipefail

# Resolve the real location of this script, including symbolic links.
SCRIPT="${BASH_SOURCE[0]}"

while [[ -L "$SCRIPT" ]]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT")" && pwd)"
  SCRIPT="$(readlink "$SCRIPT")"

  [[ "$SCRIPT" != /* ]] && SCRIPT="$SCRIPT_DIR/$SCRIPT"
done

SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ ! -d "$ROOT/.git" ]]; then
  echo "Error: $ROOT is not a Git repository." >&2
  exit 1
fi

cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "Usage: $(basename "$0") \"commit message\"" >&2
  exit 1
fi

MESSAGE="$*"

echo "Repository: $ROOT"
echo "Branch: $(git branch --show-current)"
echo

echo "Staging changes..."
git add -A

if git diff --cached --quiet; then
  echo "No new changes to commit."
else
  echo "Committing..."
  git commit -m "$MESSAGE"
fi

echo "Pushing main to origin..."
git push -u --force-with-lease origin main

echo "Done."
