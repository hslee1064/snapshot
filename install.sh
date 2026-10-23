#!/usr/bin/env bash
# Install Snapshot for the current user (~/.local).
#   ./install.sh               install app + launcher
#   ./install.sh --deps        also apt-install dependencies (needs sudo)
#   ./install.sh --shortcuts   also register GNOME keyboard shortcuts
set -euo pipefail
cd "$(dirname "$0")"

DEPS=(ffmpeg python3-gi gir1.2-gtk-3.0 xdg-desktop-portal-gnome gstreamer1.0-tools gstreamer1.0-pipewire
      gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-pulseaudio
      gir1.2-notify-0.7 gir1.2-gstreamer-1.0 gstreamer1.0-gtk3 gstreamer1.0-libav pulseaudio-utils xdg-user-dirs)

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
install -Dm644 snapshot.svg "$HOME/.local/share/icons/hicolor/scalable/apps/snapshot.svg"
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
install -Dm644 snapshot.desktop "$HOME/.local/share/applications/snapshot.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
# Shell extension for hover-picking windows; GNOME loads new extensions after the next login.
EXT="$HOME/.local/share/gnome-shell/extensions/snapshot@hyunsik1-lee"
install -Dm644 extension/metadata.json "$EXT/metadata.json"
install -Dm644 extension/extension.js "$EXT/extension.js"
gnome-extensions enable snapshot@hyunsik1-lee 2>/dev/null \
  || gsettings set org.gnome.shell enabled-extensions \
       "$(gsettings get org.gnome.shell enabled-extensions | python3 -c 'import ast,sys; l=ast.literal_eval(sys.stdin.read().replace("@as ","")); u="snapshot@hyunsik1-lee"; print(l if u in l else l+[u])')"
echo "Installed: ~/.local/bin/snapshot"

if [[ "${WANT_SHORTCUTS:-}" == 1 ]]; then
  ./shortcuts.sh install
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "NOTE: add ~/.local/bin to PATH" ;; esac
