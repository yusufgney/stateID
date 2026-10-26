"""Small adapters: Nautilus (folder), Evince (page), VS Code (project and active file)."""
import glob
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from ..procs import HOME
from .base import Adapter


class Nautilus(Adapter):
    pattern = re.compile(r"nautilus", re.I)

    def capture(self, w, st):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            r = bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                              "org.freedesktop.DBus.Properties", "Get",
                              GLib.Variant("(ss)", ("org.freedesktop.FileManager1", "OpenWindowsWithLocations")),
                              None, 0, 3000, None).unpack()[0]
        except GLib.Error:
            return
        for locs in r.values():
            paths = [unquote(urlparse(u).path) for u in locs]
            if paths and (os.path.basename(paths[0].rstrip("/")) == w["title"] or paths[0] == str(HOME) and w["title"] == "Home"):
                st["app_state"] = {"locations": paths}
                st["restore_cmd"] = ["nautilus", "--new-window", paths[0]]
                return


class Evince(Adapter):
    pattern = re.compile(r"evince|papers", re.I)

    def capture(self, w, st):
        docs = st["files"]
        if not docs:
            return
        page = None
        texts = (st.get("ui") or {}).get("texts", [])
        for t in sorted(texts, key=lambda t: t["name"] != "page-label-entry"):  # the page entry in the header bar
            if re.fullmatch(r"\d+", t["text"].strip()):
                page = int(t["text"])
                break
        st["app_state"] = {"document": docs[0], "page": page}
        st["restore_cmd"] = ["evince"] + (["-i", str(page)] if page else []) + [docs[0]]


class VSCode(Adapter):
    pattern = re.compile(r"^code|visual studio code", re.I)

    def capture(self, w, st):
        title = re.sub(r"\s*[-—]\s*Visual Studio Code.*$", "", w["title"] or "")
        parts = [p.strip("● ").strip() for p in re.split(r"\s+[-—]\s+", title) if p.strip()]
        folder_name = parts[-1] if parts else None
        best = None
        for wsj in glob.glob(str(HOME / ".config/Code/User/workspaceStorage/*/workspace.json")):
            try:
                uri = json.loads(Path(wsj).read_text()).get("folder") or ""
            except Exception:
                continue
            path = unquote(urlparse(uri).path)
            if folder_name and os.path.basename(path.rstrip("/")) == folder_name:
                mtime = os.path.getmtime(Path(wsj).parent)
                if not best or mtime > best[0]:
                    best = (mtime, path)
        active = parts[0] if len(parts) > 1 else None
        active_path = None
        if best and active:
            for root, dirs, files in os.walk(best[1]):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "build", "dist")]
                if active in files:
                    active_path = os.path.join(root, active)
                    break
        st["app_state"] = {"folder": best[1] if best else None, "active_file": active_path or active}
        if best:
            st["restore_cmd"] = ["code", best[1]] + (["-g", active_path] if active_path else [])
