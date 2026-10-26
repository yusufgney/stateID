#!/usr/bin/env bash
# Remove stateid's command, extension, shortcut and OnlyOffice launcher override.
# Saved states in ~/.local/share/stateid are kept; delete that folder yourself if you want.
set -euo pipefail

UUID="stateid@yusufgney.github.io"
KEY_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stateid/"

gnome-extensions disable "$UUID" 2>/dev/null || true
rm -f "$HOME/.local/bin/stateid" "$HOME/.local/share/gnome-shell/extensions/$UUID"

python3 - "$UUID" "$KEY_PATH" <<'EOF'
import ast, subprocess, sys
uuid, path = sys.argv[1], sys.argv[2]
def edit(schema, key, drop):
    cur = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True).stdout.strip()
    items = ast.literal_eval(cur.removeprefix("@as ") or "[]")
    if drop in items:
        subprocess.run(["gsettings", "set", schema, key, str([i for i in items if i != drop])], check=True)
edit("org.gnome.shell", "enabled-extensions", uuid)
edit("org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings", path)
EOF

for f in "$HOME"/.local/share/applications/*onlyoffice*desktopeditors*.desktop; do
    [[ -f "$f" ]] && grep -q -- "--remote-debugging-port" "$f" && rm -f "$f"
done
echo "stateid removed. Saved states are still in ~/.local/share/stateid"
