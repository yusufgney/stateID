"""Automatic saves with a fake desktop: skipped when nothing changed, pruned to [autosave] keep,
states saved by hand untouched. No display needed."""
import pytest

from stateid import autosave, core, desktops, privacy, store


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


def test_saves_only_when_something_changed(desk):
    first = autosave.once()
    assert first and first["auto"]
    assert autosave.once() is None
    desk.titles = ["a.txt", "b.txt"]
    second = autosave.once()
    assert second and second["id"] != first["id"]
    assert [r["auto"] for r in store.list_states()] == [True, True]


def test_keeps_only_the_newest_automatic_states(desk, monkeypatch):
    privacy.CONFIG_PATH.write_text("[autosave]\nkeep = 2\n")
    manual = core.save()["id"]
    ids = []
    for i in range(4):
        desk.titles = [f"t{i}"]
        ids.append(autosave.once()["id"])
    left = {r["id"] for r in store.list_states(limit=None)}
    assert manual in left and set(ids[-2:]) <= left and not set(ids[:2]) & left


def test_a_manual_save_is_not_the_baseline(desk):
    core.save()
    assert autosave.once() is not None  # no automatic state yet, so this one is saved


def test_watch_keeps_going_when_the_desktop_is_unavailable(desk, monkeypatch):
    calls, logs = [], []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise desktops.DesktopUnavailable("shell restarting")
        return None

    class Stop(Exception):
        pass

    def sleep(seconds):
        assert seconds == 15 * 60
        if len(calls) == 2:
            raise Stop

    monkeypatch.setattr(autosave, "once", flaky)
    with pytest.raises(Stop):
        autosave.watch(sleep=sleep, log=logs.append)
    assert logs == ["not saved: shell restarting"]


def test_service_unit_runs_watch(tmp_path, monkeypatch):
    monkeypatch.setattr(autosave, "UNIT_DIR", tmp_path)
    ran = []
    monkeypatch.setattr(autosave, "_systemctl", lambda *a: ran.append(a) or type("R", (), {"returncode": 0})())
    autosave.enable("/opt/bin/stateid")
    unit = (tmp_path / autosave.UNIT).read_text()
    assert "ExecStart=/opt/bin/stateid autosave --watch" in unit and "graphical-session.target" in unit
    assert ("enable", "--now", autosave.UNIT) in ran
    autosave.disable()
    assert not (tmp_path / autosave.UNIT).exists()
