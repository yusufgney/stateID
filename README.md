# stateid

[![CI](https://github.com/yusufgney/stateID/actions/workflows/ci.yml/badge.svg)](https://github.com/yusufgney/stateID/actions/workflows/ci.yml)

**Shared memory of your desktop for you and your AI agents.** stateid gives the current state of your desktop a short ID, like `DK-7SZKSJV0`. An agent can read that ID to see exactly what you were doing and carry on, and you can paste it into a terminal to bring the moment back.

The ID stands for what was open and where you were in it: documents and the page you were on, browser tabs, folders, terminal directories, the text cursor, window placement and a screenshot of every window.

## For AI agents (MCP)

`stateid-mcp` is an [MCP](https://modelcontextprotocol.io) server, so any MCP client (Claude Code, Claude Desktop, Cursor, …) can use your desktop as context. Register it once:

```bash
claude mcp add --scope user stateid -- ~/.local/bin/stateid-mcp
```

Then just talk to the agent:

- *"Save my desktop"*: the agent takes a snapshot and gets an ID.
- *"Continue from DK-7SZKSJV0"*: the agent reads what was open, where you were and the screenshots, then picks up the work.
- *"What am I working on?"*: the agent lists the windows open right now.
- *"Restore DK-7SZKSJV0"*: the agent reopens and arranges everything.

| Tool | What it does |
|---|---|
| `list_windows` | The windows open right now |
| `save_state` | Snapshot the desktop or one window; returns an ID |
| `get_state` | A state's summary plus window screenshots |
| `list_states` | Saved states, newest first |
| `restore_state` | Bring a state back on screen (only when you ask) |
| `delete_state` | Delete a state |

Without MCP, any agent that can run commands can use `stateid show ID`, which prints the same summary as Markdown.

## Install

Currently for GNOME on Wayland (Ubuntu 26.04 or similar). It needs `python3-gi`, `gir1.2-atspi-2.0` and `gir1.2-gdkpixbuf-2.0`.

```bash
git clone https://github.com/yusufgney/stateID.git ~/stateid
~/stateid/install.sh                 # add --with-freeze for freezable sessions (asks for sudo)
```

Log out and back in once so GNOME Shell loads the extension. `~/stateid/uninstall.sh` removes it.

## Use it yourself

Save with **Super+Shift+S**, from a window's title-bar menu, or with `stateid desktop`. The ID is copied to the clipboard.

```
stateid desktop             save the whole desktop
stateid restore ID          bring the moment back (or just: stateid ID)
stateid list | show ID | delete ID
stateid run PROGRAM         start a program whose unsaved work is saved too
stateid config [--init]     privacy settings
```

## What it captures

- **Any app:** windows, position and workspace, open documents, working directory, text cursor and a screenshot.
- **Deeper app state:** OnlyOffice (slide, pen, slideshow), Chromium browsers (tabs), terminals (directories, resumed Claude Code sessions), Nautilus, Evince and VS Code. More apps, other Linux desktops and Windows 11 are planned.
- **Unsaved work (experimental):** a program started with `stateid run` has its whole memory frozen with [CRIU](https://criu.org) when you save, so unsaved changes come back on restore.

## Privacy

Agents see only what your settings allow. In `~/.config/stateid/config.toml` you can exclude apps, hide windows or tabs from agents, keep screenshots away from them, and mask secrets such as tokens and passwords. States are stored in your home folder, readable only by you.

## Development

```bash
.venv/bin/python -m pytest
```

CI runs tests, lint, a secret scan and a dependency audit on every push.

## License

MIT
