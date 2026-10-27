#!/usr/bin/env bash
# Install (or remove) the stateid freeze helper. Needs root:
#   sudo ~/stateid/helper/install-helper.sh            install or update
#   sudo ~/stateid/helper/install-helper.sh --uninstall
# The helper is copied to a root-owned place, never run from this (user-writable) repository.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/usr/local/libexec/stateid
UNIT=/etc/systemd/system/stateid-helper.service

if [[ ${1:-} == --uninstall ]]; then
    systemctl disable --now stateid-helper.service 2>/dev/null || true
    rm -f "$UNIT" "$DEST/stateid_helperd.py"
    rmdir "$DEST" 2>/dev/null || true
    systemctl daemon-reload
    echo "Removed the helper. Frozen images are kept in /var/lib/stateid (delete with: sudo rm -r /var/lib/stateid)."
    exit 0
fi

echo "Installing criu, the virtual display (Xvnc) and the viewer"
apt-get install -y criu tigervnc-standalone-server tigervnc-viewer

echo "Installing the helper -> $DEST"
install -d -o root -g root -m 0755 "$DEST"
install -o root -g root -m 0644 "$HERE/stateid_helperd.py" "$DEST/stateid_helperd.py"
install -o root -g root -m 0644 "$HERE/stateid-helper.service" "$UNIT"
systemctl daemon-reload
systemctl enable stateid-helper.service >/dev/null
systemctl restart stateid-helper.service   # running sessions survive (KillMode=process)
sleep 1
systemctl is-active --quiet stateid-helper.service && echo "stateid-helper is running." \
    || { echo "stateid-helper failed to start:"; journalctl -u stateid-helper -n 20 --no-pager; exit 1; }
