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
                     "windows": [{"app": s["app"], "private": True} if hidden(s) else
                                 {"app": s["app"], "title": privacy.redact(s["window"]["title"])} for s in d["windows"]]})
    rows.sort(key=lambda r: r["created"], reverse=True)
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


def agent_md(kid, states, created, excluded=()):
    """Markdown summary for agents. `states` must already be agent views (see core.agent_view)."""
    lines = [f"# State {kid}", "",
             f"Saved {created}. This is the user's desktop at that moment. Windows have screenshots "
             "(`windowN.png`) in this folder; look at them for anything not captured as data.", "",
             f"Restore with: `stateid restore {kid}`", ""]
    if excluded:
        lines += [f"{len(excluded)} window(s) were not captured because of the user's privacy settings.", ""]
    for i, st in enumerate(states, 1):
        if st.get("private"):
            lines += [f"## {i}. {st['app']} — private window", "",
                      "- Hidden by the user's privacy settings; restore still brings it back. "
                      "Do not try to find out what it contains.", ""]
            continue
        w, r = st["window"], st["window"]["rect"]
        lines += [f"## {i}. {st['app']} — {w['title']}", ""]
        lines.append(f"- Placement: workspace {w['workspace']}, {r['width']}x{r['height']} at {r['x']},{r['y']}"
                     + (" (maximized)" if w["maximized"] else "") + (" (minimized)" if w["minimized"] else "")
                     + (" — **focused window**" if w["focused"] else ""))
        if st.get("unsaved"):
            lines.append("- **Has unsaved changes** (title marker)")
        if st["cwd"]:
            lines.append(f"- Working directory: `{st['cwd']}`")
        if st["files"]:
            lines.append("- Open files: " + ", ".join(f"`{f}`" for f in st["files"][:15]))
        a = dict(st["app_state"]) if isinstance(st["app_state"], dict) else {}
        screen = a.pop("screen", None)
        for d in a.get("documents", []):
            desc = f"slide/page {d['page'] + 1}/{d.get('pages')}" if d.get("page") is not None else ""
            if d.get("pen"):
                desc += f", pen {d['pen']['index'] + 1} selected (color {d['pen']['color']})"
            if d.get("slideshow"):
                ss = d["slideshow"]
                desc += f", SLIDESHOW running at slide {ss['slide'] + 1}" + (f" with {ss['tool']} #{ss['color']}" if ss.get("tool") else "")
            lines.append(f"- {'**active** ' if d.get('active') else ''}`{d['path']}`: {desc}")
        if a.get("tabs") and "active_tab" in a:
            t = a["tabs"][a["active_tab"]]
            lines.append(f"- Browser: {len(a['tabs'])} tabs, active: {t['title']} <{t['url']}>")
        if a:
            lines.append("- App state:")
            lines += ["  ```json", *["  " + l for l in json.dumps(a, ensure_ascii=False, indent=1).splitlines()[:80]], "  ```"]
        ui = st.get("ui") or {}
        for c in ui.get("carets", []):
            lines.append(f"- Text cursor: line {c['line']}, column {c['column']}" + (" (focused)" if c["focused"] else ""))
        if ui.get("toggles"):
            lines.append("- Active tools/toggles: " + ", ".join(f"`{t['name']}`" for t in ui["toggles"][:20] if t["name"]))
        if ui.get("tabs"):
            lines.append("- Selected tabs: " + ", ".join(f"`{t['name']}`" for t in ui["tabs"]))
        if ui.get("texts"):
            lines.append("- Text fields: " + ", ".join(f"`{t['name'] or '?'}`=`{t['text'][:80]}`" for t in ui["texts"][:10]))
        if screen:
            lines += ["- Terminal screen (end):", "  ```", *["  " + l for l in screen.splitlines()[-40:]], "  ```"]
        if st.get("screenshot"):
            lines.append(f"- Screenshot: `{STORE / kid / st['screenshot']}`")
        lines.append("")
    return "\n".join(lines)
