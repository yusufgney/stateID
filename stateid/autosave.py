"""Optional automatic saves: a systemd user service runs `stateid autosave --watch`, which saves the
desktop every [autosave] interval_minutes, but only when the windows changed since the last automatic
state. Agents then always have a recent state to read or diff against, without the user making one."""
import os
import subprocess
import sys
import time
from pathlib import Path

from . import core, freeze, privacy, store
from .desktops import DesktopUnavailable

UNIT = "stateid-autosave.service"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"


class ServiceError(RuntimeError):
    pass


UNIT_TEXT = """\
[Unit]
Description=stateid: save the desktop state automatically when it changes
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={exe} autosave --watch
Restart=on-failure
RestartSec=60

[Install]
WantedBy=graphical-session.target
"""


def once():
    """Save if something changed. Returns the new state's summary, or None when nothing changed."""
    return core.save(auto=True)


def watch(sleep=time.sleep, log=lambda msg: print(msg, file=sys.stderr, flush=True)):
    """Save forever, re-reading the settings every round so changes apply without a restart."""
    while True:
        try:
            minutes = privacy.load()["autosave"]["interval_minutes"]
        except privacy.ConfigError as e:
            log(str(e))
            minutes = privacy.DEFAULTS["autosave"]["interval_minutes"]
        try:
            r = once()
            if r:
                log(f"saved {r['id']} ({len(r['windows'])} window(s))")
        except (DesktopUnavailable, LookupError, freeze.HelperError, freeze.HelperUnavailable) as e:
            log(f"not saved: {e}")  # GNOME Shell restarting, no windows, everything excluded...
        sleep(max(1, float(minutes)) * 60)


def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def enable(exe):
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / UNIT).write_text(UNIT_TEXT.format(exe=exe))
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", UNIT)
    if r.returncode:
        raise ServiceError(f"systemctl --user enable {UNIT} failed: {r.stderr.strip()}")


def disable():
    _systemctl("disable", "--now", UNIT)
    (UNIT_DIR / UNIT).unlink(missing_ok=True)
    _systemctl("daemon-reload")


def status():
    active = _systemctl("is-active", UNIT).stdout.strip() or "inactive"
    autos = [r for r in store.list_states(limit=None) if r.get("auto")]
    return {"service": active, "automatic_states": len(autos), "latest": autos[0] if autos else None}
