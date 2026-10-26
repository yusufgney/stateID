"""Brave, Chrome, Chromium: every tab is in the browser's own SNSS session file; no extension needed."""
import glob
import os
import re
import struct
from pathlib import Path

from .base import Adapter

BROWSERS = {
    "brave": ("~/.config/BraveSoftware/Brave-Browser", "brave-browser", "Brave"),
    "chrome": ("~/.config/google-chrome", "google-chrome", "Google Chrome"),
    "chromium": ("~/.config/chromium", "chromium", "Chromium"),
}


def snss_windows(path):
    """Parse a Chromium SNSS session file into [{tabs: [{url, title, pinned}], active}]."""
    data = Path(path).read_bytes()
    if data[:4] != b"SNSS":
        return []
    pos, tabs, wins = 8, {}, {}

    def tab(t):
        return tabs.setdefault(t, {"window": None, "index": 0, "navs": {}, "sel": 0, "closed": False, "pinned": False})

    def win(w):
        return wins.setdefault(w, {"selected": 0, "closed": False})

    while pos + 2 <= len(data):
        size, = struct.unpack_from("<H", data, pos)
        pos += 2
        if size == 0 or pos + size > len(data):
            break
        cid, p = data[pos], data[pos + 1:pos + size]
        pos += size
        try:
            if cid == 0:  # SetTabWindow
                w, t = struct.unpack_from("<ii", p); tab(t)["window"] = w; win(w)
            elif cid == 2:  # SetTabIndexInWindow
                t, i = struct.unpack_from("<ii", p); tab(t)["index"] = i
            elif cid == 6:  # UpdateTabNavigation (pickled)
                t, idx, n = struct.unpack_from("<iii", p, 4)
                url = p[16:16 + n].decode("utf-8", "replace")
                o = 16 + ((n + 3) & ~3)
                n16, = struct.unpack_from("<i", p, o)
                tab(t)["navs"][idx] = (url, p[o + 4:o + 4 + 2 * n16].decode("utf-16-le", "replace"))
            elif cid == 7:  # SetSelectedNavigationIndex
                t, i = struct.unpack_from("<ii", p); tab(t)["sel"] = i
            elif cid == 8:  # SetSelectedTabInIndex
                w, i = struct.unpack_from("<ii", p); win(w)["selected"] = i
            elif cid == 12:  # SetPinnedState
                t, b = struct.unpack_from("<i?", p); tab(t)["pinned"] = b
            elif cid == 16:  # TabClosed
                tab(struct.unpack_from("<i", p)[0])["closed"] = True
            elif cid == 17:  # WindowClosed
                win(struct.unpack_from("<i", p)[0])["closed"] = True
        except struct.error:
            pass
    out = []
    for w, wd in wins.items():
        if wd["closed"]:
            continue
        ts = sorted(((td["index"], td) for td in tabs.values() if td["window"] == w and not td["closed"] and td["navs"]),
                    key=lambda x: x[0])
        items = []
        for _, td in ts:
            url, title = td["navs"].get(td["sel"]) or td["navs"][max(td["navs"])]
            items.append({"url": url, "title": title, "pinned": td["pinned"]})
        if items:
            out.append({"tabs": items, "active": min(wd["selected"], len(items) - 1)})
    return out


class Chromium(Adapter):
    pattern = re.compile(r"brave|google-chrome|chromium", re.I)

    def _browser(self, w):
        key = (w["app_id"] or w["wm_class"] or "").lower()
        return next((v for k, v in BROWSERS.items() if k in key), BROWSERS["chrome"])

    def capture(self, w, st):
        base, exe, suffix = self._browser(w)
        title = re.sub(rf" - {re.escape(suffix)}( - .*)?$", "", w["title"] or "")
        best = None
        for f in sorted(glob.glob(os.path.expanduser(base) + "/*/Sessions/Session_*"), key=os.path.getmtime, reverse=True):
            for win in snss_windows(f):
                if win["tabs"][win["active"]]["title"] == title:
                    best = (f, win)
                    break
            if best:
                break
        if not best:
            st["app_state"] = {"active_tab_title": title, "warning": "tab list not in the session files yet"}
            return
        f, win = best
        profile = Path(f).parent.parent.name
        st["app_state"] = {"profile": profile, "active_tab": win["active"], "tabs": win["tabs"]}
        urls = [t["url"] for t in win["tabs"] if not t["url"].startswith(("chrome://newtab", "about:blank"))]
        st["restore_cmd"] = [exe, f"--profile-directory={profile}", "--new-window"] + urls
