"""A fake desktop, so saving and reading windows can be tested without a display."""
import pytest
from PIL import Image

from stateid import desktops, privacy, store


class FakeDesktop(desktops.Desktop):
    name = "fake"

    def __init__(self, titles):
        self.titles = titles
        self.shots = False  # whether capture() works
        self.saves = True  # whether an app saves when save_window() presses Ctrl+S
        self.pid = 0
        self.activated, self.opened, self.saved = [], [], []

    def windows(self):
        return [{"seq": i + 1, "pid": self.pid, "title": t, "app_id": "editor", "wm_class": "Editor", "app_name": "Editor",
                 "focused": i == 0, "minimized": False, "maximized": False, "workspace": 0, "user_time": 0,
                 "rect": {"x": 0, "y": 0, "width": 100, "height": 100}, "gtk_app_id": None,
                 "fullscreen": False} for i, t in enumerate(self.titles)]

    def capture(self, seq, path):
        if self.shots:
            Image.new("RGB", (40, 30), "red").save(path)
        return self.shots

    def activate(self, seq):
        self.activated.append(seq)
        return True

    def save_window(self, seq):
        self.saved.append(seq)
        if self.saves:
            self.titles[seq - 1] = self.titles[seq - 1].lstrip("•*● ")
        return True

    def open_file(self, path):
        self.opened.append(path)
        return "Text Editor"


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "states")
    monkeypatch.setattr(privacy, "CONFIG_PATH", tmp_path / "config.toml")
    d = FakeDesktop(["a.txt"])
    monkeypatch.setattr(desktops, "current", lambda: d)
    return d
