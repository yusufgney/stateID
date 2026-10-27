"""Saved states on disk: ~/.local/share/stateid/<ID>/{state.json, AGENT.md, windowN.png}."""
import json
import os
import re
import secrets
import io
import shutil
from pathlib import Path

from PIL import Image

from . import privacy

STORE = Path(os.environ.get("STATEID_DIR", Path.home() / ".local/share/stateid"))
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
ID_RE = re.compile(r"^DK-[0-9A-Z]{8}$")


class StateNotFound(LookupError):
    pass


def normalize(kid):
    """Validate an ID (it may come from a model or a user) so it can never escape the store."""
    kid = (kid or "").strip().upper()
    if not ID_RE.match(kid):
        raise StateNotFound(f"Not a state ID: {kid!r} (expected DK- followed by 8 characters)")
    return kid


def new_id():
    while True:
        n = secrets.randbits(40)
        kid = "DK-" + "".join(ALPHABET[(n >> (5 * i)) & 31] for i in range(8))
        if not (STORE / kid).exists():
            return kid


def path(kid):
    return STORE / normalize(kid)


def load(kid):
    f = path(kid) / "state.json"
    if not f.exists():
        raise StateNotFound(f"No such state: {normalize(kid)} (see `stateid list`)")
    return json.loads(f.read_text())


def list_states(limit=20):
    """Saved states, newest first; titles of private windows are hidden."""
    cfg = privacy.load()
    hidden = lambda s: s.get("private") or privacy.matches(cfg["privacy"]["private"], *privacy.window_fields(s["window"]))
    rows = []
    for f in STORE.glob("DK-*/state.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        rows.append({"id": d["id"], "created": d["created"], "scope": d.get("scope", "desktop"),
                     "auto": bool(d.get("auto")),
                     "windows": [{"app": s["app"], "private": True} if hidden(s) else
                                 {"app": s["app"], "title": privacy.redact(s["window"]["title"])} for s in d["windows"]],
                     "_mtime": f.stat().st_mtime_ns})
    rows.sort(key=lambda r: (r["created"], r["_mtime"]), reverse=True)  # mtime orders saves within a second
    for r in rows:
        del r["_mtime"]
    return rows[:limit]


def delete(kid):
    data = load(kid)
    from . import freeze  # frozen sessions saved in this state go with it
    for st in data["windows"]:
        iid = (st.get("app_state") or {}).get("frozen_image") if isinstance(st.get("app_state"), dict) else None
        if iid and freeze.available():
            try:
                freeze.call("delete", iid=iid)
            except (freeze.HelperUnavailable, freeze.HelperError):
                pass
    shutil.rmtree(path(kid))
    return normalize(kid)


def screenshot(kid, name, max_width=1280, quality=80):
    """A window screenshot scaled down and JPEG-encoded, small enough to hand to a model."""
    f = path(kid) / name
    if not re.fullmatch(r"window\d+\.png", name) or not f.exists():
        return None
    try:
        with Image.open(f) as im:
            im.load()
            if im.width > max_width:
                im = im.resize((max_width, round(im.height * max_width / im.width)), Image.Resampling.BILINEAR)
            if im.mode in ("RGBA", "LA", "P"):  # JPEG has no alpha; flatten onto white
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, "white")
                flat.paste(im, mask=im.getchannel("A"))
                im = flat
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=quality)
    except OSError:
        return None
    return out.getvalue()


