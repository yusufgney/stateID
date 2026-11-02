"""Programs that save their own work: saving through them, the documents a state keeps, versions and updates."""
import os

from stateid import apps, core, procs, store


def editor(monkeypatch, cwd):
    monkeypatch.setattr(procs, "recent_files", lambda: [])  # never the user's real recent documents
    monkeypatch.setattr(apps, "cmdline", lambda pid: ["gnome-text-editor"])
    monkeypatch.setattr(apps, "read_proc", lambda pid, name: str(cwd))


def test_titled_documents_are_saved_in_their_app_untitled_ones_are_not(desk, tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("hello")
    editor(monkeypatch, tmp_path)
    desk.pid = 4321
    desk.titles = ["b.txt", "• notes.txt", "• New Document"]
    res = apps.save_in_apps(desk, desk.windows())
    assert res[2] == "saved" and "has no file for it yet" in res[3] and 1 not in res
    assert desk.saved == [2] and desk.titles[1] == "notes.txt"
    assert desk.activated == [1]  # the focus goes back to where the user was


def test_an_app_that_does_not_save_is_reported(desk, tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("hello")
    editor(monkeypatch, tmp_path)
    monkeypatch.setattr(apps, "SAVE_WAIT", 0.2)
    desk.pid, desk.saves = 4321, False
    desk.titles = ["• notes.txt"]
    assert "did not finish saving" in apps.save_in_apps(desk, desk.windows())[1]


def test_other_programs_are_never_sent_ctrl_s(desk, tmp_path, monkeypatch):
    monkeypatch.setattr(apps, "cmdline", lambda pid: ["python3", "train.py"])
    desk.pid = 4321
    desk.titles = ["* notes.txt"]
    assert apps.save_in_apps(desk, desk.windows()) == {} and desk.saved == []


def test_save_reports_what_was_saved_in_the_app(desk, monkeypatch):
    def fake(shell, wins):
        shell.titles[0] = "notes.txt"
        return {1: "saved"}
    monkeypatch.setattr(apps, "save_in_apps", fake)
    desk.titles = ["• notes.txt"]
    r = core.save(save_apps=True)
    assert r["windows"][0]["saved_in_app"] == "saved" and "unsaved" not in r["windows"][0]
    assert r["windows"][0]["title"] == "notes.txt"
    assert "Saved with the app's own save just before this state" in (store.STORE / r["id"] / "AGENT.md").read_text()
    monkeypatch.setattr(apps, "save_in_apps", lambda *a: (_ for _ in ()).throw(AssertionError("auto saved apps")))
    core.save(auto=True, save_apps=True)  # automatic saves never write the user's files


def test_a_state_keeps_its_documents_and_gives_them_back(desk, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DOC_FILE_LIMIT", 100)
    doc, big = tmp_path / "doc.txt", tmp_path / "big.bin"
    doc.write_text("version 1")
    big.write_bytes(b"x" * 200)
    (tmp_path / "link.txt").symlink_to(doc)
    st = {"files": [str(doc), str(big), str(tmp_path / "link.txt"), str(tmp_path / "gone.txt")]}
    d = store.STORE / "DK-AAAAAAAA"
    d.mkdir(parents=True)
    store.copy_docs(d, [st])
    assert [x["path"] for x in st["docs"]] == [str(doc)] and st["docs_skipped"] == [str(big)]
    assert (d / st["docs"][0]["copy"]).read_text() == "version 1"
    assert store.changed_docs("DK-AAAAAAAA", st) == []
    doc.write_text("version 2")
    [(path, copy)] = store.changed_docs("DK-AAAAAAAA", st)
    assert path == str(doc) and open(copy).read() == "version 1" and doc.read_text() == "version 2"
    assert oct(os.stat(copy).st_mode & 0o777) == "0o600"


def test_update_risk(monkeypatch):
    info = {"source": "deb", "package": "gimp", "version": "1:2.10.38-1"}
    for new, frozen, risk in (("1:3.0.4-1", False, "medium"), ("1:2.10.40-1", False, "low"),
                              ("1:2.10.40-1", True, "high")):
        monkeypatch.setattr(apps, "available_version", lambda i, n=new: n)
        row = apps.check_update("GIMP", info, frozen=frozen)
        assert row["risk"] == risk and row["available"] == new
        assert row["command"] == "sudo apt update && sudo apt install --only-upgrade gimp"
    monkeypatch.setattr(apps, "available_version", lambda i: None)
    assert apps.check_update("GIMP", info)["risk"] is None


def test_package_lookups_are_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(apps, "CACHE", tmp_path / "versions.json")
    exe = tmp_path / "prog"
    exe.write_text("")
    calls = []

    def run(cmd, timeout=10):
        calls.append(cmd[0])
        return f"prog: {exe}\n" if "-S" in cmd else "1.2-3"
    monkeypatch.setattr(apps, "_run", run)
    want = {"source": "deb", "package": "prog", "version": "1.2-3"}
    assert apps.package_of(str(exe)) == want and apps.package_of(str(exe)) == want
    assert len(calls) == 2


def test_editors_without_a_title_marker_are_saved_too(desk, tmp_path, monkeypatch):
    doc = tmp_path / "notes.txt"
    doc.write_text("hello")
    editor(monkeypatch, tmp_path)  # GNOME Text Editor shows no unsaved marker in its window title
    monkeypatch.setattr(apps, "QUIET_WAIT", 0.2)
    desk.pid = 4321
    desk.titles = ["notes.txt - Text Editor"]
    assert apps.save_in_apps(desk, desk.windows()) == {1: "nothing to save"}

    def saves(seq):
        os.utime(doc, ns=(1, 1))
        return True
    monkeypatch.setattr(desk, "save_window", saves)
    assert apps.save_in_apps(desk, desk.windows()) == {1: "saved"}
