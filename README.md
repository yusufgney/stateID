# stateid

Give the current state of your desktop a short ID, like `DK-7SZKSJV0`, and bring that exact moment back later.

The ID stands for what was open and where you were in it: the slide you were on, the selected red pen, the browser tabs, the folder, the terminal directories and the line your cursor was on. Paste the ID into a terminal to restore it, or give it to an AI agent so it can read what you were doing and carry on.

```
$ stateid save --copy
DK-7SZKSJV0                      # also on the clipboard

$ stateid DK-7SZKSJV0            # later: reopen and restore everything
• ONLYOFFICE — lecture.pptx — ONLYOFFICE
  already open
  ONLYOFFICE: lecture.pptx: page 30, slideshow at slide 30, pen #E81416
• Text Editor — settings.yml (~/project) - Text Editor
  launched: gnome-text-editor /home/me/project/settings.yml
  Text Editor: cursor at line 6, column 8
```

## Requirements

- GNOME Shell 50 on Wayland (Ubuntu 26.04 or similar)
- `python3-gi`, `gir1.2-atspi-2.0` and `python3-websockets`

## Install

```bash
git clone https://github.com/yusufgney/stateID.git ~/stateid
~/stateid/install.sh
```

Then log out and back in once, so GNOME Shell loads the extension. The installer symlinks the files, so `git pull` updates your installation. To remove it, run `~/stateid/uninstall.sh`. Saved states are kept.

## Use

You can get an ID in three ways:

| Where | How |
|---|---|
| Any window | Right-click the title bar, or **Super + right-click** anywhere inside the window, and choose **Copy state ID of this window** or **Copy state ID of the whole desktop** |
| Keyboard | **Super+Shift+S** saves the whole desktop |
| Terminal | `stateid save --copy` |

The ID goes to the clipboard and a notification shows it.

```
stateid save [--window SEQ | --focused] [--copy]   snapshot, print a new ID
stateid restore ID          bring the moment back (shortcut: stateid ID)
stateid show ID             the summary an agent should read (AGENT.md)
stateid list [-n N]         saved states, newest first
stateid delete ID...        delete saved states
stateid --help              full help; every command also has --help
```

### Continuing with an AI agent

Tell the agent: *"Run `stateid show DK-7SZKSJV0` and continue from there."*

`show` prints a Markdown summary of each window: the documents, the app state, the text on terminal screens, and paths to a screenshot of every window. The agent can look at the screenshots for anything the data does not cover.

## What gets saved

| App | State |
|---|---|
| Every app | Title, position, size, workspace, screenshot, working directory. Open documents, found from the process, the window title and GNOME's recent files. Pressed tool buttons, selected tabs, scroll positions, text fields, and the text cursor (line and column), all read from the accessibility tree. |
| OnlyOffice | Every open document and which one is active. Current slide or page, toolbar tab, selected pen with its colour and size, eraser. A running slideshow with its slide, pen or highlighter, and colour. |
| Brave, Chrome, Chromium | All tabs of the window with their URLs and which one is active. Read from the browser's own session file, so no browser extension is needed. |
| Ptyxis, GNOME Terminal | Each tab's directory, the command running in it, and the text on screen. |
| Nautilus | The folder shown. |
| Evince | The document and page. |
| VS Code | The project folder and the active file. |

On restore, `stateid`:

- reopens apps that are closed and reuses windows that are still open;
- puts each window back in its place;
- re-applies the saved state, for example going to the page, selecting the pen, starting the slideshow, or moving the cursor.

Commands that were running in terminals are printed, not re-run.

## How it works

- **`extension/`** is a small GNOME Shell extension. It adds the window-menu items and exposes a D-Bus interface at `io.github.yusufgney.StateId`. The interface lists windows, takes screenshots, moves windows and uses the clipboard; on Wayland, only the compositor can do these things.
- **`bin/stateid`** is a Python script that does the rest. Each app is read in the way that app allows:
  - The **accessibility tree** (AT-SPI) works for any toolkit app.
  - **OnlyOffice** runs its editor in an embedded Chromium. The installer adds `--remote-debugging-port=9333` to its launcher, so `stateid` can ask the editor which slide and pen are active, and set them again.
  - **Chromium browsers** write every tab to an SNSS session file, which `stateid` parses.
  - **Nautilus** reports its open folders over D-Bus.

States are stored in `~/.local/share/stateid/<ID>/` as `state.json`, `AGENT.md` and `windowN.png`. Set `STATEID_DIR` to use another folder.

## Limits

- Anything an app does not expose cannot be restored as data; it stays visible in the screenshot. Examples are the scroll position inside a web page, unsaved form input, and the internal state of Electron apps.
- Unsaved changes are flagged but not recreated. Apps such as Text Editor and OnlyOffice keep their own drafts.
- **Security:** the OnlyOffice debug port listens on `127.0.0.1` only, but any program running as your user can use it to control OnlyOffice. Screenshots and terminal text are stored unencrypted in your home folder.

## License

MIT