def _app_lines(a):
    """Readable lines for the app state an adapter captured; unknown keys go last, compactly."""
    a, out = dict(a), []
    for note in ("warning", "error"):
        if a.get(note):
            out.append(f"- Note: {a.pop(note)}")
    tabs, active = a.pop("tabs", None), a.pop("active_tab", None)
    if isinstance(tabs, list) and tabs and any(isinstance(t, dict) and "url" in t for t in tabs):
        out.append(f"- Browser tabs ({len(tabs)}):")
        for i, t in enumerate(tabs[:20]):
            out.append(f"  - {'**active** ' if i == active else ''}{t.get('title') or '?'} <{t.get('url')}>")
        if len(tabs) > 20:
            out.append(f"  - … {len(tabs) - 20} more")
    elif isinstance(tabs, list) and tabs:
        out.append(f"- Terminal tabs ({len(tabs)}):")
        for t in tabs:
            line = f"  - `{t.get('cwd') or '?'}`"
            if t.get("running") and t["running"] != t.get("shell"):
                line += f" running `{t['running']}`"
            if t.get("claude_session"):
                line += f" (Claude Code session {t['claude_session']})"
            out.append(line)
    for d in a.pop("documents", None) or []:
        desc = f"slide/page {d['page'] + 1}/{d.get('pages')}" if d.get("page") is not None else ""
        if d.get("pen"):
            desc += f", pen {d['pen']['index'] + 1} selected (color {d['pen']['color']})"
        if d.get("slideshow"):
            ss = d["slideshow"]
            desc += f", SLIDESHOW running at slide {ss['slide'] + 1}" + (f" with {ss['tool']} #{ss['color']}" if ss.get("tool") else "")
        out.append(f"- {'**active** ' if d.get('active') else ''}`{d['path']}`: {desc}".rstrip(": "))
    if a.get("document"):
        page = a.pop("page", None)
        out.append(f"- Document `{a.pop('document')}`" + (f", page {page + 1}" if isinstance(page, int) else ""))
    if a.get("locations"):
        out.append("- Folders: " + ", ".join(f"`{x}`" for x in a.pop("locations")))
    if "folder" in a or "active_file" in a:
        folder, f = a.pop("folder", None), a.pop("active_file", None)
        out.append("- " + ", ".join(x for x in (folder and f"Project `{folder}`", f and f"active file `{f}`") if x))
    if a.get("freeze_session"):
        a.pop("freeze_session")
        prog = " ".join(a.pop("program", None) or [])
        out.append(f"- Freezable session running `{prog}`"
                   + ("; its memory (unsaved work included) was saved" if a.pop("frozen_image", None) else ""))
        a.pop("frozen_bytes", None)
    for k in ("profile", "current_dir_label", "screen"):
        a.pop(k, None)
    if a:
        text = json.dumps(a, ensure_ascii=False, separators=(", ", ": "))
        out.append(f"- Other app state: `{text[:600]}{'…' if len(text) > 600 else ''}`")
    return out


def agent_md(kid, states, created, excluded=(), screenshots=True):
    """Markdown summary for agents: the focused window first, then every window's documents,
    place in the app and screen text. `states` must already be agent views (see core.agent_view).
    Screenshot paths are listed only when the user lets agents see screenshots."""
    focus = next((st for st in states if st["window"].get("focused")), states[0] if states else None)
    lines = [f"# State {kid}", "", f"Saved {created}: {len(states)} window(s)."]
    if focus is not None:
        what = "a private window" if focus.get("private") else f"{focus['app']} — {focus['window']['title']}"
        lines.append(f"The user was working in **{what}**.")
    if excluded:
        lines.append(f"{len(excluded)} window(s) were not captured because of the user's privacy settings.")
    lines += ["", f"Restore: `stateid restore {kid}` · Changes since then: `stateid diff {kid}`", ""]
    if screenshots:
        lines += ["Each window has a screenshot; look at it for anything not captured as data.", ""]
    for i, st in enumerate(states, 1):
        w = st["window"]
        flags = [x for x, on in (("focused", w.get("focused")), ("maximized", w.get("maximized")),
                                 ("minimized", w.get("minimized"))) if on]
        where = f"workspace {w['workspace']}" if w.get("workspace") is not None else ""
        tags = ", ".join([x for x in [where] if x] + flags)
        if st.get("private"):
            lines += [f"## {i}. {st['app']} — private window", "",
                      "- Hidden by the user's privacy settings; restore still brings it back. "
                      "Do not try to find out what it contains.", ""]
            continue
        lines += [f"## {i}. {st['app']} — {w['title']}", ""]
        if tags:
            lines.append(f"- Window: {tags}")
        if st.get("unsaved"):
            lines.append("- **Has unsaved changes** (title marker)")
        if st["cwd"]:
            lines.append(f"- Working directory: `{st['cwd']}`")
        if st["files"]:
            lines.append("- Open files: " + ", ".join(f"`{f}`" for f in st["files"][:15]))
        a = st["app_state"] if isinstance(st["app_state"], dict) else {}
        lines += _app_lines(a)
        ui = st.get("ui") or {}
        for c in ui.get("carets", []):
            lines.append(f"- Text cursor: line {c['line']}, column {c['column']}" + (" (focused)" if c["focused"] else ""))
        if ui.get("toggles"):
            lines.append("- Active tools/toggles: " + ", ".join(f"`{t['name']}`" for t in ui["toggles"][:20] if t["name"]))
        if ui.get("tabs"):
            lines.append("- Selected tabs: " + ", ".join(f"`{t['name']}`" for t in ui["tabs"]))
        if ui.get("texts"):
            lines.append("- Text fields: " + ", ".join(f"`{t['name'] or '?'}`=`{t['text'][:80]}`" for t in ui["texts"][:10]))
        if a.get("screen"):
            lines += ["- Terminal screen (end):", "  ```", *["  " + l for l in a["screen"].splitlines()[-30:]], "  ```"]
        if screenshots and st.get("screenshot"):
            lines.append(f"- Screenshot: `{STORE / kid / st['screenshot']}`")
        lines.append("")
    return "\n".join(lines)
