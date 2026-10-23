// Exposes the visible windows' frame rects on the session bus for snapshot's
// window picker: org.gnome.Shell.Introspect.GetWindows is denied to normal apps.
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `<node>
  <interface name="org.gnome.Shell.Extensions.Snapshot">
    <method name="GetWindows"><arg type="s" direction="out" name="json"/></method>
    <method name="Raise"><arg type="t" direction="in" name="id"/></method>
  </interface>
</node>`;

const TYPES = [Meta.WindowType.NORMAL, Meta.WindowType.DIALOG,
    Meta.WindowType.MODAL_DIALOG, Meta.WindowType.UTILITY];

export default class SnapshotExtension extends Extension {
    enable() {
        this._impl = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._impl.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/Snapshot');
    }

    disable() {
        this._impl.unexport();
        this._impl = null;
    }

    // JSON {stage: [w, h], windows: [{id, rect: [x, y, w, h], title}]}, topmost first.
    GetWindows() {
        const workspace = global.workspace_manager.get_active_workspace();
        const windows = [];
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (!TYPES.includes(win.get_window_type()) || win.minimized ||
                !win.located_on_workspace(workspace))
                continue;
            const r = win.get_frame_rect();
            windows.unshift({id: win.get_id(), rect: [r.x, r.y, r.width, r.height], title: win.get_title() ?? ''});
        }
        return JSON.stringify({stage: [global.stage.width, global.stage.height], windows});
    }

    // Bring a window to the front so nothing covers it in the capture.
    Raise(id) {
        const actor = global.get_window_actors().find(a => a.meta_window.get_id() === id);
        actor?.meta_window.activate(global.get_current_time());
    }
}
