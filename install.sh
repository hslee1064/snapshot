#!/usr/bin/env bash
# Install Snapshot for the current user (~/.local).
#   ./install.sh               install app + launcher
#   ./install.sh --deps        also apt-install dependencies (needs sudo)
#   ./install.sh --shortcuts   also register GNOME keyboard shortcuts
set -euo pipefail
cd "$(dirname "$0")"

DEPS=(ffmpeg python3-gi python3-xlib gir1.2-gtk-3.0 gir1.2-wnck-3.0
      gir1.2-notify-0.7 gir1.2-ayatanaappindicator3-0.1 pulseaudio-utils xdg-user-dirs)

for arg in "$@"; do
  case "$arg" in
    --deps) sudo apt-get install -y "${DEPS[@]}" ;;
    --shortcuts) WANT_SHORTCUTS=1 ;;
  esac
done

install -Dm755 snapshot.py "$HOME/.local/share/snapshot/snapshot.py"
install -Dm755 /dev/stdin "$HOME/.local/bin/snapshot" <<'SH'
#!/bin/sh
exec /usr/bin/python3 "$HOME/.local/share/snapshot/snapshot.py" "$@"
SH
install -Dm644 snapshot.desktop "$HOME/.local/share/applications/snapshot.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed: ~/.local/bin/snapshot"

if [[ "${WANT_SHORTCUTS:-}" == 1 ]]; then
  ./shortcuts.sh install
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "NOTE: add ~/.local/bin to PATH" ;; esac
