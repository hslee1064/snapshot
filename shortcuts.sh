#!/usr/bin/env bash
# Register / remove GNOME custom keyboard shortcuts for BlueSnap.
#   ./shortcuts.sh install | uninstall
set -euo pipefail
BASE=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
SCHEMA=org.gnome.settings-daemon.plugins.media-keys
BIN="$HOME/.local/bin/bluesnap"

# name | command | binding
SHORTCUTS=(
  "BlueSnap|$BIN ui|<Super><Shift>s"
  "BlueSnap area|$BIN shot area|<Ctrl><Shift>Print"
  "BlueSnap window|$BIN shot window|<Ctrl><Alt>Print"
  "BlueSnap record|$BIN toggle-record|<Ctrl><Shift><Alt>r"
)

current=$(gsettings get $SCHEMA custom-keybindings)
# Drop our previous entries (paths containing /bluesnap).
paths=$(/usr/bin/python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); print(' '.join(p for p in l if '/bluesnap' not in p))" "$current")

if [[ "${1:-install}" == uninstall ]]; then
  for i in "${!SHORTCUTS[@]}"; do dconf reset -f "$BASE/bluesnap$i/"; done
else
  for i in "${!SHORTCUTS[@]}"; do
    IFS='|' read -r name cmd key <<<"${SHORTCUTS[$i]}"
    p="$BASE/bluesnap$i/"
    gsettings set $SCHEMA.custom-keybinding:$p name "$name"
    gsettings set $SCHEMA.custom-keybinding:$p command "$cmd"
    gsettings set $SCHEMA.custom-keybinding:$p binding "$key"
    paths="$paths $p"
    echo "  $key -> $cmd"
  done
fi

list=$(for p in $paths; do printf "'%s', " "$p"; done)
gsettings set $SCHEMA custom-keybindings "[${list%, }]"
echo "Shortcuts ${1:-install}ed."
