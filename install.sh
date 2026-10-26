#!/usr/bin/env bash
# Install stateid for the current user (GNOME on Wayland).
# Files are symlinked, so `git pull` updates the installation.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="stateid@yusufgney.github.io"
BIN_DIR="$HOME/.local/bin"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions"
APP_DIR="$HOME/.local/share/applications"
KEY_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stateid/"
ONLYOFFICE_PORT="${STATEID_ONLYOFFICE_PORT:-9333}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }

say "Checking dependencies"
missing=()
python3 -c 'import gi; gi.require_version("Gio", "2.0")' 2>/dev/null || missing+=(python3-gi)
python3 -c 'import gi; gi.require_version("Atspi", "2.0")' 2>/dev/null || missing+=(gir1.2-atspi-2.0)
python3 -c 'import websockets' 2>/dev/null || missing+=(python3-websockets)
if ((${#missing[@]})); then
    echo "Missing packages: ${missing[*]}"
    echo "Install them with: sudo apt install ${missing[*]}"
    exit 1
fi

say "Installing the stateid command -> $BIN_DIR/stateid"
mkdir -p "$BIN_DIR"
ln -sfn "$REPO/bin/stateid" "$BIN_DIR/stateid"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "  note: add $BIN_DIR to your PATH" ;; esac

say "Installing the GNOME Shell extension ($UUID)"
mkdir -p "$EXT_DIR"
ln -sfn "$REPO/extension/$UUID" "$EXT_DIR/$UUID"
python3 - "$UUID" <<'EOF'
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
                     capture_output=True, text=True).stdout.strip().removeprefix("@as ")
exts = ast.literal_eval(cur or "[]")
if uuid not in exts:
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(exts + [uuid])], check=True)
EOF
gnome-extensions enable "$UUID" 2>/dev/null || true

say "Adding the Super+Shift+S shortcut (snapshot the whole desktop)"
python3 - "$KEY_PATH" "$BIN_DIR/stateid" <<'EOF'
import ast, subprocess, sys
path, binary = sys.argv[1], sys.argv[2]
schema = "org.gnome.settings-daemon.plugins.media-keys"
cur = subprocess.run(["gsettings", "get", schema, "custom-keybindings"], capture_output=True, text=True).stdout.strip()
paths = ast.literal_eval(cur.removeprefix("@as ") or "[]")
if path not in paths:
    subprocess.run(["gsettings", "set", schema, "custom-keybindings", str(paths + [path])], check=True)
kb = f"{schema}.custom-keybinding:{path}"
for key, value in (("name", "stateid: copy state ID"), ("command", f"{binary} save --copy"),
                   ("binding", "<Shift><Super>s")):
    subprocess.run(["gsettings", "set", kb, key, value], check=True)
EOF

say "Letting stateid read OnlyOffice (debug port $ONLYOFFICE_PORT on 127.0.0.1)"
oo=""
for d in /var/lib/snapd/desktop/applications /usr/share/applications /var/lib/flatpak/exports/share/applications; do
    for f in "$d"/*onlyoffice*desktopeditors*.desktop; do [[ -f "$f" ]] && oo="$f" && break 2; done
done
if [[ -n "$oo" ]]; then
    mkdir -p "$APP_DIR"
    sed -E "s#^(Exec=[^ ]*(desktopeditors|DesktopEditors)[^ ]*)#\1 --remote-debugging-port=$ONLYOFFICE_PORT#" \
        "$oo" > "$APP_DIR/$(basename "$oo")"
    update-desktop-database "$APP_DIR" 2>/dev/null || true
    echo "  $APP_DIR/$(basename "$oo") (restart OnlyOffice from the app menu once)"
else
    echo "  OnlyOffice not found, skipped"
fi

say "Done."
if ! gdbus introspect --session --dest org.gnome.Shell --object-path /io/github/yusufgney/StateId 2>/dev/null \
        | grep -q io.github.yusufgney.StateId; then
    echo "Log out and back in once so GNOME Shell loads the extension."
fi
echo "Then try: stateid save --copy   |   stateid --help"
