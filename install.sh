#!/usr/bin/env bash
# Install the time tracker as a SwiftBar plugin (+ a `tt` CLI on your PATH).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="timetracker.15s.py"

if [ $# -ge 1 ]; then
  DIR="$1"
else
  DIR="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
fi

if [ -z "${DIR:-}" ]; then
  echo "SwiftBar has no plugin folder configured."
  echo "Open SwiftBar → Preferences → set a plugin folder, then re-run:"
  echo "    ./install.sh [/path/to/plugin/folder]"
  exit 1
fi

mkdir -p "$DIR"
chmod +x "$HERE/$PLUGIN" "$HERE/tt_core.py"
# drop links from earlier installs that used a different refresh interval
find "$DIR" -maxdepth 1 -name 'timetracker.*.py' ! -name "$PLUGIN" -delete
ln -sfn "$HERE/$PLUGIN" "$DIR/$PLUGIN"
echo "plugin  → $DIR/$PLUGIN"

mkdir -p "$HOME/.local/bin"
ln -sfn "$HERE/tt_core.py" "$HOME/.local/bin/tt"
echo "cli     → ~/.local/bin/tt"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "        (add ~/.local/bin to PATH to use \`tt\` from the shell)" ;;
esac

echo "data    → ${TIME_TRACKER_HOME:-$HOME/.time-tracker}"
open -g "swiftbar://refreshallplugins" 2>/dev/null || true
echo "done — a stopwatch icon should appear in the menu bar."
