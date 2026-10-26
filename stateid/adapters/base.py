class Adapter:
    """Reads and restores one app's internal state.

    capture() fills st["app_state"] (and optionally st["restore_cmd"] / st["files"]);
    launch() reopens the app and returns the launched command lines, or [] to fall back
    to the generic launcher; apply() runs once the window is back and returns what it did.
    """
    pattern = None
    single_window = False  # match an open window by app alone (its title changes with content)

    def capture(self, w, st):
        pass

    def launch(self, st):
        return []

    def apply(self, st, win, shell):
        return []
