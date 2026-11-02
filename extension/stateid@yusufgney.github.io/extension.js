// stateid: adds "Copy state ID" items to the window menu (right-click a title bar,
// or Super + right-click anywhere in a window) and exposes the D-Bus interface the
// `stateid` command uses to list, capture, place, focus and save windows.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {WindowMenu} from 'resource:///org/gnome/shell/ui/windowMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

Gio._promisify(Shell.Screenshot, 'composite_to_stream');
Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

const OBJECT_PATH = '/io/github/yusufgney/StateId';
const IFACE = `
<node>
  <interface name="io.github.yusufgney.StateId">
    <method name="ListWindows"><arg type="s" direction="out"/></method>
    <method name="Capture">
      <arg type="u" direction="in"/><arg type="s" direction="in"/>
      <arg type="b" direction="out"/>
    </method>
    <method name="Activate"><arg type="u" direction="in"/><arg type="b" direction="out"/></method>
    <method name="SaveWindow"><arg type="u" direction="in"/><arg type="b" direction="out"/></method>
    <method name="MoveResize">
      <arg type="u" direction="in"/>
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
      <arg type="i" direction="in"/><arg type="i" direction="in"/>
      <arg type="b" direction="in"/>
      <arg type="b" direction="out"/>
    </method>
    <method name="CopyToClipboard"><arg type="s" direction="in"/></method>
    <method name="Notify"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
  </interface>
</node>`;

const STATEID_BIN = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'stateid']);
const USER_TYPES = [Meta.WindowType.NORMAL, Meta.WindowType.DIALOG];

function userWindows() {
    return global.display.list_all_windows().filter(w =>
        !w.is_skip_taskbar() && USER_TYPES.includes(w.get_window_type()));
}

function findWindow(seq) {
    return userWindows().find(w => w.get_stable_sequence() === seq) ?? null;
}

function describe(win) {
    const app = Shell.WindowTracker.get_default().get_window_app(win);
    const r = win.get_frame_rect();
    return {
        seq: win.get_stable_sequence(),
        title: win.get_title(),
        wm_class: win.get_wm_class(),
        app_id: app?.get_id() ?? null,
        app_name: app?.get_name() ?? null,
        gtk_app_id: win.get_gtk_application_id(),
        sandboxed_app_id: win.get_sandboxed_app_id(),
        pid: win.get_pid(),
        workspace: win.get_workspace()?.index() ?? null,
        monitor: win.get_monitor(),
        rect: {x: r.x, y: r.y, width: r.width, height: r.height},
        maximized: win.is_maximized(),
        fullscreen: win.is_fullscreen(),
        minimized: win.minimized,
        focused: win.has_focus(),
        user_time: win.get_user_time(),
    };
}

async function capture(win, path) {
    const actor = win.get_compositor_private();
    if (!actor || win.minimized)
        return false;
    const texture = actor.paint_to_content(null)?.get_texture();
    if (!texture)
        return false;
    const stream = Gio.MemoryOutputStream.new_resizable();
    await Shell.Screenshot.composite_to_stream(texture, 0, 0, -1, -1, 1, null, 0, 0, 1, stream);
    stream.close(null);
    Gio.File.new_for_path(path).replace_contents(stream.steal_as_bytes().toArray(), null, false,
        Gio.FileCreateFlags.REPLACE_DESTINATION, null);
    return true;
}

const wait = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));

let keyboard = null;
const HELD = Clutter.ModifierType.SHIFT_MASK | Clutter.ModifierType.CONTROL_MASK |
    Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.SUPER_MASK | Clutter.ModifierType.MOD4_MASK;

// Focus the window and press Ctrl+S in it, as the user would: the app saves with its own save.
// Waits until the user lets go of the keys (Super+Shift+S may still be held: with Shift it would
// be Save As).
async function pressSave(win) {
    for (let i = 0; i < 40 && (global.get_pointer()[2] & HELD); i++)
        await wait(25);
    if (global.get_pointer()[2] & HELD)
        return false;
    Main.activateWindow(win);
    for (let i = 0; i < 40 && !win.has_focus(); i++)
        await wait(25);
    if (!win.has_focus())
        return false;
    keyboard ??= Clutter.get_default_backend().get_default_seat()
        .create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
    const t = GLib.get_monotonic_time();
    keyboard.notify_keyval(t, Clutter.KEY_Control_L, Clutter.KeyState.PRESSED);
    keyboard.notify_keyval(t, Clutter.KEY_s, Clutter.KeyState.PRESSED);
    keyboard.notify_keyval(t, Clutter.KEY_s, Clutter.KeyState.RELEASED);
    keyboard.notify_keyval(t, Clutter.KEY_Control_L, Clutter.KeyState.RELEASED);
    return true;
}

async function runStateId(args) {
    try {
        const proc = Gio.Subprocess.new([STATEID_BIN, ...args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        const [stdout, stderr] = await proc.communicate_utf8_async(null, null);
        if (!proc.get_successful())
            throw new Error(stderr || stdout);
    } catch (e) {
        Main.notifyError('Could not create a state ID', String(e.message ?? e));
    }
}

class StateIdService {
    ListWindows() {
        return JSON.stringify(userWindows().map(describe));
    }

    async CaptureAsync([seq, path], invocation) {
        try {
            const win = findWindow(seq);
            invocation.return_value(new GLib.Variant('(b)', [win ? await capture(win, path) : false]));
        } catch (e) {
            logError(e, 'stateid: capture');
            invocation.return_value(new GLib.Variant('(b)', [false]));
        }
    }

    Activate(seq) {
        const win = findWindow(seq);
        if (win)
            Main.activateWindow(win);
        return win !== null;
    }

    async SaveWindowAsync([seq], invocation) {
        try {
            const win = findWindow(seq);
            invocation.return_value(new GLib.Variant('(b)', [win ? await pressSave(win) : false]));
        } catch (e) {
            logError(e, 'stateid: save');
            invocation.return_value(new GLib.Variant('(b)', [false]));
        }
    }

    MoveResize(seq, x, y, w, h, maximize) {
        const win = findWindow(seq);
        if (!win)
            return false;
        if (win.is_maximized())
            win.unmaximize();
        win.move_resize_frame(true, x, y, w, h);
        if (maximize)
            win.maximize();
        return true;
    }

    CopyToClipboard(text) {
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
    }

    Notify(title, body) {
        Main.notify(title, body);
    }
}

export default class StateIdExtension extends Extension {
    enable() {
        this._service = Gio.DBusExportedObject.wrapJSObject(IFACE, new StateIdService());
        this._service.export(Gio.DBus.session, OBJECT_PATH);

        const origBuild = WindowMenu.prototype._buildMenu;
        this._origBuild = origBuild;
        WindowMenu.prototype._buildMenu = function (window) {
            origBuild.call(this, window);
            if (!USER_TYPES.includes(window.get_window_type()))
                return;
            this.addMenuItem(new PopupMenu.PopupSeparatorMenuItem(), 0);
            this.addAction('Copy state ID of the whole desktop',
                () => runStateId(['save', '--copy']), null);
            this.moveMenuItem(this._getMenuItems().at(-1), 0);
            this.addAction('Copy state ID of this window',
                () => runStateId(['save', '--copy', '--window', String(window.get_stable_sequence())]), null);
            this.moveMenuItem(this._getMenuItems().at(-1), 0);
        };
    }

    disable() {
        WindowMenu.prototype._buildMenu = this._origBuild;
        this._origBuild = null;
        this._service?.unexport();
        this._service = null;
        keyboard?.run_dispose();
        keyboard = null;
    }
}
