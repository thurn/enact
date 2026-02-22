#!/usr/bin/env bash
# Opens the META.md from the most recent Enact session
# in a pager, wrapped at 80 characters.

set -euo pipefail

ENACT_DIR="$HOME/.enact"

if [ ! -d "$ENACT_DIR" ]; then
  echo "error: no enact directory found at $ENACT_DIR" >&2
  exit 1
fi

# Find the newest META.md by sorting session dirs
# numerically (they are Unix timestamps)
latest=""
for dir in "$ENACT_DIR"/*/; do
  meta="${dir}META.md"
  if [ -f "$meta" ]; then
    latest="$meta"
  fi
done

if [ -z "$latest" ]; then
  echo "error: no META.md found in any session" \
    "under $ENACT_DIR" >&2
  exit 1
fi

echo "Reading: $latest"
fold -s -w 80 "$latest" | "${PAGER:-less}"
