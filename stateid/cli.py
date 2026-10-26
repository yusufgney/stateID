"""The `stateid` command."""
import argparse
import sys

from . import __version__, core, store
from .shell import ShellUnavailable

DESCRIPTION = """\
stateid gives the current state of your open windows a short ID (like DK-7SZKSJV0)
and brings that exact moment back later: the same apps, documents, pages, tools,
tabs, folders, cursor positions and window placement. Hand the ID to an AI agent
and it can read what you were doing with `stateid show`.

Ways to create an ID without the terminal:
  * right-click a window's title bar, or Super + right-click anywhere in a window,
    and choose "Copy state ID of this window" / "... of the whole desktop"
  * press Super+Shift+S (whole desktop)
The ID is copied to the clipboard and a notification shows it."""

EPILOG = """\
what is saved, per window:
  every app       title, placement, workspace, screenshot, working directory,
                  open documents (from the process, the title and recent files),
                  pressed tool buttons, selected tabs, scroll positions,
                  text-field contents and text cursor (line/column)
  OnlyOffice      every open document, current slide/page, active toolbar tab,
                  selected pen and its colour, eraser, running slideshow + its pen
  Brave/Chrome    all tabs of the window with their URLs and the active tab
  Ptyxis/Terminal every tab's directory, the command running in it, screen text
  Nautilus        the folder shown
  Evince          document and page
  VS Code         project folder and active file

examples:
  stateid save --copy             snapshot the whole desktop, copy the ID
  stateid save --focused          snapshot only the window you are using
  stateid list                    find an ID
  stateid DK-7SZKSJV0             go back to that moment
  stateid show DK-7SZKSJV0        what an agent should read to continue

AI agents: `stateid-mcp` is an MCP server with the same features, e.g.
  claude mcp add --scope user stateid -- stateid-mcp

files: states live in ~/.local/share/stateid/<ID>/ (state.json, AGENT.md, windowN.png);
set STATEID_DIR to use another folder."""


def cmd_save(args):
    print(core.save(window=args.window, focused=args.focused, copy=args.copy)["id"])


def cmd_restore(args):
    report = core.restore(args.id)
    for w in report["windows"]:
        print(f"• {w['app']} — {w['title']}")
        print(f"  {w['status']}")
        for line in w["launched"]:
            print(f"  launched: {line}")
        if w["actions"]:
            print("  restored: " + "; ".join(w["actions"]))
        for note in w["notes"]:
            print(f"  note: {note}")
    print(f"\nAgent summary: {report['agent_summary']}")


def cmd_show(args):
    print(store.agent_summary(args.id))


def cmd_list(args):
    rows = store.list_states(args.n)
    if not rows:
        print("No saved states yet. Create one with `stateid save`.")
    for r in rows:
        names = ", ".join(f"{w['app']}: {w['title']}"[:60] for w in r["windows"][:2])
        print(f"{r['id']}  {r['created'].replace('T', ' ')}  {len(r['windows']):>2} window(s)  {names}")


def cmd_delete(args):
    for kid in args.id:
        print("deleted:", store.delete(kid))


def build_parser():
    p = argparse.ArgumentParser(prog="stateid", description=DESCRIPTION, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>", title="commands")

    s = sub.add_parser("save", help="snapshot the open windows and print a new state ID",
                       description="Snapshot open windows (all of them by default) and print the new state ID. "
                                   "Each window gets a screenshot plus everything stateid can read about the "
                                   "app's internal state.")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--window", type=int, metavar="SEQ",
                   help="only the window with this sequence number (as used by the window menu)")
    g.add_argument("--focused", action="store_true", help="only the window that has the keyboard focus")
    s.add_argument("--copy", action="store_true", help="also copy the ID to the clipboard and show a notification")
    s.set_defaults(fn=cmd_save)

    s = sub.add_parser("restore", help="bring a saved moment back (shortcut: stateid ID)",
                       description="Reopen what is closed, reuse what is already open, put every window back in "
                                   "place and re-apply the saved app state (pages, tools, tabs, cursor...). "
                                   "Commands that were running in terminals are printed, not re-run.")
    s.add_argument("id", help="a state ID such as DK-7SZKSJV0")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("show", help="print the summary an agent should read to continue the work",
                       description="Print AGENT.md for a state: windows, documents, app state, terminal screens "
                                   "and paths to the screenshots.")
    s.add_argument("id", help="a state ID")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("list", help="list saved states, newest first",
                       description="List saved states, newest first, with the first windows of each.")
    s.add_argument("-n", type=int, default=20, metavar="N", help="how many to show (default: 20)")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("delete", help="delete saved states", description="Delete one or more saved states.")
    s.add_argument("id", nargs="+", help="state IDs")
    s.set_defaults(fn=cmd_delete)
    return p


def main():
    argv = sys.argv[1:]
    if len(argv) == 1 and store.ID_RE.match(argv[0].strip().upper()):
        argv = ["restore", argv[0]]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return
    try:
        args.fn(args)
    except (ShellUnavailable, store.StateNotFound, LookupError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
