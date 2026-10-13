#!/usr/bin/env bash
# Install BlueSnap for the current user (~/.local).
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

install -Dm755 bluesnap.py "$HOME/.local/share/bluesnap/bluesnap.py"
install -Dm755 /dev/stdin "$HOME/.local/bin/bluesnap" <<'SH'
#!/bin/sh
exec /usr/bin/python3 "$HOME/.local/share/bluesnap/bluesnap.py" "$@"
SH
install -Dm644 bluesnap.desktop "$HOME/.local/share/applications/bluesnap.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed: ~/.local/bin/bluesnap"

if [[ "${WANT_SHORTCUTS:-}" == 1 ]]; then
  ./shortcuts.sh install
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "NOTE: add ~/.local/bin to PATH" ;; esac
