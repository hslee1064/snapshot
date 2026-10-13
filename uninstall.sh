#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./shortcuts.sh uninstall || true
rm -rf "$HOME/.local/share/bluesnap" "$HOME/.local/bin/bluesnap" "$HOME/.local/share/applications/bluesnap.desktop"
echo "BlueSnap removed."
