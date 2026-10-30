"""What an agent reads without an ID: the desktop now (nothing saved) and the newest state."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

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


def test_one_window_read_now_by_its_seq(desk):
    desk.titles = ["a.txt", "b.txt"]
    md, shot = core.window_detail(2)
    assert "Editor — b.txt" in md and "a.txt" not in md and "working in" not in md  # b.txt is not focused
    assert shot is None  # agents see no screenshots unless the user allows them
    with pytest.raises(LookupError, match="No open window 7"):
        core.window_detail(7)


def test_window_detail_screenshot_follows_the_privacy_settings(desk):
    desk.shots = True
    desk.titles = ["a.txt", "bank account", "secret notes"]
    privacy.CONFIG_PATH.write_text('[privacy]\nagent_screenshots = true\nexclude = ["secret"]\nprivate = ["bank"]\n')
    md, shot = core.window_detail(1)
    assert shot[:2] == b"\xff\xd8"  # a JPEG
    md, shot = core.window_detail(2)
    assert shot is None and "private window" in md and "bank" not in md
    with pytest.raises(LookupError, match="excluded"):
        core.window_detail(3)
    result = mcp_server.get_window_detail(1)
    assert "a.txt" in result[0] and result[1].data[:2] == b"\xff\xd8"
    assert len(mcp_server.get_window_detail(1, include_screenshot=False)) == 1


def test_focus_window(desk):
    desk.titles = ["a.txt", "b.txt", "secret notes"]
    privacy.CONFIG_PATH.write_text('[privacy]\nexclude = ["secret"]\n')
    assert mcp_server.focus_window(2)["title"] == "b.txt" and desk.activated == [2]
    for seq in (3, 9):  # excluded, gone
        with pytest.raises(ToolError):
            mcp_server.focus_window(seq)
    assert desk.activated == [2]


def test_open_document_opens_documents_only(desk, tmp_path):
    doc, script, launcher = tmp_path / "report.odt", tmp_path / "run.sh", tmp_path / "app.desktop"
    for f in (doc, script, launcher):
        f.write_text("x")
    script.chmod(0o755)
    assert mcp_server.open_document(str(doc)) == {"opened": str(doc), "app": "Text Editor"}
    assert mcp_server.open_document(str(tmp_path))["opened"] == str(tmp_path)  # a folder
    for bad in (script, launcher, tmp_path / "missing.txt", "report.odt"):
        with pytest.raises(ToolError):
            mcp_server.open_document(str(bad))
    assert desk.opened == [str(doc), str(tmp_path)]
