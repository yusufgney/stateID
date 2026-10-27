"""Pick the backend for the desktop stateid runs on. Backends are imported lazily, so a backend's
dependencies (GObject for GNOME, Win32 for Windows) are only needed where it is used."""
import os
import sys

from .base import Desktop, DesktopUnavailable

SUPPORTED = ("gnome",)


def detect(env=None, platform=None):
    """The desktop of this session: gnome, kde, sway, hyprland, x11, windows, macos or unknown.
    STATEID_DESKTOP overrides the guess."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("STATEID_DESKTOP"):
        return env["STATEID_DESKTOP"].lower()
    if platform == "win32":
        return "windows"
    if platform == "darwin":
        return "macos"
    current = env.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    for name, marks in (("gnome", ("gnome",)), ("kde", ("kde",)), ("sway", ("sway",)),
                        ("hyprland", ("hyprland",))):
        if any(m in current for m in marks):
            return name
    if env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return "x11"
    return "unknown"


def current():
    """The backend for this session, or DesktopUnavailable saying what is missing."""
    name = detect()
    if name == "gnome":
        try:
            from .gnome import GnomeShell
        except (ImportError, ValueError) as e:
            raise DesktopUnavailable(f"The GNOME backend needs PyGObject (python3-gi): {e}") from None
        return GnomeShell()
    raise DesktopUnavailable(
        f"stateid does not support this desktop yet (detected: {name}). Supported: {', '.join(SUPPORTED)}. "
        "Set STATEID_DESKTOP to override the detection.")


__all__ = ["Desktop", "DesktopUnavailable", "SUPPORTED", "current", "detect"]
