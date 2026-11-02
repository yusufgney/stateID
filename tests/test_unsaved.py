"""Unsaved work: said plainly when a state does not hold it, and freezing kept for work that cannot be saved."""
import pytest

from stateid import apps, cli, core, freeze, privacy, store


def test_save_says_unsaved_changes_are_not_in_the_state(desk):
    desk.titles = ["• notes.txt", "b.txt"]
    r = core.save()
    assert r["windows"][0]["unsaved"] and "unsaved" not in r["windows"][1]
    md = (store.STORE / r["id"] / "AGENT.md").read_text()
    assert "Has unsaved changes** (title marker): they are not in this state" in md
    now = core.now()
    assert "Has unsaved changes** (title marker)" in now and "not in this state" not in now


def test_a_frozen_session_keeps_its_unsaved_work():
    assert store.unsaved_lost({"unsaved": True, "app_state": None})
    assert not store.unsaved_lost({"unsaved": True, "app_state": {"frozen_image": "img000000001"}})
    assert not store.unsaved_lost({"unsaved": False, "app_state": None})


def test_programs_that_save_themselves():
    assert apps.saves_itself(["/usr/bin/soffice", "--writer"]) == "LibreOffice"
    assert apps.saves_itself(["gimp-2.10", "x.png"]) == "GIMP"
    assert apps.saves_itself(["python3", "train.py"]) is None
    assert apps.saves_itself(["PacketTracer"]) is None


def test_run_refuses_a_program_that_saves_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "CONFIG_PATH", tmp_path / "config.toml")
    started = []
    monkeypatch.setattr(freeze, "start", lambda argv, **kw: started.append(argv) or {"sid": "s", "display": 1})
    monkeypatch.setattr(freeze, "show", lambda s: None)
    args = cli.build_parser().parse_args(["run", "gimp", "x.png"])
    with pytest.raises(SystemExit, match="GIMP saves its own work"):
        args.fn(args)
    assert not started
    args = cli.build_parser().parse_args(["run", "--anyway", "gimp", "x.png"])
    args.fn(args)
    assert started == [["gimp", "x.png"]]
