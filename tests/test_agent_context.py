"""What an agent reads without an ID: the desktop now (nothing saved) and the newest state."""
import pytest

from stateid import core, mcp_server, privacy, store


def test_now_reads_without_saving(desk):
    desk.titles = ["a.txt", "b.txt"]
    md = core.now()
    assert md.startswith("# Desktop now") and "not saved: 2 window(s)" in md
    assert "Editor — a.txt" in md and "Editor — b.txt" in md and "stateid restore" not in md
    assert not store.STORE.exists() or not list(store.STORE.iterdir())
    focused = core.now(focused=True)
    assert "a.txt" in focused and "b.txt" not in focused


def test_now_respects_privacy(desk):
    privacy.CONFIG_PATH.write_text('[privacy]\nexclude = ["secret"]\nprivate = ["bank"]\n')
    desk.titles = ["bank account", "secret notes", "a.txt"]
    md = core.now()
    assert "bank account" not in md and "secret notes" not in md and "private window" in md
    assert "1 window(s) were not captured" in md


def test_latest_is_the_newest_state(desk):
    with pytest.raises(store.StateNotFound, match="No saved states yet"):
        store.load("latest")
    core.save()
    desk.titles = ["b.txt"]
    newest = core.save()["id"]
    assert store.load("latest")["id"] == newest and store.load(" LATEST ")["id"] == newest
    assert f"# State {newest}" in core.summary("latest")
    assert "Changes from" in core.diff("latest") and "Nothing changed" in core.diff("latest")
    with pytest.raises(store.StateNotFound):
        store.delete("latest")  # deleting needs a real ID


def test_mcp_read_desktop_and_prompts(desk):
    assert "# Desktop now" in mcp_server.read_desktop()
    assert "latest" in mcp_server.continue_from_state() and "diff_states" in mcp_server.catch_up("DK-AAAAAAAA")
    assert "read_desktop" in mcp_server.look_at_my_screen("why does it fail?")
