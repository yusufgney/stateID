"""What stateid needs from a desktop. Each supported desktop (GNOME, later X11, KDE, Windows) implements
this; everything else in stateid only talks to this interface.

A window is a dict with these keys:
    seq         int, stable id of the window for as long as it exists
    pid         int or None, process that owns it
    title       str
    app_id      str or None, the desktop's app id (e.g. "org.gnome.TextEditor.desktop")
    app_name    str or None, human-readable app name
    wm_class    str or None, X11/Wayland window class
    gtk_app_id  str or None
    rect        {"x", "y", "width", "height"} in desktop coordinates, without shadows
    workspace   int or None
    maximized, minimized, focused, fullscreen   bool
    user_time   int, larger means used more recently
Backends may add keys of their own; nothing outside the backend may rely on them."""


class DesktopUnavailable(RuntimeError):
    pass


class Desktop:
    name = "unknown"

    def windows(self):
        """Every normal window, as dicts described above."""
        raise NotImplementedError

    def capture(self, seq, path):
        """Write a PNG screenshot of the window to path; return whether it worked."""
        raise NotImplementedError

    def activate(self, seq):
        """Focus the window and raise it."""
        raise NotImplementedError

    def move_resize(self, seq, rect, maximize):
        """Put the window at rect, or maximize it."""
        raise NotImplementedError

    def copy(self, text):
        """Put text on the clipboard."""
        raise NotImplementedError

    def notify(self, title, body):
        """Show a desktop notification."""
        raise NotImplementedError
