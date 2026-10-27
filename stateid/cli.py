"""The `stateid` command."""
import argparse
import json
import os
import sys

from . import __version__, core, freeze, privacy, store
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
  stateid desktop                 save the whole desktop, copy the ID
  stateid save --focused          snapshot only the window you are using
  stateid list                    find an ID
  stateid DK-7SZKSJV0             go back to that moment
  stateid show DK-7SZKSJV0        what an agent should read to continue

privacy: password managers and private/incognito windows are never captured;
  `stateid config --init` writes ~/.config/stateid/config.toml where you can
  exclude more apps, hide windows or browser tabs from agents, turn off
  screenshots or terminal text and delete old states automatically.
  Secrets (tokens, passwords, keys) are masked in everything agents read.

AI agents: `stateid-mcp` is an MCP server with the same features, e.g.
  claude mcp add --scope user stateid -- stateid-mcp

files: states live in ~/.local/share/stateid/<ID>/ (state.json, AGENT.md, windowN.png);
set STATEID_DIR to use another folder."""


def cmd_save(args):
    print(core.save(window=args.window, focused=args.focused, copy=args.copy)["id"])


def cmd_desktop(args):
    result = core.save(copy=not args.no_copy)
    print(result["id"] + ("" if args.no_copy else "    (copied to the clipboard)"))
    for w in result["windows"]:
        print(f"  • {w['app']}" + ("" if w.get("private") else f" — {w['title']}"))
    if result["excluded_windows"]:
        print(f"  ({result['excluded_windows']} window(s) skipped by your privacy settings)")
    print(f"\nGo back to this moment with: stateid {result['id']}")


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
    print(f"\nAgent summary: stateid show {report['id']}")


def cmd_show(args):
    print(core.summary(args.id))


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


def cmd_config(args):
    if args.init:
        if privacy.init_config():
            print(f"wrote {privacy.CONFIG_PATH}; edit it to choose what is excluded or kept private")
        else:
            print(f"{privacy.CONFIG_PATH} already exists; not overwritten")
        return
    exists = privacy.CONFIG_PATH.exists()
    print(f"# {privacy.CONFIG_PATH}" + ("" if exists else " (not created yet: defaults below; `stateid config --init`)"))
    print(json.dumps(privacy.load(), indent=2))


def cmd_run(args):
    if not args.command:
        sys.exit("usage: stateid run [--geometry WxH] PROGRAM [ARGS...]")
    cfg = privacy.load()
    if privacy.matches(cfg["privacy"]["exclude"], os.path.basename(args.command[0]), " ".join(args.command)):
        sys.exit(f"{args.command[0]} is excluded by your privacy settings; it will not be frozen")
    s = freeze.start(args.command, geometry=args.geometry)
    freeze.show(s)
    print(f"session {s['sid']} on display :{s['display']} — saved with its memory by "
          "`stateid save` / `stateid desktop`")


def cmd_sessions(args):
    if args.stop:
        freeze.stop(args.stop)
        print(f"stopped {args.stop}")
        return
    data = freeze.sessions()
    for s in data["sessions"]:
        shown = "shown" if freeze.viewer_pid(s["sid"]) else "hidden"
        print(f"{s['sid']}  :{s['display']}  {shown:6}  {' '.join(s['argv'])[:60]}")
    if not data["sessions"]:
        print("No freezable sessions running. Start one with `stateid run PROGRAM`.")
    if data["images"]:
        total = sum(m["bytes"] for m in data["images"]) / 2**20
        print(f"\n{len(data['images'])} frozen image(s), {total:.0f} MB")


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

    s = sub.add_parser("desktop", help="save the whole desktop and copy its ID (same as Super+Shift+S)",
                       description="Snapshot every open window, copy the new state ID to the clipboard and list "
                                   "what was saved. Same as `stateid save --copy`, with a summary.")
    s.add_argument("--no-copy", action="store_true", help="do not touch the clipboard; only print the ID")
    s.set_defaults(fn=cmd_desktop)

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

    s = sub.add_parser("run", help="start a program whose memory can be saved (unsaved work included)",
                       description="Start PROGRAM in a freezable session: it runs on its own virtual display "
                                   "and is shown in a viewer window. `stateid save` then freezes its whole "
                                   "memory, so restoring brings back unsaved changes too. Needs the freeze "
                                   "helper (`install.sh --with-freeze`).")
    s.add_argument("--geometry", default="1600x900", help="virtual screen size (default 1600x900)")
    s.add_argument("command", nargs=argparse.REMAINDER, help="the program and its arguments")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("sessions", help="list freezable sessions",
                       description="List the sessions started with `stateid run` and the frozen images.")
    s.add_argument("--stop", metavar="SID", help="stop a session (unsaved work in it is lost)")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("config", help="show the privacy and storage settings",
                       description="Show the effective settings from ~/.config/stateid/config.toml: windows never "
                                   "captured (exclude), windows hidden from agents (private), private browser tabs, "
                                   "screenshots, terminal text, secret redaction and automatic clean-up.")
    s.add_argument("--init", action="store_true", help="write a commented config file to start from")
    s.set_defaults(fn=cmd_config)
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
    except (ShellUnavailable, store.StateNotFound, LookupError, privacy.ConfigError,
            freeze.HelperUnavailable, freeze.HelperError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
