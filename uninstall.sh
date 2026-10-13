#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./shortcuts.sh uninstall || true
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/snapshot.svg"
rm -rf "$HOME/.local/share/snapshot" "$HOME/.local/bin/snapshot" "$HOME/.local/share/applications/snapshot.desktop"
echo "Snapshot removed."
