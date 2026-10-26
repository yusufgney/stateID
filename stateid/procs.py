"""Process inspection, document discovery and launching."""
import glob
import os
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

HOME = Path.home()


def read_proc(pid, name):
    try:
        if name in ("cwd", "exe"):
            return os.readlink(f"/proc/{pid}/{name}")
        return Path(f"/proc/{pid}/{name}").read_text()
    except OSError:
        return None


def cmdline(pid):
    raw = read_proc(pid, "cmdline")
    return [a for a in raw.split("\0") if a] if raw else []


def children(pid):
    out = []
    for task in glob.glob(f"/proc/{pid}/task/*/children"):
        try:
            out += [int(c) for c in Path(task).read_text().split()]
        except OSError:
            pass
    return out


def descendants(pid, depth=4):
    if depth == 0:
        return []
    out = []
    for c in children(pid):
        out.append(c)
        out += descendants(c, depth - 1)
    return out


def tty_of(pid):
    try:
        t = os.readlink(f"/proc/{pid}/fd/0")
        return t if t.startswith("/dev/pts/") else None
    except OSError:
        return None


def foreground(pid):
    """Foreground process of the terminal `pid` is attached to."""
    try:
        tpgid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[5])
        return tpgid if tpgid > 0 else pid
    except (OSError, ValueError, IndexError):
        return pid


SKIP_PARTS = {".cache", ".config", ".local", ".mozilla", ".pki", "snap", ".var", ".vscode", "node_modules", ".git"}


def user_files(pids):
    """Documents the processes hold open (not libraries, caches or std streams)."""
    found = []
    for pid in pids:
        for fd in glob.glob(f"/proc/{pid}/fd/*"):
            if fd.rsplit("/", 1)[1] in ("0", "1", "2"):
                continue
            try:
                p = os.readlink(fd)
            except OSError:
                continue
            if not p.startswith(("/home/", "/media/", "/mnt/")) or p.endswith(" (deleted)"):
                continue
            parts = Path(p).parts
            if SKIP_PARTS & set(parts) or any(x.startswith(".") for x in parts[3:]) or not os.path.isfile(p):
                continue
            if p not in found:
                found.append(p)
    return found


def file_arg_docs(cmd):
    return [os.path.abspath(a) for a in cmd[1:] if not a.startswith("-") and os.path.isfile(a)]


def recent_files():
    """GTK's recently-used list, newest first: most apps record every document they open here."""
    try:
        root = ET.parse(HOME / ".local/share/recently-used.xbel").getroot()
    except (OSError, ET.ParseError):
        return []
    items = []
    for b in root.iter("bookmark"):
        href = b.get("href", "")
        if href.startswith("file://"):
            items.append((b.get("visited") or b.get("modified") or "", unquote(urlparse(href).path)))
    return [p for _, p in sorted(items, reverse=True)]


FILENAME_RE = re.compile(r"[^/\\:*?\"<>|()]+\.[A-Za-z0-9]{1,8}")


def files_from_title(title, cwd):
    """Resolve document names shown in a window title, e.g. "settings.yml (~/project) - Text Editor"."""
    title = re.sub(r"^[\s•*●]+", "", title or "")
    hints = [os.path.expanduser(h) for h in re.findall(r"\(([~/][^)]*)\)", title)]
    names = []
    for seg in re.split(r"\s+[-—–|]\s+|\s*\([^)]*\)\s*", title):
        seg = seg.strip(" •*●")
        if FILENAME_RE.fullmatch(seg) and seg not in names:
            names.append(seg)
    if not names:
        return []
    recent = None
    found = []
    for name in names:
        hit = next((os.path.join(h, name) for h in hints if os.path.isfile(os.path.join(h, name))), None)
        if hit is None:
            recent = recent if recent is not None else recent_files()
            hit = next((p for p in recent if os.path.basename(p) == name and os.path.isfile(p)), None)
        if hit is None and cwd and os.path.isfile(os.path.join(cwd, name)):
            hit = os.path.join(cwd, name)
        if hit:
            found.append(hit)
    return found


