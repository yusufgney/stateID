"""save / restore / inspect: the operations shared by the CLI and the MCP server. Nothing here prints."""
import json
import os
import re
import threading
import time
from datetime import datetime

from . import __version__, a11y, store
from .adapters import adapter_for
from .procs import (cmdline, descendants, desktop_command, file_arg_docs, files_from_title, read_proc, spawn,
                    user_files)
from .shell import Shell

WINDOW_WAIT = 30  # seconds to wait for a relaunched app's window
# AT-SPI and the D-Bus proxy are not meant for concurrent use; the MCP server runs tools in threads.
_lock = threading.RLock()


def _summary(w):
    return {"seq": w["seq"], "app": w["app_name"] or w["wm_class"], "title": w["title"], "focused": w["focused"],
            "minimized": w["minimized"], "workspace": w["workspace"]}


def list_windows():
    with _lock:
        return [_summary(w) for w in Shell().windows()]


def describe_window(w):
    pid = w["pid"]
    cmd = cmdline(pid) if pid > 0 else []
    cwd = read_proc(pid, "cwd") if pid > 0 else None
    pids = [pid] + descendants(pid, 2) if pid > 0 else []
    st = {
        "window": w,
        "app": w["app_name"] or w["wm_class"],
        "command": cmd,
        "cwd": cwd,
        "files": list(dict.fromkeys(files_from_title(w["title"], cwd) + file_arg_docs(cmd) + user_files(pids))),
        "unsaved": bool(re.match(r"^\s*[•*●]", w["title"] or "")),
        "app_state": None,
        "restore_cmd": None,
    }
    try:
        st["ui"] = a11y.capture(a11y.frame(pid, w["title"])) if pid > 0 else None
    except Exception as e:
        st["ui"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        adapter_for(w).capture(w, st)
    except Exception as e:  # an adapter failing must not lose the snapshot
        st["app_state"] = {"error": f"{type(e).__name__}: {e}"}
    return st


def save(window=None, focused=False, copy=False, screenshots=True):
    """Snapshot windows (all, one by sequence number, or the focused one). Returns the new state's summary."""
    with _lock:
        shell = Shell()
        wins = shell.windows()
        for w in wins:
            w["app_window_count"] = sum(o["app_id"] == w["app_id"] for o in wins)
        if window is not None:
            wins = [w for w in wins if w["seq"] == window]
        elif focused:
            wins = [w for w in wins if w["focused"]]
        if not wins:
            raise LookupError("No matching window")
        kid = store.new_id()
        d = store.STORE / kid
        d.mkdir(parents=True)
        states = []
        for i, w in enumerate(sorted(wins, key=lambda w: (not w["focused"], -w["user_time"]))):
            st = describe_window(w)
            png = f"window{i + 1}.png"
            if screenshots and shell.capture(w["seq"], d / png):
                st["screenshot"] = png
            states.append(st)
        created = datetime.now().isoformat(timespec="seconds")
        scope = "window" if window is not None or focused else "desktop"
        (d / "state.json").write_text(json.dumps(
            {"id": kid, "version": __version__, "created": created, "scope": scope, "windows": states},
            ensure_ascii=False, indent=1))
        (d / "AGENT.md").write_text(store.agent_md(kid, states, created))
        if copy:
            shell.copy(kid)
            names = ", ".join(s["app"] or "?" for s in states[:3]) + ("…" if len(states) > 3 else "")
            shell.notify(f"State ID copied: {kid}", f"{len(states)} window(s): {names}")
        return {"id": kid, "created": created, "scope": scope,
                "windows": [{"app": s["app"], "title": s["window"]["title"], "files": s["files"][:5]} for s in states]}


def _launch(st, adapter):
    """Reopen a closed window. Returns (launched command lines, notes)."""
    launched = adapter.launch(st)
    if launched:
        return launched, []
    if st.get("restore_cmd"):
        launched = [spawn(st["restore_cmd"], st["cwd"])]
        for extra in st.get("restore_more", []):
            time.sleep(1.0)
            launched.append(spawn(extra, st["cwd"]))
        return launched, []
    docs = [f for f in st["files"] if os.path.exists(f)][:5] or files_from_title(st["window"]["title"], st["cwd"])
    cmd = desktop_command(st["window"]["app_id"], docs)
    if cmd:
        notes = [] if docs else ["no document was recorded for this window; the app opens empty (see the screenshot)"]
        return [spawn(cmd, st["cwd"])], notes
    if st["command"] and "--gapplication-service" not in st["command"]:
        return [spawn(st["command"], st["cwd"])], []
    return [], ["cannot reopen: no launcher found for this app"]


def _same_app(a, b):
    return (a["app_id"] and a["app_id"] == b["app_id"]) or (a["wm_class"] and a["wm_class"] == b["wm_class"])


def restore(kid):
    """Bring a saved moment back. Returns a per-window report."""
    data = store.load(kid)
    with _lock:
        shell = Shell()
        current = shell.windows()
        known = {c["seq"] for c in current}
        used = set()
        items = []  # dicts with private keys _st, _ad, _win
        for st in data["windows"]:
            w = st["window"]
            ad = adapter_for(w)
            same = [c for c in current if c["seq"] not in used and _same_app(c, w)]
            match = next((c for c in same if c["title"] == w["title"]), None)
            if match is None and ad.single_window and same:
                match = same[0]
            item = {"app": st["app"], "title": w["title"], "launched": [], "actions": [], "notes": [],
                    "_st": st, "_ad": ad, "_win": match}
            if match:
                used.add(match["seq"])
                item["status"] = "already open"
            else:
                item["launched"], item["notes"] = _launch(st, ad)
                item["status"] = "launched" if item["launched"] else "failed"
            items.append(item)

        # wait for relaunched windows
        deadline = time.time() + WINDOW_WAIT
        while any(i["_win"] is None and i["launched"] for i in items) and time.time() < deadline:
            time.sleep(0.5)
            for c in shell.windows():
                if c["seq"] in known:
                    continue
                for i in items:
                    if i["_win"] is None and i["launched"] and _same_app(c, i["_st"]["window"]):
                        i["_win"] = c
                        known.add(c["seq"])
                        break
        time.sleep(1.0)

        # put windows back and re-apply app state
        for i in items:
            st, ad, win = i["_st"], i["_ad"], i["_win"]
            w = st["window"]
            if win is None:
                if i["launched"]:
                    i["status"] = "window did not appear"
                    i["notes"].append(f"no window within {WINDOW_WAIT}s")
                continue
            if not w.get("fullscreen"):  # fullscreen windows (e.g. a slideshow) are recreated by the app
                shell.move_resize(win["seq"], w["rect"], w["maximized"])
            try:
                i["actions"] += ad.apply(st, win, shell)
            except Exception as e:
                i["notes"].append(f"app restore failed: {e}")
            if st.get("ui") and "error" not in st["ui"]:
                for _ in range(6):  # the document may still be loading
                    try:
                        fresh = next((c for c in shell.windows() if c["seq"] == win["seq"]), win)
                        got = a11y.apply(a11y.frame(fresh["pid"], fresh["title"]), st["ui"])
                    except Exception as e:
                        i["notes"].append(f"accessibility restore failed: {e}")
                        break
                    if got or not st["ui"].get("carets"):
                        i["actions"] += got
                        break
                    time.sleep(0.5)
            tabs = (st["app_state"] or {}).get("tabs", []) if isinstance(st["app_state"], dict) else []
            for t in tabs:
                if isinstance(t, dict) and t.get("running") and t.get("cwd") is not None \
                        and not re.fullmatch(r"-?(ba|z|fi)?sh", os.path.basename(t["running"].split()[0])):
                    i["notes"].append(f"was running in {t['cwd']}: {t['running']} (not re-run)")
            if st.get("unsaved"):
                i["notes"].append("had unsaved changes when saved; check the app's own draft recovery")

        focused = next((i for i in items if i["_st"]["window"]["focused"] and i["_win"]), None)
        if focused:
            shell.activate(focused["_win"]["seq"])
    for i in items:
        for k in ("_st", "_ad", "_win"):
            i.pop(k)
    return {"id": data["id"], "windows": items, "agent_summary": str(store.path(kid) / "AGENT.md")}


def screenshots(kid, max_width=1280):
    """[(window number, app, title, jpeg bytes)] for a saved state."""
    data = store.load(kid)
    out = []
    for n, st in enumerate(data["windows"], 1):
        if st.get("screenshot") and (img := store.screenshot(kid, st["screenshot"], max_width)):
            out.append((n, st["app"], st["window"]["title"], img))
    return out
