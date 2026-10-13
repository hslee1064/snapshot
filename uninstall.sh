#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./shortcuts.sh uninstall || true
rm -rf "$HOME/.local/share/snapshot" "$HOME/.local/bin/snapshot" "$HOME/.local/share/applications/snapshot.desktop"
echo "Snapshot removed."
