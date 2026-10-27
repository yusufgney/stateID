"""Choosing the desktop backend. No display needed."""
import pytest

from stateid import desktops


@pytest.mark.parametrize("env,platform,expected", [
    ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME", "WAYLAND_DISPLAY": "wayland-0"}, "linux", "gnome"),
    ({"XDG_CURRENT_DESKTOP": "GNOME"}, "linux", "gnome"),
    ({"XDG_CURRENT_DESKTOP": "KDE", "WAYLAND_DISPLAY": "wayland-0"}, "linux", "kde"),
    ({"XDG_CURRENT_DESKTOP": "sway"}, "linux", "sway"),
    ({"XDG_CURRENT_DESKTOP": "Hyprland"}, "linux", "hyprland"),
    ({"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0"}, "linux", "x11"),
    ({"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0", "WAYLAND_DISPLAY": "w"}, "linux", "unknown"),
    ({}, "win32", "windows"),
    ({}, "darwin", "macos"),
    ({}, "linux", "unknown"),
    ({"STATEID_DESKTOP": "GNOME", "XDG_CURRENT_DESKTOP": "KDE"}, "linux", "gnome"),
])
def test_detect(env, platform, expected):
    assert desktops.detect(env, platform) == expected


def test_unsupported_desktop_says_what_was_detected(monkeypatch):
    monkeypatch.setenv("STATEID_DESKTOP", "kde")
    with pytest.raises(desktops.DesktopUnavailable, match="detected: kde"):
        desktops.current()


def test_gnome_backend_implements_the_interface():
    from stateid.desktops.gnome import GnomeShell
    for name in ("windows", "capture", "activate", "move_resize", "copy", "notify"):
        assert getattr(GnomeShell, name) is not getattr(desktops.Desktop, name), name


def test_works_without_gobject(tmp_path):
    """Other platforms have no PyGObject: the package, the CLI and the MCP server must still load,
    and UI-state capture quietly returns nothing."""
    import subprocess
    import sys
    code = ("import sys; sys.modules['gi'] = None\n"
            "import stateid.core, stateid.cli, stateid.mcp_server, stateid.store\n"
            "from stateid import a11y, desktops\n"
            "assert a11y.frame(1, 'x') is None and a11y.capture(None) is None\n"
            "try:\n"
            "    desktops.current()\n"
            "except desktops.DesktopUnavailable as e:\n"
            "    print('ok', e)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={"STATEID_DESKTOP": "gnome", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert "PyGObject" in r.stdout


def test_desktop_files_are_read_without_glib(tmp_path):
    from stateid import procs
    f = tmp_path / "x.desktop"
    f.write_text("[Desktop Entry]\nName=X\nExec=xapp %U\nStartupWMClass=XApp\nName[de]=X 100%\n"
                 "# comment\n\n[Desktop Action new-window]\nExec=xapp --new-window\n")
    groups = procs.read_desktop(f)
    assert groups["Desktop Entry"]["StartupWMClass"] == "XApp"
    assert groups["Desktop Action new-window"]["Exec"] == "xapp --new-window"
    assert procs.read_desktop(tmp_path / "missing.desktop") == {}


def test_screenshots_for_agents_are_scaled_jpegs(tmp_path, monkeypatch):
    from PIL import Image

    from stateid import store
    monkeypatch.setattr(store, "STORE", tmp_path)
    (tmp_path / "DK-AAAAAAAA").mkdir()
    Image.new("RGBA", (2000, 1000), (255, 0, 0, 0)).save(tmp_path / "DK-AAAAAAAA" / "window1.png")
    data = store.screenshot("DK-AAAAAAAA", "window1.png")
    assert data[:2] == b"\xff\xd8"
    import io
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (1280, 640) and im.getpixel((10, 10))[0] > 240  # transparent -> white
    assert store.screenshot("DK-AAAAAAAA", "../x.png") is None
