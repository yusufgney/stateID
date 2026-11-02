"""Programs that save their own work: saving through them, the versions they were saved with, and
whether an update to them is waiting (and could get in the way of a saved state)."""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .procs import cmdline, files_from_title, read_proc

# Programs that save their own work (documents, projects, drafts). Freezing is for work that cannot be
# saved and for long-running jobs; these are saved with their own save instead.
SELF_SAVING = {
    "soffice": "LibreOffice", "libreoffice": "LibreOffice", "lowriter": "LibreOffice",
    "localc": "LibreOffice", "loimpress": "LibreOffice", "desktopeditors": "OnlyOffice",
    "onlyoffice-desktopeditors": "OnlyOffice", "code": "VS Code", "codium": "VSCodium",
    "gnome-text-editor": "Text Editor", "gedit": "gedit", "kate": "Kate", "kwrite": "KWrite",
    "mousepad": "Mousepad", "xed": "Xed", "pluma": "Pluma", "gimp": "GIMP", "inkscape": "Inkscape",
    "krita": "Krita", "xournalpp": "Xournal++", "blender": "Blender", "freecad": "FreeCAD",
}
# Editors where Ctrl+S on a document without changes does nothing, so it is pressed even when the title
# shows no unsaved marker (some, like GNOME Text Editor, never show one in the window title).
QUIET_SAVE = {"Text Editor", "gedit", "Kate", "KWrite", "Mousepad", "Xed", "Pluma", "VS Code", "VSCodium"}
UNSAVED_RE = re.compile(r"^\s*[•*●]")
SAVE_WAIT = 5.0  # seconds an app gets to clear its unsaved marker after Ctrl+S
QUIET_WAIT = 1.0  # seconds to wait for a file to change when the window showed no marker
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "stateid" / "versions.json"


def saves_itself(argv):
    """The name of the program in argv when it is known to save its own work, else None."""
    if not argv:
        return None
    name = re.sub(r"(\.bin)?[-_]?\d+(\.\d+)*$", "", os.path.basename(argv[0]).lower())
    return SELF_SAVING.get(name)


def unsaved(title):
    return bool(UNSAVED_RE.match(title or ""))


def _mtimes(paths):
    out = {}
    for f in paths:
        try:
            out[f] = os.stat(f).st_mtime_ns
        except OSError:
            out[f] = None
    return out


def save_in_apps(shell, wins):
    """Have programs that save their own work save it, as the user would with Ctrl+S: windows that show
    unsaved changes, and every document window of the editors in QUIET_SAVE. Only windows that name their
    file in the title: an untitled document would open a file dialog. The focus goes back afterwards.
    Returns {seq: "saved" | "nothing to save" | reason it was not saved}."""
    results, pending = {}, {}
    focused = next((w["seq"] for w in wins if w["focused"]), None)
    for w in wins:
        if not w.get("pid"):
            continue
        app = saves_itself(cmdline(w["pid"]))
        marked = unsaved(w["title"])
        if not app or not (marked or app in QUIET_SAVE):
            continue
        files = files_from_title(w["title"], read_proc(w["pid"], "cwd"))
        before = _mtimes(files)  # before the keys: a quick app may have saved by the time they return
        if not files:
            if marked:
                results[w["seq"]] = f"{app} has no file for it yet: save it once in {app} to give it a name"
        elif shell.save_window(w["seq"]):
            pending[w["seq"]] = (app, marked, before)
        else:
            results[w["seq"]] = "the window could not be focused, or keys were still held down"
    start = time.monotonic()
    while pending:
        titles = {w["seq"]: w["title"] for w in shell.windows()}
        for seq, (app, marked, before) in list(pending.items()):
            if seq not in titles:
                results[seq] = "the window closed while saving"
            elif marked and not unsaved(titles[seq]) or _mtimes(before) != before:
                results[seq] = "saved"
            elif not marked and time.monotonic() - start > QUIET_WAIT:
                results[seq] = "nothing to save"
            else:
                continue
            del pending[seq]
        if not pending or time.monotonic() - start > SAVE_WAIT:
            break
        time.sleep(0.1)
    for seq, (app, *_) in pending.items():
        results[seq] = f"{app} did not finish saving (it may be asking something in its window)"
    if focused is not None and (results or pending):
        shell.activate(focused)
    return results


