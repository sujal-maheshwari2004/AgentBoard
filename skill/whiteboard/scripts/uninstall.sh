#!/usr/bin/env bash
# Remove the whiteboard skill symlink from ~/.claude/skills. Project-level files are left alone.
set -euo pipefail

TARGET="$HOME/.claude/skills/whiteboard"
if [ -L "$TARGET" ]; then
  rm "$TARGET"
  echo "removed symlink $TARGET"
elif [ -d "$TARGET" ]; then
  echo "$TARGET is a real directory, not a symlink; remove it manually if intended" >&2
  exit 1
else
  echo "nothing to remove at $TARGET"
fi
