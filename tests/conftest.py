"""A fake desktop, so saving and reading windows can be tested without a display."""
import pytest

from stateid import desktops, privacy, store


class FakeDesktop(desktops.Desktop):
    name = "fake"

    def __init__(self, titles):
        self.titles = titles

    def windows(self):
        return [{"seq": i + 1, "pid": 0, "title": t, "app_id": "editor", "wm_class": "Editor", "app_name": "Editor",
                 "focused": i == 0, "minimized": False, "maximized": False, "workspace": 0, "user_time": 0,
                 "rect": {"x": 0, "y": 0, "width": 100, "height": 100}, "gtk_app_id": None,
                 "fullscreen": False} for i, t in enumerate(self.titles)]

    def capture(self, seq, path):
        return False


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "states")
    monkeypatch.setattr(privacy, "CONFIG_PATH", tmp_path / "config.toml")
    d = FakeDesktop(["a.txt"])
    monkeypatch.setattr(desktops, "current", lambda: d)
    return d