def spawn(cmd, cwd=None):
    """Start a detached program; returns the command line for reports."""
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=cwd if cwd and os.path.isdir(cwd) else HOME)
    return shlex.join(cmd)


# ---------------------------------------------------------------- .desktop launchers
DESKTOP_DIRS = [HOME / ".local/share/applications", Path("/usr/share/applications"),
                Path("/usr/local/share/applications"), Path("/var/lib/snapd/desktop/applications"),
                Path("/var/lib/flatpak/exports/share/applications"),
                HOME / ".local/share/flatpak/exports/share/applications"]


def environ(pid):
    raw = read_proc(pid, "environ")
    return dict(kv.split("=", 1) for kv in raw.split("\0") if "=" in kv) if raw else {}


def launch_argv(pid, cmd, cwd):
    """(command, working directory) that still work after the app exits.

    An AppImage runs from a temporary mount (/tmp/.mount_*) that disappears with it: use the
    .AppImage file itself and the directory it was started from. Relative program paths become
    absolute."""
    env = environ(pid)
    if env.get("APPIMAGE") and cmd:
        return [env["APPIMAGE"], *cmd[1:]], env.get("OWD") or str(HOME)
    if cmd and "/" in cmd[0] and not os.path.isabs(cmd[0]):
        exe = read_proc(pid, "exe")
        cmd = [exe if exe and not exe.endswith(" (deleted)") else os.path.join(cwd or "", cmd[0]), *cmd[1:]]
    return cmd, cwd


def _desktop_entries():
    for d in DESKTOP_DIRS:
        try:
            yield from sorted(d.glob("*.desktop"))
        except OSError:
            continue


def find_desktop(app_id, wm_class=None):
    """The .desktop file for an app id, or, when the window has no app id (e.g. AppImages show up
    as "window:N"), the one whose StartupWMClass or program name matches the window class."""
    if app_id:
        hit = next((str(p / app_id) for p in DESKTOP_DIRS if (p / app_id).exists()), None)
        if hit:
            return hit
    if not wm_class:
        return None
    want = wm_class.lower()
    by_exec = None
    for path in _desktop_entries():
        kf = GLib.KeyFile()
        try:
            kf.load_from_file(str(path), GLib.KeyFileFlags.NONE)
            keys = kf.get_keys("Desktop Entry")[0]
            if "StartupWMClass" in keys and kf.get_string("Desktop Entry", "StartupWMClass").lower() == want:
                return str(path)
            prog = os.path.basename(shlex.split(kf.get_string("Desktop Entry", "Exec"))[0]).lower()
        except (GLib.Error, ValueError, IndexError):
            continue
        if by_exec is None and (prog == want or prog.split(".")[0] == want):
            by_exec = str(path)
    return by_exec


def desktop_command(app_id, files, wm_class=None):
    """The app's own Exec line with the documents filled in.

    Running Exec directly (instead of D-Bus activation, which `gio launch` uses for
    DBusActivatable apps) always opens a window, even when the app has no document.
    """
    path = find_desktop(app_id, wm_class)
    if not path:
        return None
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.NONE)
        exec_ = kf.get_string("Desktop Entry", "Exec")
        if not files:  # prefer an explicit "new window" action when there is nothing to open
            for group in kf.get_groups()[0]:
                if group.startswith("Desktop Action") and "new-window" in group.lower():
                    exec_ = kf.get_string(group, "Exec")
                    break
    except GLib.Error:
        return None
    cmd = []
    for arg in shlex.split(exec_):
        if arg in ("%U", "%F"):
            cmd += files
        elif arg in ("%u", "%f"):
            cmd += files[:1]
        elif arg in ("%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"):
            continue
        else:
            cmd.append(arg.replace("%%", "%"))
    return cmd
