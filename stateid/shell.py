"""Talk to the stateid GNOME Shell extension over D-Bus (window list, screenshots, placement, clipboard)."""
import json

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

DBUS_PATH, DBUS_IFACE = "/io/github/yusufgney/StateId", "io.github.yusufgney.StateId"
EXTENSION_UUID = "stateid@yusufgney.github.io"


class ShellUnavailable(RuntimeError):
    pass


class Shell:
    def __init__(self):
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
            "org.gnome.Shell", DBUS_PATH, DBUS_IFACE, None)

    def call(self, method, sig=None, *args):
        params = GLib.Variant(sig, args) if sig else None
        try:
            res = self.proxy.call_sync(method, params, Gio.DBusCallFlags.NONE, 10000, None)
        except GLib.Error as e:
            raise ShellUnavailable(
                "Cannot reach the GNOME Shell extension. Is it enabled?\n"
                f"  gnome-extensions info {EXTENSION_UUID}\n"
                "(A newly installed extension loads after you log out and back in.)\n" + e.message) from None
        return res.unpack()[0] if res and res.n_children() else None

    def windows(self):
        return json.loads(self.call("ListWindows"))

    def capture(self, seq, path):
        return self.call("Capture", "(us)", seq, str(path))

    def activate(self, seq):
        return self.call("Activate", "(u)", seq)

    def move_resize(self, seq, r, maximize):
        return self.call("MoveResize", "(uiiiib)", seq, r["x"], r["y"], r["width"], r["height"], maximize)

    def copy(self, text):
        self.call("CopyToClipboard", "(s)", text)

    def notify(self, title, body):
        self.call("Notify", "(ss)", title, body)
