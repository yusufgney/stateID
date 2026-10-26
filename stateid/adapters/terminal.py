"""Ptyxis and GNOME Terminal: each tab's directory, running command and screen text.

Commands running in a tab are not re-run on restore, with one exception: an interactive
Claude Code session is reopened with `claude --resume <session id>`.
"""
import json
import os
import re
import shlex
import subprocess
import time

from .. import a11y
from ..procs import HOME, cmdline, descendants, foreground, read_proc, spawn, tty_of
from ..shell import Shell
from .base import Adapter

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHELLS = ("bash", "zsh", "fish")


def claude_session(pids):
    """(session id, argv) of the first pid that is an interactive Claude Code session.
    Claude Code writes ~/.claude/sessions/<pid>.json for every running session."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(HOME / ".claude")
    for pid in pids:
        try:
            with open(f"{base}/sessions/{pid}.json") as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        sid = info.get("sessionId", "")
        if SESSION_ID.match(sid) and info.get("kind", "interactive") == "interactive":
            return sid, cmdline(pid)
    return None, None


def resume_argv(running, session_id=None):
    """`claude [flags] --resume ID` from the saved command line, or None if it is not Claude Code."""
    argv = shlex.split(running or "")
    if not argv or os.path.basename(argv[0]) != "claude":
        return None
    flags, it = [], iter(argv[1:])
    for a in it:
        if a in ("--resume", "-r"):
            nxt = next(it, "")
            if SESSION_ID.match(nxt):
                session_id = session_id or nxt
            elif nxt:
                flags.append(nxt)
        elif a in ("--continue", "-c") or a.startswith("--resume="):
            session_id = session_id or (a.split("=", 1)[1] if "=" in a else None)
        elif a in ("-p", "--print"):
            return None  # one-shot run, nothing to resume
        else:
            flags.append(a)
    if not session_id or not SESSION_ID.match(session_id):
        return None
    return ["claude", *flags, "--resume", session_id]


def tab_command(tab):
    """What to run in a restored tab: resume Claude Code inside the user's shell, so the tab
    stays open when it exits. None means just a shell."""
    argv = resume_argv(tab.get("running"), tab.get("claude_session"))
    if not argv:
        return None
    sh = tab.get("shell") if tab.get("shell") in SHELLS else "bash"
    return [sh, "-ic", f"{shlex.join(argv)}; exec {sh}"]


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
            session, argv = claude_session([fg] + [p for p in descendants(pid, 2) if p != fg])
            sh = os.path.basename((cmdline(pid) or [""])[0]).lstrip("-")
            shells.append({"pid": pid, "tty": tty, "cwd": read_proc(fg, "cwd") or read_proc(pid, "cwd"),
                           "command": shlex.join(argv) if session else " ".join(cmdline(fg)),
                           "shell": sh, "claude_session": session})
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
            tab = {"title": title if score(s, title) else None, "cwd": s["cwd"], "running": s["command"],
                   "shell": s["shell"]}
            if s["claude_session"]:
                tab["claude_session"] = s["claude_session"]
            tabs.append(tab)
        st["app_state"] = {"tabs": tabs or [{"title": t, "cwd": None, "running": None} for t in titles]}
        if subtitle:
            st["app_state"]["current_dir_label"] = subtitle
        if (st.get("ui") or {}).get("terminals"):
            st["app_state"]["screen"] = st["ui"]["terminals"][-1]["text"][-2500:]

    def launch(self, st):
        """One window with every saved tab; Claude Code tabs resume their session."""
        tabs = [t for t in (st.get("app_state") or {}).get("tabs", []) if isinstance(t, dict)] or [{}]
        dirs = [t.get("cwd") if t.get("cwd") and os.path.isdir(t["cwd"]) else str(HOME) for t in tabs]
        cmds = [tab_command(t) for t in tabs]
        if "ptyxis" not in (st["window"]["app_id"] or "").lower():
            argv = ["gnome-terminal"]
            for i, (d, c) in enumerate(zip(dirs, cmds)):
                argv += ["--window" if i == 0 else "--tab", f"--working-directory={d}"]
                if c:
                    argv += ["-e", shlex.join(c)]
            return [spawn(argv)]

        def ptyxis(first, d, c):
            return ["ptyxis", "--new-window" if first else "--tab", "-d", d] + (["--", *c] if c else [])

        try:
            shell = Shell()
            before = {w["seq"] for w in shell.windows()}
        except Exception:
            shell = None
        launched = [spawn(ptyxis(True, dirs[0], cmds[0]))]
        if len(tabs) > 1:
            # --tab goes to the active window: wait for the new one instead of sleeping a fixed time
            deadline = time.time() + 8
            while shell and time.time() < deadline and not any(
                    w["seq"] not in before and "ptyxis" in (w["app_id"] or "").lower() and w["focused"]
                    for w in shell.windows()):
                time.sleep(0.1)
            for d, c in zip(dirs[1:], cmds[1:]):
                argv = ptyxis(False, d, c)
                try:  # the client hands the request to the running instance and exits, which keeps tab order
                    subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5, cwd=d)
                except subprocess.TimeoutExpired:
                    pass
                launched.append(shlex.join(argv))
        return launched
