"""Ptyxis and GNOME Terminal: each tab's directory, running command and screen text."""
import os
import re

from .. import a11y
from ..procs import HOME, cmdline, descendants, foreground, read_proc, tty_of
from .base import Adapter


class Terminal(Adapter):
    pattern = re.compile(r"ptyxis|gnome-terminal|org\.gnome\.terminal|kgx|console", re.I)

    def capture(self, w, st):
        root = a11y.frame(w["pid"], w["title"])
        shells = []
        for pid in descendants(w["pid"], 3):
            tty, stat = tty_of(pid), read_proc(pid, "stat")
            ppid = int(stat.rsplit(")", 1)[1].split()[1]) if stat else 0
            if not tty or tty_of(ppid) == tty:
                continue  # keep only the top process on each tty (the shell)
            fg = foreground(pid)
            shells.append({"pid": pid, "tty": tty, "cwd": read_proc(fg, "cwd") or read_proc(pid, "cwd"),
                           "command": " ".join(cmdline(fg))})
        titles, subtitle = [], None
        if root is not None:
            for path, node, role, name, ss in a11y.walk(root):
                if role == "terminal":
                    parent = node.get_parent() and node.get_parent().get_parent()
                    titles.append((parent.get_name() if parent else "") or w["title"])
                elif role == "label" and name.startswith(("~", "/")) and subtitle is None:
                    subtitle = name

        def score(s, title):  # does the shell's command or directory show in the tab title?
            cmd = os.path.basename(s["command"].split(" ")[0]) if s["command"] else ""
            d = s["cwd"].replace(str(HOME), "~") if s["cwd"] else ""
            return (bool(cmd) and cmd in title) * 2 + (bool(d) and d in title)

        titles = titles or [w["title"]]
        shells.sort(key=lambda s: int(s["tty"].rsplit("/", 1)[1]))
        if w.get("app_window_count", 1) <= 1:
            mine = shells  # the only window of this terminal app owns every shell, hidden tabs too
        else:  # several windows share one process: keep the shells that match this window's visible tabs
            mine = []
            for title in titles:
                cand = sorted((s for s in shells if s not in mine), key=lambda s: score(s, title), reverse=True)
                if cand and (score(cand[0], title) or len(titles) == 1):
                    mine.append(cand[0])
        tabs = []
        for s in mine:
            title = max(titles, key=lambda t: score(s, t))
            tabs.append({"title": title if score(s, title) else None, "cwd": s["cwd"], "running": s["command"]})
        st["app_state"] = {"tabs": tabs or [{"title": t, "cwd": None, "running": None} for t in titles]}
        if subtitle:
            st["app_state"]["current_dir_label"] = subtitle
        if (st.get("ui") or {}).get("terminals"):
            st["app_state"]["screen"] = st["ui"]["terminals"][-1]["text"][-2500:]
        dirs = [t["cwd"] or str(HOME) for t in st["app_state"]["tabs"]] or [str(HOME)]
        if "ptyxis" in (w["app_id"] or "").lower():
            st["restore_cmd"] = ["ptyxis", "--new-window", "-d", dirs[0]]
            st["restore_more"] = [["ptyxis", "--tab", "-d", d] for d in dirs[1:]]
        else:
            cmd = ["gnome-terminal", "--window", f"--working-directory={dirs[0]}"]
            for d in dirs[1:]:
                cmd += ["--tab", f"--working-directory={d}"]
            st["restore_cmd"] = cmd