def _load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def package_of(exe):
    """Where the program at exe comes from and its version: {"source", "package", "version"} or None.
    Answers are cached per file and its modification time (dpkg -S is slow)."""
    try:
        exe = os.path.realpath(exe)
        key = f"{exe}:{os.stat(exe).st_mtime_ns}"
    except OSError:
        return None
    cache = _load_cache()
    if key in cache:
        return cache[key]
    info = None
    m = re.match(r"/snap/([^/]+)/", exe)
    if m:
        out = _run(["snap", "list", m.group(1)]) or ""
        rows = [r.split() for r in out.splitlines()[1:]]
        if rows and len(rows[0]) > 1:
            info = {"source": "snap", "package": m.group(1), "version": rows[0][1]}
    elif exe.lower().endswith(".appimage"):
        info = {"source": "appimage", "package": os.path.basename(exe), "version": None}
    else:
        out = _run(["dpkg-query", "-S", exe])
        if out:
            pkg = out.split(":", 1)[0].split(",")[0].strip()
            ver = _run(["dpkg-query", "-W", "-f", "${Version}", pkg])
            info = {"source": "deb", "package": pkg, "version": ver}
    cache = {k: v for k, v in cache.items() if not k.startswith(exe + ":")}
    cache[key] = info
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return info


def _major(version):
    m = re.match(r"(?:\d+:)?(\d+)", version or "")
    return int(m.group(1)) if m else None


def available_version(info):
    """The newer version waiting for this package, None if it is up to date, or "unknown"."""
    if info["source"] == "deb":
        out = _run(["apt-cache", "policy", info["package"]]) or ""
        cand = re.search(r"Candidate:\s*(\S+)", out)
        if not cand or cand.group(1) == "(none)":
            return "unknown"
        return None if cand.group(1) == info["version"] else cand.group(1)
    if info["source"] == "snap":
        out = _run(["snap", "refresh", "--list"], timeout=30)
        if out is None:
            return "unknown"
        row = next((r.split() for r in out.splitlines()[1:] if r.split()[:1] == [info["package"]]), None)
        return row[1] if row and len(row) > 1 else None
    return "unknown"


def update_command(info):
    if info["source"] == "deb":
        return f"sudo apt update && sudo apt install --only-upgrade {info['package']}"
    if info["source"] == "snap":
        return f"snap refresh {info['package']}"
    return None


def apt_lists_age():
    """Days since the package lists were last downloaded (apt-cache only knows that far)."""
    try:
        return (time.time() - max(p.stat().st_mtime for p in Path("/var/lib/apt/lists").glob("*Release*"))) / 86400
    except (OSError, ValueError):
        return None


def check_update(app, info, frozen=False):
    """What an update to this program means for the user's work: {app, package, installed, available,
    risk, why, command}. risk is "high" when frozen sessions of the program (frozen=True) would no longer
    restore, "medium" for a new major version (its file or session format may change), else "low"."""
    new = available_version(info)
    row = {"app": app, "source": info["source"], "package": info["package"], "installed": info["version"],
           "available": new, "risk": None, "why": None, "command": update_command(info)}
    if new is None:
        row["why"] = "up to date"
        return row
    if new == "unknown":
        row["why"] = "could not tell whether a newer version is waiting"
        return row
    if frozen:
        row["risk"], row["why"] = "high", (f"frozen sessions of {app} were saved with this version; after the "
                                           "update they may not restore. Stop and save that work in the "
                                           "program first, or keep the old version until you no longer need them.")
    elif _major(new) != _major(info["version"]):
        row["risk"], row["why"] = "medium", ("a new major version: its file or session format may change. Save "
                                             "your work first (a state ID keeps a copy of the open documents).")
    else:
        row["risk"], row["why"] = "low", "a minor update; open documents and sessions are not expected to change"
    return row


def programs(wins):
    """(app name, executable path) of the windows' programs, each program once."""
    seen = {}
    for w in wins:
        cmd = cmdline(w["pid"]) if w.get("pid") else []
        exe = read_proc(w["pid"], "exe") if w.get("pid") else None
        if not exe and cmd:
            exe = shutil.which(cmd[0])
        if exe:
            seen.setdefault(exe, saves_itself(cmd) or w.get("app_name") or os.path.basename(exe))
    return [(app, exe) for exe, app in seen.items()]
