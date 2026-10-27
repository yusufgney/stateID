"""What changed between two states, for an agent: windows opened and closed, and per window the
title, documents, directory, tabs, pages and text cursor. Works on agent views (core.agent_view),
so private windows and masked secrets stay hidden here too."""

MAX_ITEMS = 15


def _title(v):
    return v["window"].get("title") or ""


def _label(v):
    return f"{v['app'] or '?'} — private window" if v.get("private") else f"{v['app'] or '?'} — {_title(v)}"


def _places(v):
    return set(v.get("files") or []) | ({v["cwd"]} if v.get("cwd") else set())


def match(old, new):
    """Pair windows of two states: same app, then the same title, else shared documents or
    directory, else the remaining windows of that app in order. Returns (pairs, opened, closed)."""
    left, pairs = list(old), []
    for test in (lambda a, b: not a.get("private") and _title(a) == _title(b),
                 lambda a, b: bool(_places(a) & _places(b)),
                 lambda a, b: True):
        for b in new:
            if any(b is p[1] for p in pairs):
                continue
            a = next((a for a in left if a["app"] == b["app"] and bool(a.get("private")) == bool(b.get("private"))
                      and test(a, b)), None)
            if a is not None:
                left.remove(a)
                pairs.append((a, b))
    paired = [p[1] for p in pairs]
    return pairs, [b for b in new if not any(b is p for p in paired)], left


def _item(t):
    if not isinstance(t, dict):
        return str(t)
    if t.get("private"):
        return "[private tab]"
    if t.get("url"):
        return f"{t.get('title') or ''} <{t['url']}>".strip()
    return " ".join(str(t[k]) for k in ("title", "cwd", "command", "path") if t.get(k)) or str(t)


def _added_removed(name, a, b, out):
    a, b = [_item(x) for x in a or []], [_item(x) for x in b or []]
    added, removed = [x for x in b if x not in a], [x for x in a if x not in b]
    for word, xs in (("opened", added), ("closed", removed)):
        if xs:
            more = f" (+{len(xs) - MAX_ITEMS} more)" if len(xs) > MAX_ITEMS else ""
            out.append(f"{name} {word}: " + "; ".join(xs[:MAX_ITEMS]) + more)


def changes(a, b):
    """Readable differences between two views of the same window."""
    if a.get("private") or b.get("private"):
        return []  # nothing about a private window's content
    out = []
    if _title(a) != _title(b):
        out.append(f"title: {_title(a)!r} → {_title(b)!r}")
    if a.get("cwd") != b.get("cwd"):
        out.append(f"directory: {a.get('cwd')} → {b.get('cwd')}")
    _added_removed("documents", a.get("files"), b.get("files"), out)
    if bool(a.get("unsaved")) != bool(b.get("unsaved")):
        out.append("now has unsaved changes" if b.get("unsaved") else "changes were saved")
    wa, wb = a["window"], b["window"]
    if wa.get("workspace") != wb.get("workspace"):
        out.append(f"moved to workspace {wb.get('workspace')}")
    if wa.get("minimized") != wb.get("minimized"):
        out.append("minimized" if wb.get("minimized") else "unminimized")
    sa = a.get("app_state") if isinstance(a.get("app_state"), dict) else {}
    sb = b.get("app_state") if isinstance(b.get("app_state"), dict) else {}
    _added_removed("tabs", sa.get("tabs"), sb.get("tabs"), out)
    ta, tb = sa.get("tabs") or [], sb.get("tabs") or []
    ia, ib = sa.get("active_tab"), sb.get("active_tab")
    if isinstance(ia, int) and isinstance(ib, int) and ib < len(tb) and (ia >= len(ta) or _item(ta[ia]) != _item(tb[ib])):
        out.append(f"active tab: {_item(tb[ib])}")
    docs_a = {d.get("path"): d for d in sa.get("documents") or [] if isinstance(d, dict)}
    for d in sb.get("documents") or []:
        old = docs_a.get(d.get("path")) if isinstance(d, dict) else None
        if old and old.get("page") != d.get("page") and d.get("page") is not None:
            out.append(f"{d['path']}: page {(old.get('page') or 0) + 1} → {d['page'] + 1}")
    for key in sorted(set(sa) | set(sb)):
        va, vb = sa.get(key), sb.get(key)
        if key in ("tabs", "active_tab", "documents", "screen") or va == vb:
            continue
        if isinstance(va, (str, int, float, bool, type(None))) and isinstance(vb, (str, int, float, bool, type(None))):
            out.append(f"{key}: {va!r} → {vb!r}")
    ca = [(c["line"], c["column"]) for c in (a.get("ui") or {}).get("carets", [])]
    cb = [(c["line"], c["column"]) for c in (b.get("ui") or {}).get("carets", [])]
    if ca != cb and cb:
        out.append("text cursor: " + ", ".join(f"line {l}, column {c}" for l, c in cb))
    return out


def report(old_id, old_created, old, new_id, new_created, new):
    """Markdown for an agent. old/new are lists of agent views."""
    pairs, opened, closed = match(old, new)
    changed = [(b, c) for a, b in pairs if (c := changes(a, b))]
    lines = [f"# Changes from {old_id} to {new_id or 'now'}", "",
             f"{old_id} was saved {old_created}; {new_id or 'the current desktop was read'} {new_created}."
             if new_id else f"{old_id} was saved {old_created}; compared with the desktop at {new_created}.", ""]
    if not (opened or closed or changed):
        lines.append("Nothing changed in the open windows.")
        return "\n".join(lines)
    if opened:
        lines += [f"## Opened ({len(opened)})", *[f"- {_label(v)}" for v in opened], ""]
    if closed:
        lines += [f"## Closed ({len(closed)})", *[f"- {_label(v)}" for v in closed], ""]
    if changed:
        lines.append(f"## Changed ({len(changed)})")
        for v, cs in changed:
            lines += ["", f"### {_label(v)}", *[f"- {c}" for c in cs]]
        lines.append("")
    same = len(pairs) - len(changed)
    if same:
        lines.append(f"{same} other window(s) unchanged.")
    return "\n".join(lines).rstrip() + "\n"
