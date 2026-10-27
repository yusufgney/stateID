"""Differences between two states, as agents read them. No display needed."""
from stateid import diff


def view(app, title, **kw):
    v = {"app": app, "window": {"title": title, "workspace": 0, "minimized": False}, "files": [], "cwd": None,
         "app_state": None, "ui": None}
    v.update(kw)
    return v


def test_opened_closed_and_unchanged():
    old = [view("Editor", "a.txt"), view("Files", "Home")]
    new = [view("Editor", "a.txt"), view("Brave", "News")]
    md = diff.report("DK-AAAAAAAA", "t1", old, None, "t2", new)
    assert "## Opened (1)\n- Brave — News" in md
    assert "## Closed (1)\n- Files — Home" in md
    assert "1 other window(s) unchanged." in md and "compared with the desktop" in md


def test_window_whose_title_changed_is_matched_by_its_documents():
    old = [view("Editor", "a.txt", files=["/p/a.txt"])]
    new = [view("Editor", "• a.txt", files=["/p/a.txt", "/p/b.txt"], unsaved=True)]
    pairs, opened, closed = diff.match(old, new)
    assert len(pairs) == 1 and not opened and not closed
    cs = diff.changes(*pairs[0])
    assert "title: 'a.txt' → '• a.txt'" in cs and "documents opened: /p/b.txt" in cs
    assert "now has unsaved changes" in cs


def test_tabs_pages_and_cursor():
    a = view("Brave", "x", app_state={"tabs": [{"title": "A", "url": "https://a"}], "active_tab": 0,
                                      "documents": [{"path": "/d.pptx", "page": 2}]},
             ui={"carets": [{"line": 3, "column": 1}]})
    b = view("Brave", "x", app_state={"tabs": [{"title": "A", "url": "https://a"}, {"title": "B", "url": "https://b"}],
                                      "active_tab": 1, "documents": [{"path": "/d.pptx", "page": 5}]},
             ui={"carets": [{"line": 9, "column": 4}]})
    cs = diff.changes(a, b)
    assert "tabs opened: B <https://b>" in cs and "active tab: B <https://b>" in cs
    assert "/d.pptx: page 3 → 6" in cs and "text cursor: line 9, column 4" in cs


def test_private_windows_reveal_nothing():
    a = {"app": "Bank", "private": True, "window": {"title": None}}
    b = {"app": "Bank", "private": True, "window": {"title": None}}
    assert diff.changes(a, b) == []
    md = diff.report("DK-AAAAAAAA", "t1", [a], "DK-BBBBBBBB", "t2", [])
    assert "Bank — private window" in md


def test_private_tabs_stay_masked():
    a = view("Brave", "x", app_state={"tabs": []})
    b = view("Brave", "x", app_state={"tabs": [{"title": "[private tab]", "url": "[private tab]", "private": True}]})
    assert diff.changes(a, b) == ["tabs opened: [private tab]"]


def test_nothing_changed():
    md = diff.report("DK-AAAAAAAA", "t1", [view("Editor", "a")], "DK-BBBBBBBB", "t2", [view("Editor", "a")])
    assert "Nothing changed" in md
