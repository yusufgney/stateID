"""save / restore / inspect: the operations shared by the CLI and the MCP server. Nothing here prints."""
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime

from . import __version__, a11y, desktops, diff as diffs, freeze, privacy, store
from .adapters import adapter_for
from .adapters.terminal import resume_argv
from .procs import (cmdline, descendants, desktop_command, file_arg_docs, files_from_title, launch_argv, read_proc,
                    spawn, user_files)

WINDOW_WAIT = 30  # seconds to wait for a relaunched app's window
# AT-SPI and the D-Bus proxy are not meant for concurrent use; the MCP server runs tools in threads.
_lock = threading.RLock()


def _summary(w, cfg):
    p = cfg["privacy"]
    fields = privacy.window_fields(w)
    hidden = "excluded" if privacy.matches(p["exclude"], *fields) else \
        "private" if privacy.matches(p["private"], *fields) else None
    title = f"[{hidden} window]" if hidden else (privacy.redact(w["title"]) if p["redact"] else w["title"])
    return {"seq": w["seq"], "app": w["app_name"] or w["wm_class"], "title": title, "focused": w["focused"],
            "minimized": w["minimized"], "workspace": w["workspace"]}


def list_windows():
    """Windows open right now, as an agent may see them (private and excluded titles hidden)."""
    cfg = privacy.load()
    with _lock:
        return [_summary(w, cfg) for w in desktops.current().windows()]


def is_private(st, cfg):
    """Private if it was private when saved or matches the current settings (settings can only add privacy)."""
    return bool(st.get("private")) or privacy.matches(cfg["privacy"]["private"], *privacy.window_fields(st["window"]))


def agent_view(st, cfg):
    """What an agent may see of a saved window: private windows reduced to their app, secrets masked."""
    p = cfg["privacy"]
    w = st["window"]
    if is_private(st, cfg):
        return {"app": st["app"], "private": True,
                "window": {k: w[k] for k in ("workspace", "focused", "maximized", "minimized", "rect")}}
    v = json.loads(json.dumps(st))  # deep copy
    a = v.get("app_state")
    if isinstance(a, dict) and isinstance(a.get("tabs"), list):
        a["tabs"] = [{"title": "[private tab]", "url": "[private tab]", "private": True}
                     if isinstance(t, dict) and "url" in t
                     and (t.get("private") or privacy.matches(p["private_tabs"], t.get("title"), t.get("url")))
                     else t for t in a["tabs"]]
    if not p["terminal_text"]:
        (v.get("ui") or {}).pop("terminals", None)
        if isinstance(a, dict):
            a.pop("screen", None)
    v.pop("restore_cmd", None)
    v.pop("restore_more", None)
    if p["redact"]:
        for key in ("app_state", "ui", "command", "files", "cwd"):
            v[key] = privacy.redact_deep(v.get(key))
        v["window"]["title"] = privacy.redact(w["title"])
    return v


def summary(kid):
    """The agent summary of a state, rendered with the current privacy settings."""
    cfg = privacy.load()
    data = store.load(kid)
    views = [agent_view(st, cfg) for st in data["windows"]]
    return store.agent_md(data["id"], views, data["created"], data.get("excluded", []),
                          screenshots=cfg["privacy"]["agent_screenshots"])


def diff(old_id, new_id=None):
    """What changed from one state to another, or to the desktop right now (read, not saved).
    Rendered for agents, with the current privacy settings."""
    cfg = privacy.load()
    old = store.load(old_id)
    old_views = [agent_view(st, cfg) for st in old["windows"]]
    if new_id:
        new = store.load(new_id)
        new_views, created = [agent_view(st, cfg) for st in new["windows"]], new["created"]
    else:
        with _lock:
            wins = desktops.current().windows()
            for w in wins:
                w["app_window_count"] = sum(o["app_id"] == w["app_id"] for o in wins)
            wins = [w for w in wins if not privacy.matches(cfg["privacy"]["exclude"], *privacy.window_fields(w))]
            states = _describe_all(wins, cfg)
        new_views, created = [agent_view(st, cfg) for st in states], datetime.now().isoformat(timespec="seconds")
    return diffs.report(old["id"], old["created"], old_views, new_id and store.normalize(new_id), created, new_views)


