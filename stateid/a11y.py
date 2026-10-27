"""Generic UI state through the accessibility tree (AT-SPI): works for any app whose toolkit exposes it."""
import re
import time

try:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib
except (ImportError, ValueError):  # no AT-SPI here (another platform, or a minimal install): no UI state
    Atspi = GLib = None

if Atspi:
    Atspi.set_timeout(1500, 1500)
S = Atspi.StateType if Atspi else None
TOGGLE_ROLES = {"toggle button", "radio button", "check box", "push button"}
VALUE_ROLES = {"scroll bar", "slider", "spin button"}
TEXT_ROLES = {"entry", "spin button"}
SKIP_TOGGLE = re.compile(r"menu|close|minimi|maximi|search|show open tabs", re.I)


def frame(pid, title):
    """The AT-SPI frame of a window: same pid, best title match."""
    if Atspi is None:
        return None
    try:
        desk = Atspi.get_desktop(0)
        best = None
        for i in range(desk.get_child_count()):
            app = desk.get_child_at_index(i)
            if not app or app.get_process_id() != pid:
                continue
            for j in range(app.get_child_count()):
                f = app.get_child_at_index(j)
                if not f:
                    continue
                if (f.get_name() or "").strip() == (title or "").strip():
                    return f
                best = best or f
        return best
    except GLib.Error:
        return None


def text_of(node, limit):
    t = node.get_text_iface()
    if not t:
        return None
    n = Atspi.Text.get_character_count(t)
    return Atspi.Text.get_text(t, max(0, n - limit), n)


def walk(root, max_nodes=5000, seconds=3.0):
    """Yield (path, node, role, name, states) for showing nodes; path is a stable-ish signature."""
    end = time.time() + seconds
    stack = [(root, ())]
    count = 0
    while stack and count < max_nodes and time.time() < end:
        node, path = stack.pop()
        count += 1
        try:
            role, name = node.get_role_name(), node.get_name() or ""
            ss = node.get_state_set()
            if not ss.contains(S.SHOWING) and node is not root:
                continue
            yield path, node, role, name, ss
            seen = {}
            kids = []
            for i in range(node.get_child_count()):
                c = node.get_child_at_index(i)
                if c is None:
                    continue
                key = (c.get_role_name(), c.get_name() or "")
                seen[key] = seen.get(key, -1) + 1
                kids.append((c, path + (key + (seen[key],),)))
            stack.extend(reversed(kids))
        except GLib.Error:
            continue


def is_editor(role, ss):
    return role in ("text", "document text", "terminal") and ss.contains(S.MULTI_LINE) and ss.contains(S.EDITABLE)


def capture(root, terminal_chars=4000):
    """Pressed tools, selected tabs, scroll/slider values, field texts, editor cursors, terminal screens."""
    if root is None:
        return None
    toggles, tabs, values, texts, carets, terminals = [], [], [], [], [], []
    for path, node, role, name, ss in walk(root):
        on = ss.contains(S.CHECKED) or ss.contains(S.PRESSED)
        if role in TOGGLE_ROLES and on and not ss.contains(S.HAS_POPUP) and not SKIP_TOGGLE.search(name):
            toggles.append({"path": path, "role": role, "name": name})
        elif role == "page tab" and ss.contains(S.SELECTED):
            tabs.append({"path": path, "name": name})
        if role in VALUE_ROLES and (v := node.get_value_iface()):
            values.append({"path": path, "role": role, "name": name, "value": Atspi.Value.get_current_value(v),
                           "max": Atspi.Value.get_maximum_value(v)})
        if role == "terminal" and (t := text_of(node, terminal_chars)) is not None:
            terminals.append({"text": t.rstrip()})
        elif is_editor(role, ss) and (ti := node.get_text_iface()):
            caret = Atspi.Text.get_caret_offset(ti)
            n = Atspi.Text.get_character_count(ti)
            before = Atspi.Text.get_text(ti, 0, max(0, caret))
            carets.append({"path": path, "caret": caret, "chars": n, "line": before.count("\n") + 1,
                           "column": len(before) - (before.rfind("\n") + 1) + 1,
                           "focused": ss.contains(S.FOCUSED)})
        elif (role in TEXT_ROLES or role == "text") and ss.contains(S.EDITABLE) \
                and (t := text_of(node, 300)) and "\n" not in t:  # GTK3 entries have role "text"
            texts.append({"name": name, "text": t})
    return {k: v for k, v in (("toggles", toggles), ("tabs", tabs), ("values", values), ("texts", texts),
                              ("carets", carets), ("terminals", terminals)) if v}


def apply(root, ui):
    """Re-press tools, reselect tabs, restore scroll positions and text cursors. Returns what was done."""
    if root is None or not ui:
        return []
    want = {tuple(map(tuple, t["path"])): ("toggle", t) for t in ui.get("toggles", [])}
    want.update({tuple(map(tuple, t["path"])): ("tab", t) for t in ui.get("tabs", [])})
    want.update({tuple(map(tuple, v["path"])): ("value", v) for v in ui.get("values", []) if v["role"] != "spin button"})
    editors, done = [], []
    for path, node, role, name, ss in walk(root):
        if is_editor(role, ss):
            editors.append((path, node))
        hit = want.get(path)
        if not hit:
            continue
        kind, rec = hit
        try:
            if kind == "toggle" and not (ss.contains(S.CHECKED) or ss.contains(S.PRESSED)):
                if (act := node.get_action_iface()) and Atspi.Action.do_action(act, 0):
                    done.append(f"pressed '{name}'")
            elif kind == "tab" and not ss.contains(S.SELECTED):
                if (act := node.get_action_iface()) and Atspi.Action.do_action(act, 0):
                    done.append(f"selected tab '{name}'")
            elif kind == "value" and (v := node.get_value_iface()):
                cur_max = Atspi.Value.get_maximum_value(v)
                target = rec["value"] * cur_max / rec["max"] if rec.get("max") and cur_max else rec["value"]
                Atspi.Value.set_current_value(v, target)
        except GLib.Error:
            pass
    # text cursors: same path if the layout is identical, otherwise the editors in order
    for i, c in enumerate(ui.get("carets", [])):
        node = next((n for p, n in editors if p == tuple(map(tuple, c["path"]))), None)
        if node is None and i < len(editors):
            node = editors[i][1]
        if node is None or c["caret"] <= 0 or not (ti := node.get_text_iface()):
            continue
        try:
            if Atspi.Text.get_character_count(ti) >= c["caret"] and Atspi.Text.set_caret_offset(ti, c["caret"]):
                done.append(f"cursor at line {c['line']}, column {c['column']}")
        except GLib.Error:
            pass
    return done
