#!/usr/bin/env bash
# Remove the SwiftBar plugin and CLI symlinks. Your tracked data is untouched.
set -euo pipefail
DIR="${1:-$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)}"
if [ -n "${DIR:-}" ]; then
  find "$DIR" -maxdepth 1 -name 'timetracker.*.py' -print -delete
fi
rm -f "$HOME/.local/bin/tt" && echo "removed ~/.local/bin/tt"
open -g "swiftbar://refreshallplugins" 2>/dev/null || true
echo "data left in ${TIME_TRACKER_HOME:-$HOME/.time-tracker}"