def _lock_down(path):
    """States hold screenshots and terminal text: readable by the user only."""
    os.chmod(path, 0o700)
    for f in path.iterdir():
        os.chmod(f, 0o600)


def prune(keep_days):
    """Delete states older than keep_days (0 keeps everything). Returns the deleted IDs."""
    if not keep_days or keep_days <= 0:
        return []
    cutoff = datetime.now().timestamp() - keep_days * 86400
    gone = []
    for row in store.list_states(limit=None):
        try:
            if datetime.fromisoformat(row["created"]).timestamp() < cutoff:
                gone.append(store.delete(row["id"]))
        except (ValueError, store.StateNotFound):
            continue
    return gone


def prune_auto(keep):
    """Keep only the newest `keep` automatic states; states saved by hand are never touched."""
    autos = [r for r in store.list_states(limit=None) if r.get("auto")]
    gone = []
    for row in autos[max(0, keep):]:
        try:
            gone.append(store.delete(row["id"]))
        except store.StateNotFound:
            continue
    return gone


def unchanged(old, states, cfg):
    """True when the windows read now show nothing new against a saved state (as diff_states sees it)."""
    old_views = [agent_view(st, cfg) for st in old["windows"]]
    pairs, opened, closed = diffs.match(old_views, [agent_view(st, cfg) for st in states])
    return not (opened or closed or any(diffs.changes(a, b) for a, b in pairs))


