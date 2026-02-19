#!/usr/bin/env bash
# Creates symlinks from ~/.claude into the enact project.
# Safe to run multiple times — skips existing correct
# symlinks and warns on conflicts.

set -euo pipefail

ENACT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="$HOME/.claude"

DIRS=(agents skills scripts)

for dir in "${DIRS[@]}"; do
  src="$ENACT_DIR/$dir"
  dest="$CLAUDE_DIR/$dir"

  if [ ! -d "$src" ]; then
    echo "skip: $src does not exist"
    continue
  fi

  if [ -L "$dest" ]; then
    target="$(readlink "$dest")"
    if [ "$target" = "$src" ]; then
      echo "ok:   $dest -> $src (already exists)"
      continue
    else
      echo "WARN: $dest -> $target (points elsewhere)"
      continue
    fi
  fi

  if [ -e "$dest" ]; then
    echo "WARN: $dest exists and is not a symlink"
    continue
  fi

  ln -s "$src" "$dest"
  echo "new:  $dest -> $src"
done