def describe_window(w):
    pid = w["pid"]
    cmd = cmdline(pid) if pid > 0 else []
    cwd = read_proc(pid, "cwd") if pid > 0 else None
    pids = [pid] + descendants(pid, 2) if pid > 0 else []
    files = files_from_title(w["title"], cwd) + file_arg_docs(cmd) + user_files(pids)
    if pid > 0:
        cmd, cwd = launch_argv(pid, cmd, cwd)
    st = {
        "window": w,
        "app": w["app_name"] or w["wm_class"],
        "command": cmd,
        "cwd": cwd,
        "files": list(dict.fromkeys(files)),
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


def _describe_all(wins, cfg):
    states = []
    for w in wins:
        st = describe_window(w)
        _apply_privacy(st, cfg)
        states.append(st)
    return states


def _apply_privacy(st, cfg):
    """Mark private windows and tabs; drop or mask terminal text according to the settings."""
    p = cfg["privacy"]
    st["private"] = privacy.matches(p["private"], *privacy.window_fields(st["window"]))
    a = st.get("app_state")
    if isinstance(a, dict) and isinstance(a.get("tabs"), list):
        for t in a["tabs"]:
            if isinstance(t, dict) and "url" in t and privacy.matches(p["private_tabs"], t.get("title"), t.get("url")):
                t["private"] = True
    ui = st.get("ui") or {}
    if not p["terminal_text"]:
        ui.pop("terminals", None)
        if isinstance(a, dict):
            a.pop("screen", None)
    elif p["redact"]:  # the screen text is only for agents; never keep secrets from it on disk
        for t in ui.get("terminals", []):
            t["text"] = privacy.redact(t["text"])
        if isinstance(a, dict) and a.get("screen"):
            a["screen"] = privacy.redact(a["screen"])


def freeze_sessions(states):
    """Freeze the memory of every `stateid run` session among the saved windows. Runs after the
    screenshots, because the viewer window is closed and reopened around the freeze."""
    for st in states:
        a = st.get("app_state")
        if not (isinstance(a, dict) and a.get("freeze_session")):
            continue
        try:
            meta = freeze.freeze(a["freeze_session"])
            a["frozen_image"], a["frozen_bytes"] = meta["iid"], meta["bytes"]
        except (freeze.HelperUnavailable, freeze.HelperError) as e:
            a["freeze_error"] = str(e)


def save(window=None, focused=False, copy=False, screenshots=True, auto=False):
    """Snapshot windows (all, one by sequence number, or the focused one). Returns the new state's summary,
    already filtered for agents.

    auto: an automatic save of the whole desktop. It is skipped (returns None) when nothing changed since
    the last automatic state, does not freeze `stateid run` sessions and is pruned by [autosave] keep."""
    cfg = privacy.load()
    p = cfg["privacy"]
    with _lock:
        shell = desktops.current()
        wins = shell.windows()
        for w in wins:
            w["app_window_count"] = sum(o["app_id"] == w["app_id"] for o in wins)
        if window is not None:
            wins = [w for w in wins if w["seq"] == window]
        elif focused:
            wins = [w for w in wins if w["focused"]]
        if not wins:
            raise LookupError("No matching window")
        excluded = [w for w in wins if privacy.matches(p["exclude"], *privacy.window_fields(w))]
        wins = [w for w in wins if w not in excluded]
        if not wins:
            raise LookupError("Every matching window is excluded by the privacy settings "
                              f"({privacy.CONFIG_PATH})")
        states = _describe_all(sorted(wins, key=lambda w: (not w["focused"], -w["user_time"])), cfg)
        if auto:
            last = next((r for r in store.list_states(limit=None) if r.get("auto")), None)
            if last and unchanged(store.load(last["id"]), states, cfg):
                return None
        kid = store.new_id()
        store.STORE.mkdir(parents=True, exist_ok=True)
        os.chmod(store.STORE, 0o700)
        d = store.STORE / kid
        d.mkdir(mode=0o700)
        for i, st in enumerate(states):
            png = f"window{i + 1}.png"
            if screenshots and p["screenshots"] and shell.capture(st["window"]["seq"], d / png):
                st["screenshot"] = png
        if not auto:
            freeze_sessions(states)
        created = datetime.now().isoformat(timespec="seconds")
        scope = "window" if window is not None or focused else "desktop"
        excluded_apps = [w["app_name"] or w["wm_class"] for w in excluded]
        (d / "state.json").write_text(json.dumps(
            {"id": kid, "version": __version__, "created": created, "scope": scope, "desktop": shell.name,
             "auto": auto, "excluded": excluded_apps,
             "windows": states}, ensure_ascii=False, indent=1))
        views = [agent_view(st, cfg) for st in states]
        (d / "AGENT.md").write_text(store.agent_md(kid, views, created, excluded_apps,
                                                   screenshots=p["agent_screenshots"]))
        _lock_down(d)
        if copy:
            shell.copy(kid)
            names = ", ".join(s["app"] or "?" for s in states[:3]) + ("…" if len(states) > 3 else "")
            shell.notify(f"State ID copied: {kid}", f"{len(states)} window(s): {names}")
    pruned = prune(cfg["storage"]["keep_days"]) + (prune_auto(cfg["autosave"]["keep"]) if auto else [])
    return {"id": kid, "created": created, "scope": scope, "auto": auto, "excluded_windows": len(excluded_apps),
            "pruned_old_states": len(pruned),
            "windows": [{"app": v["app"], "private": True} if v.get("private") else
                        {"app": v["app"], "title": v["window"]["title"], "files": v["files"][:5]} for v in views]}


def _runnable(cmd):
    """Can this saved command still be started? (Programs in temporary or deleted places cannot.)"""
    if not cmd or "--gapplication-service" in cmd:
        return False
    prog = cmd[0]
    if os.path.isabs(prog):
        return os.access(prog, os.X_OK)
    return "/" not in prog and shutil.which(prog) is not None


def _launch(st, adapter):
    """Reopen a closed window. Returns (launched command lines, notes); never raises for a
    program that cannot be started, so one broken window does not stop the others."""
    notes = []
    try:
        launched = adapter.launch(st)
        if launched:
            return launched, []
        if st.get("restore_cmd") and _runnable(st["restore_cmd"]):
            launched = [spawn(st["restore_cmd"], st["cwd"])]
            for extra in st.get("restore_more", []):
                time.sleep(1.0)
                launched.append(spawn(extra, st["cwd"]))
            return launched, []
        w = st["window"]
        docs = [f for f in st["files"] if os.path.exists(f)][:5] or files_from_title(w["title"], st["cwd"])
        cmd = desktop_command(w["app_id"], docs, w.get("wm_class"))
        if cmd and _runnable(cmd):
            if not docs:
                notes.append("no document was recorded for this window; the app opens empty (see the screenshot)")
            return [spawn(cmd, st["cwd"])], notes
        if _runnable(st["command"]):
            return [spawn(st["command"], st["cwd"])], []
    except OSError as e:
        return [], [f"cannot reopen: {e}"]
    return [], ["cannot reopen: the program is no longer where it was and no launcher was found"]


def _same_app(a, b):
    return (a["app_id"] and a["app_id"] == b["app_id"]) or (a["wm_class"] and a["wm_class"] == b["wm_class"])


def restore(kid):
    """Bring a saved moment back. Returns a per-window report."""
    data = store.load(kid)
    with _lock:
        shell = desktops.current()
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
            time.sleep(0.2)
            for c in shell.windows():
                if c["seq"] in known:
                    continue
                for i in items:
                    if i["_win"] is None and i["launched"] and _same_app(c, i["_st"]["window"]):
                        i["_win"] = c
                        known.add(c["seq"])
                        break
        if any(i["launched"] for i in items):
            time.sleep(0.3)  # let new windows finish mapping before they are moved

        # put every window back first, so the desktop looks right at once; app state follows
        for i in items:
            w, win = i["_st"]["window"], i["_win"]
            if win is None:
                if i["launched"]:
                    i["status"] = "window did not appear"
                    i["notes"].append(f"no window within {WINDOW_WAIT}s")
            elif not w.get("fullscreen"):  # fullscreen windows (e.g. a slideshow) are recreated by the app
                shell.move_resize(win["seq"], w["rect"], w["maximized"])

        for i in items:
            st, ad, win = i["_st"], i["_ad"], i["_win"]
            if win is None:
                continue
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
                    time.sleep(0.3)
            tabs = (st["app_state"] or {}).get("tabs", []) if isinstance(st["app_state"], dict) else []
            for t in tabs:
                if not (isinstance(t, dict) and t.get("running") and t.get("cwd") is not None) \
                        or re.fullmatch(r"-?(ba|z|fi)?sh", os.path.basename(t["running"].split()[0])):
                    continue
                argv = resume_argv(t["running"], t.get("claude_session"))
                if argv and i["launched"]:
                    i["actions"].append(f"resumed Claude Code session {argv[-1]} in {t['cwd']}")
                elif argv:
                    i["notes"].append(f"Claude Code session {argv[-1]} was open in {t['cwd']}; "
                                      f"the terminal is still open, so it was not started again")
                else:
                    i["notes"].append(f"was running in {t['cwd']}: {t['running']} (not re-run)")
            if st.get("unsaved"):
                i["notes"].append("had unsaved changes when saved; check the app's own draft recovery")

        focused = next((i for i in items if i["_st"]["window"]["focused"] and i["_win"]), None)
        if focused:
            shell.activate(focused["_win"]["seq"])
    for i in items:
        for k in ("_st", "_ad", "_win"):
            i.pop(k)
    return {"id": data["id"], "windows": items}


def agent_safe_report(report):
    """A restore report fit for an agent: private windows anonymised, launch commands summarised
    (a browser command line holds every tab URL), the rest redacted."""
    cfg = privacy.load()
    data = store.load(report["id"])
    out = []
    for st, item in zip(data["windows"], report["windows"]):
        if is_private(st, cfg):
            out.append({"app": item["app"], "private": True, "status": item["status"]})
            continue
        r = dict(item, title=privacy.redact(item["title"]))
        r["launched"] = [line.split(" ", 1)[0] + (" …" if " " in line else "") for line in item["launched"]]
        r["actions"] = [privacy.redact(a) for a in item["actions"]]
        r["notes"] = [privacy.redact(n) for n in item["notes"]]
        out.append(r)
    return {"id": report["id"], "windows": out}


def screenshots(kid, max_width=1280):
    """[(window number, app, title, jpeg bytes)] an agent may see: none for private windows,
    none at all when agent_screenshots is off."""
    cfg = privacy.load()
    data = store.load(kid)
    if not cfg["privacy"]["agent_screenshots"]:
        return []
    out = []
    for n, st in enumerate(data["windows"], 1):
        if is_private(st, cfg):
            continue
        if st.get("screenshot") and (img := store.screenshot(kid, st["screenshot"], max_width)):
            title = privacy.redact(st["window"]["title"]) if cfg["privacy"]["redact"] else st["window"]["title"]
            out.append((n, st["app"], title, img))
    return out
