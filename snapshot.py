#!/usr/bin/python3
"""Snapshot - a GNOME-style screenshot / screencast tool for Ubuntu (GNOME on Wayland).

Targets: selection (area), window, screen (monitor under pointer), all (every monitor).
Screenshots come from the xdg-desktop-portal Screenshot API after an area is
picked on a see-through overlay, saved as PNG and copied to the clipboard.
Screencasts are recorded from the portal ScreenCast PipeWire stream with GStreamer
(+ optional PulseAudio monitor) to MP4. The UI itself runs through XWayland so the
overlays can be placed on screen.
"""

import argparse
import datetime
import os
import shutil
import signal
import subprocess
import sys
import tempfile

import json

import cairo
import gi

# Wayland doesn't let clients place their own windows; the overlays need XWayland.
os.environ["GDK_BACKEND"] = "x11"
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Notify  # noqa: E402


APP_ID = "snapshot"
APP_NAME = "Snapshot"
TOKEN_FILE = os.path.join(GLib.get_user_config_dir(), APP_ID, "screencast-tokens.json")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
PID_FILE = os.path.join(RUNTIME_DIR, "snapshot-record.pid")
TARGETS = ("area", "window", "screen", "all")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def xdg_dir(kind, fallback):
    try:
        out = subprocess.run(["xdg-user-dir", kind], capture_output=True, text=True).stdout.strip()
        if out and out != os.path.expanduser("~"):
            return out
    except FileNotFoundError:
        pass
    return os.path.expanduser(fallback)


def default_output(video):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    folder = xdg_dir("DOWNLOAD", "~/Downloads")
    if video:
        name = f"Screencast from {now}.mp4"
    else:
        name = f"Screenshot from {now}.png"
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def scale_factor():
    display = Gdk.Display.get_default()
    monitor = display.get_monitor(0)
    return monitor.get_scale_factor() if monitor else 1


def root_size():
    """Root window size in physical pixels."""
    root = Gdk.get_default_root_window()
    s = scale_factor()
    return root.get_width() * s, root.get_height() * s


def pointer_monitor_rect():
    """Physical-pixel geometry of the monitor under the pointer."""
    display = Gdk.Display.get_default()
    _, x, y = display.get_default_seat().get_pointer().get_position()[:3]
    monitor = display.get_monitor_at_point(x, y)
    g = monitor.get_geometry()
    s = monitor.get_scale_factor()
    return g.x * s, g.y * s, g.width * s, g.height * s


def show_in_folder(path):
    """Open the file manager with `path` selected; fall back to opening its folder."""
    try:
        subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                        "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                        f"array:string:{GLib.filename_to_uri(path)}", "string:"],
                       check=True, capture_output=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def notify_saved(summary, path, icon, extra=""):
    """Saved-file notification whose click opens the file's folder.

    Handled by a detached helper process (`snapshot notify-saved`) because the
    main process usually exits right after saving and would miss the click.
    """
    cmd = [sys.executable, os.path.abspath(__file__), "notify-saved", path, summary, icon, extra]
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_notify_saved(path, summary, icon, extra):
    Notify.init(APP_NAME)
    loop = GLib.MainLoop()
    body = path + (f"\n{extra}" if extra else "")
    n = Notify.Notification.new(summary, body, icon)
    n.set_hint("desktop-entry", GLib.Variant("s", APP_ID))

    def open_folder(*_):
        show_in_folder(path)
        n.close()
        loop.quit()

    n.add_action("default", "폴더에서 보기", open_folder)
    n.add_action("open-folder", "폴더에서 보기", open_folder)
    n.connect("closed", lambda *_: loop.quit())
    try:
        n.show()
    except GLib.Error:
        return 1
    # The notification stays in the message tray; give up after a while.
    GLib.timeout_add_seconds(30 * 60, loop.quit)
    loop.run()
    return 0


def notify(summary, body="", icon="camera-photo"):
    if not Notify.is_initted():
        Notify.init(APP_NAME)
    try:
        n = Notify.Notification.new(summary, body, icon)
        n.set_hint("desktop-entry", GLib.Variant("s", APP_ID))
        n.show()
    except GLib.Error:
        pass


def check_session():
    if not os.environ.get("DISPLAY"):
        msg = "XWayland(DISPLAY)를 찾을 수 없습니다. GNOME 세션에서 실행하세요."
        print(msg, file=sys.stderr)
        notify(APP_NAME, msg, "dialog-error")
        return False
    for tool, pkg in (("ffmpeg", "ffmpeg"), ("gst-launch-1.0", "gstreamer1.0-tools")):
        if not shutil.which(tool):
            print(f"{tool}가 필요합니다: sudo apt install {pkg}", file=sys.stderr)
            return False
    return True


# ---------------------------------------------------------------------------
# xdg-desktop-portal
# ---------------------------------------------------------------------------

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"


class PortalCancelled(Exception):
    pass


class Portal:
    """Blocking wrapper around portal Request/Response calls (runs a nested main loop)."""

    count = 0

    def __init__(self):
        # A private connection: the shared one may predate detach()'s fork and lack its worker thread.
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        self.bus = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")

    def token(self):
        Portal.count += 1
        return f"snapshot_{os.getpid()}_{Portal.count}"

    def request(self, iface, method, args, options):
        """args: [(signature, value)]. Returns the results dict; raises PortalCancelled / GLib.Error."""
        token = self.token()
        path = f"{PORTAL_PATH}/request/{self.sender}/{token}"
        loop = GLib.MainLoop()
        response = []

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            response.extend(params.unpack())
            loop.quit()

        sub = self.bus.signal_subscribe(PORTAL_NAME, "org.freedesktop.portal.Request", "Response",
                                        path, None, Gio.DBusSignalFlags.NONE, on_response)
        options = dict(options, handle_token=GLib.Variant("s", token))
        signature = "(" + "".join(sig for sig, _ in args) + "a{sv})"
        try:
            self.bus.call_sync(PORTAL_NAME, PORTAL_PATH, f"org.freedesktop.portal.{iface}", method,
                               GLib.Variant(signature, (*(v for _, v in args), options)),
                               None, Gio.DBusCallFlags.NONE, -1, None)
            loop.run()
        finally:
            self.bus.signal_unsubscribe(sub)
        code, results = response
        if code != 0:
            raise PortalCancelled()
        return results


def portal_screenshot(interactive=False):
    """Full-desktop screenshot (or, interactively, whatever the user picks in GNOME's UI)."""
    results = Portal().request("Screenshot", "Screenshot", [("s", "")],
                               {"interactive": GLib.Variant("b", interactive)})
    path = GLib.filename_from_uri(results["uri"])[0]
    try:
        return GdkPixbuf.Pixbuf.new_from_file(path)
    finally:
        # The portal drops its file into ~/Pictures; we only want the pixels.
        try:
            os.unlink(path)
        except OSError:
            pass


def without_alpha(pixbuf):
    """Opaque RGB copy. Portal grabs are RGBA, and Windows apps (Wine, Remmina's RDP
    clipboard) mishandle the 32-bit alpha BMP that GTK would offer for them."""
    if not pixbuf.get_has_alpha():
        return pixbuf
    w, h = pixbuf.get_width(), pixbuf.get_height()
    rgb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
    rgb.fill(0x000000ff)
    pixbuf.composite(rgb, 0, 0, w, h, 0, 0, 1, 1, GdkPixbuf.InterpType.NEAREST, 255)
    return rgb


def load_tokens():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_tokens(tokens):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)


def monitor_key(rect):
    """Key ("x,y" of the monitor holding the rect's centre) for the saved screencast tokens."""
    display = Gdk.Display.get_default()
    s = scale_factor()
    x, y, w, h = rect
    g = display.get_monitor_at_point(int((x + w / 2) / s), int((y + h / 2) / s)).get_geometry()
    return f"{g.x},{g.y}"


class ScreenCast:
    """Portal screencast session handing out a PipeWire fd + node for pipewiresrc.

    Monitor sessions are persisted per monitor, so GNOME only asks which monitor to
    share the first time; window sessions always ask which window to record.
    """

    def __init__(self):
        self.portal = Portal()
        self.session = None
        self.fd = None
        self.node = None
        self.props = {}

    def start(self, window, pointer, key=None):
        """Returns True when streaming; raises PortalCancelled / GLib.Error otherwise."""
        p = self.portal
        try:
            res = p.request("ScreenCast", "CreateSession", [],
                            {"session_handle_token": GLib.Variant("s", p.token())})
            self.session = res["session_handle"]
            tokens = load_tokens()
            options = {"types": GLib.Variant("u", 2 if window else 1),
                       "multiple": GLib.Variant("b", False),
                       "cursor_mode": GLib.Variant("u", 2 if pointer else 1),
                       "persist_mode": GLib.Variant("u", 2)}
            if not window and tokens.get(key):
                options["restore_token"] = GLib.Variant("s", tokens[key])
            p.request("ScreenCast", "SelectSources", [("o", self.session)], options)
            res = p.request("ScreenCast", "Start", [("o", self.session), ("s", "")], {})
            self.node, self.props = res["streams"][0]
            if not window and res.get("restore_token"):
                pos = self.props.get("position", (0, 0))
                tokens[f"{pos[0]},{pos[1]}"] = res["restore_token"]
                save_tokens(tokens)
            ret, fds = p.bus.call_with_unix_fd_list_sync(
                PORTAL_NAME, PORTAL_PATH, "org.freedesktop.portal.ScreenCast", "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session, {})), GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE, -1, None, None)
            self.fd = fds.get(ret.unpack()[0])
            return True
        except Exception:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.session is not None:
            try:
                self.portal.bus.call_sync(PORTAL_NAME, self.session, "org.freedesktop.portal.Session",
                                          "Close", None, None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
            self.session = None


def gst_record_command(fd, node, crop, fps, audio_device, path):
    """gst-launch pipeline: PipeWire stream (cropped) -> H.264 (+ AAC) -> MP4."""
    cmd = ["gst-launch-1.0", "-q", "-e",
           "pipewiresrc", f"fd={fd}", f"path={node}", "do-timestamp=true", "keepalive-time=1000", "!",
           "videoconvert", "!", "videorate", "!", f"video/x-raw,framerate={fps}/1", "!"]
    if crop:
        left, top, right, bottom = crop
        cmd += ["videocrop", f"left={left}", f"top={top}", f"right={right}", f"bottom={bottom}", "!"]
    cmd += ["videoconvert", "!", "video/x-raw,format=I420", "!",
            "x264enc", "speed-preset=veryfast", "pass=quant", "quantizer=23", "!", "h264parse", "!",
            "queue", "!", "mp4mux", "name=mux", "faststart=true", "!", "filesink", f"location={path}"]
    if audio_device:
        cmd += ["pulsesrc", f"device={audio_device}", "!", "audioconvert", "!", "audioresample", "!",
                "avenc_aac", "bitrate=160000", "!", "queue", "!", "mux."]
    return cmd


def gif_command(src, dst, fps=15, max_width=1280):
    vf = (f"fps={fps},scale='min({max_width},iw)':-2:flags=lanczos,split[a][b];"
          "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    return ["ffmpeg", "-loglevel", "error", "-y", "-i", src, "-vf", vf, "-loop", "0", dst]


def clamp_rect(rect, width, height):
    x, y, w, h = rect
    x2, y2 = min(x + w, width), min(y + h, height)
    x, y = max(x, 0), max(y, 0)
    return x, y, max(x2 - x, 0), max(y2 - y, 0)


# ---------------------------------------------------------------------------
# Selection overlay
# ---------------------------------------------------------------------------

class SelectionOverlay(Gtk.Window):
    """Translucent fullscreen overlay over the live screen to pick an area.

    The screen is only grabbed after the pick: every portal grab flashes the screen.

    Calls on_done(rect) with a physical-pixel rect, or on_done(None) when cancelled.
    """

    def __init__(self, on_done):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.on_done = on_done
        self.scale = scale_factor()
        self.start = None
        self.current = None
        self.finished = False

        w, h = root_size()
        self.move(0, 0)
        self.set_default_size(w // self.scale, h // self.scale)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)

        area = Gtk.DrawingArea()
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        area.connect("draw", self.on_draw)
        area.connect("button-press-event", self.on_press)
        area.connect("button-release-event", self.on_release)
        area.connect("motion-notify-event", self.on_motion)
        self.connect("key-press-event", self.on_key)
        self.add(area)
        self.area = area

    def present_and_grab(self):
        self.show_all()
        gdk_win = self.get_window()
        cursor = Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.CROSSHAIR)
        seat = Gdk.Display.get_default().get_default_seat()
        # Retry briefly: the grab can fail until the popup is actually mapped.
        def try_grab(attempt=[0]):
            status = seat.grab(gdk_win, Gdk.SeatCapabilities.ALL, True, cursor, None, None)
            attempt[0] += 1
            return status != Gdk.GrabStatus.SUCCESS and attempt[0] < 20
        GLib.timeout_add(25, try_grab)

    def pointer_pos(self):
        _, x, y, _ = self.get_window().get_pointer()
        return x, y

    # -- geometry ----------------------------------------------------------

    def selection_logical(self):
        if self.start is None or self.current is None:
            return None
        (x1, y1), (x2, y2) = self.start, self.current
        return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)

    # -- drawing -----------------------------------------------------------

    def on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        sel = self.selection_logical()
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.rectangle(0, 0, width, height)
        if sel:
            x, y, w, h = sel
            cr.rectangle(x, y, w, h)
            cr.set_fill_rule(1)  # EVEN_ODD: the selection becomes a hole
        cr.fill()

        if sel:
            x, y, w, h = sel
            cr.set_source_rgba(0.21, 0.52, 0.89, 1)
            cr.set_line_width(2)
            cr.rectangle(x + 1, y + 1, max(w - 2, 0), max(h - 2, 0))
            cr.stroke()
            label = f"{int(w * self.scale)} × {int(h * self.scale)}"
            self.draw_label(cr, label, x, y + h + 8, height)
        else:
            hint = "드래그해서 영역을 선택하세요 · Esc 취소"
            x, y = self.pointer_pos()
            self.draw_label(cr, hint, x + 16, y + 16, height)

    def draw_label(self, cr, text, x, y, max_y):
        cr.select_font_face("Sans")
        cr.set_font_size(13)
        ext = cr.text_extents(text)
        pad = 6
        if y + ext.height + pad * 2 > max_y:
            y = max_y - ext.height - pad * 2 - 4
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.rectangle(x, y, ext.width + pad * 2, ext.height + pad * 2)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(x + pad - ext.x_bearing, y + pad - ext.y_bearing)
        cr.show_text(text)

    # -- events ------------------------------------------------------------

    def on_press(self, widget, event):
        if event.button == 3:
            self.finish(None)
        elif event.button == 1:
            self.start = self.current = (event.x, event.y)
        return True

    def on_motion(self, widget, event):
        if self.start is not None:
            self.current = (event.x, event.y)
        widget.queue_draw()
        return True

    def on_release(self, widget, event):
        if event.button != 1 or self.start is None:
            return True
        self.current = (event.x, event.y)
        x, y, w, h = self.selection_logical()
        if w < 4 or h < 4:
            self.start = self.current = None
            widget.queue_draw()
            return True
        s = self.scale
        self.finish((round(x * s), round(y * s), round(w * s), round(h * s)))
        return True

    def on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.finish(None)
        return True

    def finish(self, rect):
        if self.finished:
            return
        self.finished = True
        Gdk.Display.get_default().get_default_seat().ungrab()
        self.destroy()
        w, h = root_size()
        if rect is not None:
            rect = clamp_rect(rect, w, h)
        # Let the overlay unmap before anything captures the screen again.
        GLib.timeout_add(150, lambda: (self.on_done(rect), False)[1])


# ---------------------------------------------------------------------------
# Screenshot preview
# ---------------------------------------------------------------------------

class PreviewWindow(Gtk.Window):
    """Shows the capture before anything is written; the user decides to save or discard."""

    MAX_W, MAX_H = 960, 640

    def __init__(self, pixbuf, path, copied, on_close):
        super().__init__(title="스크린샷")
        self.pixbuf = pixbuf
        self.path = path
        self.saved = None
        self.on_close = on_close
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_icon_name(APP_ID)
        self.connect("destroy", lambda *_: self.on_close())
        self.connect("key-press-event", self.on_key)

        w, h = pixbuf.get_width(), pixbuf.get_height()
        self.header = Gtk.HeaderBar(title="스크린샷 미리보기", show_close_button=True)
        self.header.set_subtitle(f"{w} × {h}" + ("  ·  클립보드에 복사됨" if copied else ""))
        self.set_titlebar(self.header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(12)
        self.add(box)

        ratio = min(self.MAX_W / w, self.MAX_H / h, 1.0)
        thumb = pixbuf.scale_simple(max(int(w * ratio), 1), max(int(h * ratio), 1),
                                    GdkPixbuf.InterpType.BILINEAR)
        frame = Gtk.EventBox()
        frame.add(Gtk.Image.new_from_pixbuf(thumb))
        frame.set_tooltip_text("다른 앱으로 끌어다 놓을 수 있습니다")
        # Drag out into other apps; an unsaved capture is exported to a temp file first.
        frame.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.COPY)
        frame.drag_source_add_uri_targets()
        frame.drag_source_set_icon_pixbuf(thumb.scale_simple(
            max(thumb.get_width() // 4, 1), max(thumb.get_height() // 4, 1), GdkPixbuf.InterpType.BILINEAR))
        frame.connect("drag-data-get", lambda _w, _c, data, *_: data.set_uris([GLib.filename_to_uri(self.drag_file())]))
        box.pack_start(frame, True, True, 0)

        actions = Gtk.Box(spacing=8)
        name = Gtk.Label(label=os.path.basename(path), selectable=True)
        name.set_ellipsize(2)  # Pango.EllipsizeMode.MIDDLE
        actions.set_center_widget(name)
        self.save_btn = Gtk.Button(label="저장")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save_as())
        actions.pack_end(self.save_btn, False, False, 0)
        box.pack_start(actions, False, False, 0)

        self.save_btn.grab_focus()

    def on_key(self, _w, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape:
            self.destroy()
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) or (ctrl and key == Gdk.KEY_s):
            self.save_as()
        else:
            return False
        return True

    def save(self, path, close):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.pixbuf.savev(path, "png", [], [])
        except (GLib.Error, OSError) as exc:
            self.header.set_subtitle(f"저장 실패: {exc}")
            return
        self.saved = path
        print(path, flush=True)
        notify_saved("스크린샷을 저장했습니다", path, "camera-photo")
        if close:
            self.destroy()

    def save_as(self):
        dialog = Gtk.FileChooserDialog(title="스크린샷 저장", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("취소", Gtk.ResponseType.CANCEL, "저장", Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_folder(os.path.dirname(self.path))
        dialog.set_current_name(os.path.basename(self.path))
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            path = dialog.get_filename()
            dialog.destroy()
            if not path.lower().endswith(".png"):
                path += ".png"
            self.save(path, close=True)
        else:
            dialog.destroy()

    def drag_file(self):
        if self.saved:
            return self.saved
        folder = tempfile.mkdtemp(prefix="snapshot-")
        path = os.path.join(folder, os.path.basename(self.path))
        self.pixbuf.savev(path, "png", [], [])
        return path


SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewToolTip"/>
    <signal name="XAyatanaNewLabel"><arg type="s"/><arg type="s"/></signal>
  </interface>
</node>
"""

DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/><arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
    <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
  </interface>
</node>
"""


class RecordingIndicator:
    """Top-bar recording badge (StatusNotifierItem) that stops the recording on click.

    It uses its own bus connection and a per-recording Id, so closing the connection
    removes the item and a leftover item from an earlier recording can't shadow it.
    """

    PATH = "/StatusNotifierItem"
    MENU_PATH = "/StatusNotifierItem/Menu"

    def __init__(self, on_click):
        self.on_click = on_click
        self.label = "00:00"
        self.conn = None
        try:
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.conn = Gio.DBusConnection.new_for_address_sync(
                address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
            info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
            self.conn.register_object(self.PATH, info, self.on_call, self.on_get, None)
            # The Ubuntu tray only acts on a single left click by opening the menu, so
            # export a one-item menu and treat "menu opened" as the stop click.
            menu_info = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML).interfaces[0]
            self.conn.register_object(self.MENU_PATH, menu_info, self.on_menu_call, self.on_menu_get, None)
            self.conn.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                                "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                                GLib.Variant("(s)", (self.conn.get_unique_name(),)),
                                None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as exc:
            print(f"tray indicator unavailable: {exc.message}", file=sys.stderr)
            self.close()

    def on_get(self, _conn, _sender, _path, _iface, prop):
        tooltip = ("media-record", [], "화면 녹화 중", f"{self.label} · 클릭하면 녹화를 중지합니다")
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", f"{APP_ID}-recording-{os.getpid()}-{id(self)}"),
            "Title": GLib.Variant("s", "화면 녹화 중지"),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", "media-record"),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", tooltip),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", self.MENU_PATH),
            "XAyatanaLabel": GLib.Variant("s", self.label),
        }
        return values.get(prop)

    def on_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        invocation.return_value(None)
        # Any click on the badge (left, middle or right) stops the recording.
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            GLib.idle_add(lambda: (self.on_click(), False)[1])

    def on_menu_get(self, _conn, _sender, _path, _iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def menu_item(self, item_id):
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        return {"label": GLib.Variant("s", f"녹화 중지 ({self.label})"),
                "icon-name": GLib.Variant("s", "media-playback-stop-symbolic")}

    def on_menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            child = GLib.Variant("(ia{sv}av)", (1, self.menu_item(1), []))
            layout = (0, self.menu_item(0), [child])
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, layout)))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0] or [0, 1]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self.menu_item(i)) for i in ids if i in (0, 1)],)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            invocation.return_value(GLib.Variant("(v)", (self.menu_item(item_id).get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            item_id, event_id = params.unpack()[:2]
            invocation.return_value(None)
            if (item_id, event_id) in ((0, "opened"), (1, "clicked")):
                GLib.idle_add(lambda: (self.on_click(), False)[1])
        elif method == "EventGroup":
            events = params.unpack()[0]
            invocation.return_value(GLib.Variant("(ai)", ([],)))
            if any((i, e) in ((0, "opened"), (1, "clicked")) for i, e, *_ in events):
                GLib.idle_add(lambda: (self.on_click(), False)[1])
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))

    def set_elapsed(self, text):
        self.label = text
        if self.conn is None:
            return
        for signal_name, params in (("XAyatanaNewLabel", GLib.Variant("(ss)", (text, "00:00"))),
                                    ("NewToolTip", None)):
            try:
                self.conn.emit_signal(None, self.PATH, "org.kde.StatusNotifierItem", signal_name, params)
            except GLib.Error:
                pass

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close_sync(None)
            except GLib.Error:
                pass
            self.conn = None


def make_click_through(window):
    """Give a window an empty input shape so clicks reach the windows below.

    Set on the widget (not the GdkWindow): GTK re-applies its own input shape to
    RGBA toplevels when they are mapped, which would otherwise cover the whole area.
    """
    window.input_shape_combine_region(cairo.Region())


class CountdownWindow(Gtk.Window):
    """Big round countdown shown in the middle of the monitor under the pointer."""

    SIZE = 160

    def __init__(self, seconds, rect=None):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.remaining = int(seconds)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_default_size(self.SIZE, self.SIZE)
        if rect is not None:
            # Centre over the chosen area (physical px -> logical).
            sf = scale_factor()
            x, y, w, h = (v / sf for v in rect)
        else:
            display = Gdk.Display.get_default()
            _, px, py = display.get_default_seat().get_pointer().get_position()[:3]
            geo = display.get_monitor_at_point(px, py).get_geometry()
            x, y, w, h = geo.x, geo.y, geo.width, geo.height
        self.move(int(x + (w - self.SIZE) / 2), int(y + (h - self.SIZE) / 2))
        self.connect("draw", self.on_draw)
        make_click_through(self)

    def on_draw(self, _w, cr):
        r = self.SIZE / 2
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.arc(r, r, r - 4, 0, 6.2832)
        cr.set_source_rgba(0.14, 0.14, 0.16, 0.85)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.set_line_width(4)
        cr.stroke()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(84)
        text = str(self.remaining)
        ext = cr.text_extents(text)
        cr.move_to(r - ext.width / 2 - ext.x_bearing, r - ext.height / 2 - ext.y_bearing)
        cr.set_source_rgb(1, 1, 1)
        cr.show_text(text)
        return True


def countdown(seconds, done, rect=None):
    """Show a countdown for `seconds`, then call done() once it is off the screen."""
    if seconds <= 0:
        GLib.timeout_add(1, done)
        return
    win = CountdownWindow(seconds, rect)
    win.show_all()

    def tick():
        win.remaining -= 1
        if win.remaining > 0:
            win.queue_draw()
            return True
        win.destroy()
        # Give the compositor a moment to drop the countdown before grabbing.
        GLib.timeout_add(150, done)
        return False
    GLib.timeout_add(1000, tick)


class RecordingFrame(Gtk.Window):
    """Click-through overlay that dims everything outside the area being recorded."""

    def __init__(self, rect):
        # A managed window, not override-redirect: mutter drops the shell's input region
        # (top bar, dock) under override-redirect windows, which made them unclickable.
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.scale = scale_factor()
        self.rect = rect
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        w, h = root_size()
        self.move(0, 0)
        self.set_default_size(w // self.scale, h // self.scale)
        self.connect("draw", self.on_draw)
        make_click_through(self)

    def on_draw(self, _w, cr):
        s = self.scale
        x, y, w, h = (v / s for v in self.rect)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.paint()
        # Keep the recorded area fully clear; the border sits just outside it.
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgba(0.88, 0.11, 0.14, 0.9)
        cr.set_line_width(2)
        cr.rectangle(x - 1.5, y - 1.5, w + 3, h + 3)
        cr.stroke()
        return True


class VideoPreviewWindow(Gtk.Window):
    """Plays back a finished recording; the file is kept only if the user saves it."""

    MAX_W, MAX_H = 960, 640

    @staticmethod
    def available():
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            return Gst.ElementFactory.find("gtksink") is not None
        except (ValueError, ImportError):
            return False

    def __init__(self, temp_path, target_path, size, on_close):
        super().__init__(title="화면 녹화")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst = Gst
        self.temp_path = temp_path
        self.target_path = target_path
        self.saved = False
        self.on_close = on_close
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_icon_name(APP_ID)
        self.connect("destroy", self.on_destroy)
        self.connect("delete-event", lambda *_: self.converting)  # block close while converting
        self.connect("key-press-event", self.on_key)

        w, h = size
        self.header = Gtk.HeaderBar(title="화면 녹화 미리보기", show_close_button=True)
        self.header.set_subtitle(f"{w} × {h}")
        self.set_titlebar(self.header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(12)
        self.add(box)

        self.player = Gst.ElementFactory.make("playbin", None)
        sink = Gst.ElementFactory.make("gtksink", None)
        self.player.set_property("video-sink", sink)
        self.player.set_property("uri", GLib.filename_to_uri(temp_path))
        video = sink.get_property("widget")
        ratio = min(self.MAX_W / w, self.MAX_H / h, 1.0)
        video.set_size_request(max(int(w * ratio), 1), max(int(h * ratio), 1))
        box.pack_start(video, True, True, 0)

        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self.on_eos)

        controls = Gtk.Box(spacing=8)
        self.play_btn = self.player_button("media-playback-start-symbolic", "재생", lambda: self.set_playing(True))
        self.pause_btn = self.player_button("media-playback-pause-symbolic", "일시정지",
                                            lambda: self.set_playing(False))
        controls.pack_start(self.play_btn, False, False, 0)
        controls.pack_start(self.pause_btn, False, False, 0)
        self.pos_label = Gtk.Label(label="00:00")
        controls.pack_start(self.pos_label, False, False, 0)
        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
        self.seek.set_draw_value(False)
        self.seek.connect("change-value", self.on_seek)
        controls.pack_start(self.seek, True, True, 0)
        self.dur_label = Gtk.Label(label="00:00")
        controls.pack_start(self.dur_label, False, False, 0)
        box.pack_start(controls, False, False, 0)

        actions = Gtk.Box(spacing=8)
        self.name_label = Gtk.Label(selectable=True)
        self.name_label.set_ellipsize(2)  # Pango.EllipsizeMode.MIDDLE
        actions.set_center_widget(self.name_label)
        self.save_btn = Gtk.Button(label="저장")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save_as())
        actions.pack_end(self.save_btn, False, False, 0)
        # Output format; GIF is converted from the MP4 when saving.
        fmt_box = Gtk.Box()
        fmt_box.get_style_context().add_class("linked")
        self.mp4_btn = Gtk.RadioButton.new_with_label_from_widget(None, "MP4")
        self.gif_btn = Gtk.RadioButton.new_with_label_from_widget(self.mp4_btn, "GIF")
        for btn in (self.mp4_btn, self.gif_btn):
            btn.set_mode(False)
            fmt_box.pack_start(btn, False, False, 0)
        self.gif_btn.connect("toggled", lambda *_: self.update_name())
        actions.pack_end(fmt_box, False, False, 0)
        box.pack_start(actions, False, False, 0)
        self.update_name()

        self.duration = 0
        self.at_end = False
        self.pending_seek = None
        self.converting = False
        self.set_playing(True)
        self.progress_timer = GLib.timeout_add(100, self.update_progress)

    @staticmethod
    def player_button(icon, tooltip, callback):
        btn = Gtk.Button()
        btn.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        btn.set_tooltip_text(tooltip)
        btn.connect("clicked", lambda *_: callback())
        return btn

    def set_playing(self, playing):
        if playing and self.at_end:
            self.seek_to(0)
        self.playing = playing
        self.player.set_state(self.Gst.State.PLAYING if playing else self.Gst.State.PAUSED)
        self.play_btn.set_sensitive(not playing)
        self.pause_btn.set_sensitive(playing)

    def toggle_play(self):
        self.set_playing(not self.playing)

    def seek_to(self, seconds):
        self.at_end = False
        ok = self.player.seek_simple(self.Gst.Format.TIME,
                                     self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.ACCURATE,
                                     int(seconds * self.Gst.SECOND))
        # Seeking fails until the pipeline has prerolled; retry from update_progress.
        self.pending_seek = None if ok else seconds

    def on_seek(self, _scale, _scroll, value):
        self.seek_to(min(max(value, 0), self.duration or value))
        self.pos_label.set_text(self.fmt(value))
        return False

    @staticmethod
    def fmt(seconds):
        seconds = int(max(seconds, 0))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def update_progress(self):
        ok, dur = self.player.query_duration(self.Gst.Format.TIME)
        if ok and dur > 0 and not self.duration:
            self.duration = dur / self.Gst.SECOND
            self.seek.set_range(0, self.duration)
            self.dur_label.set_text(self.fmt(self.duration))
        ok, pos = self.player.query_position(self.Gst.Format.TIME)
        if ok and self.pending_seek is not None:
            self.seek_to(self.pending_seek)
            return True
        if ok and not self.at_end:
            self.seek.set_value(pos / self.Gst.SECOND)
            self.pos_label.set_text(self.fmt(pos / self.Gst.SECOND))
        return True

    def on_eos(self, *_):
        # Stay on the last frame; pressing play starts over from the beginning.
        self.at_end = True
        self.seek.set_value(self.duration)
        self.pos_label.set_text(self.fmt(self.duration))
        self.set_playing(False)

    def on_key(self, _w, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape:
            if not self.converting:
                self.destroy()
        elif key == Gdk.KEY_space:
            self.toggle_play()
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) or (ctrl and key == Gdk.KEY_s):
            self.save_as()
        else:
            return False
        return True

    def ext(self):
        return ".gif" if self.gif_btn.get_active() else ".mp4"

    def default_name(self):
        return os.path.splitext(os.path.basename(self.target_path))[0] + self.ext()

    def update_name(self):
        self.name_label.set_text(self.default_name())

    def save_as(self):
        if self.converting:
            return
        self.set_playing(False)
        dialog = Gtk.FileChooserDialog(title="화면 녹화 저장", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("취소", Gtk.ResponseType.CANCEL, "저장", Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        os.makedirs(os.path.dirname(self.target_path), exist_ok=True)
        dialog.set_current_folder(os.path.dirname(self.target_path))
        dialog.set_current_name(self.default_name())
        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not path:
            return
        if not path.lower().endswith((".mp4", ".gif")):
            path += self.ext()
        if path.lower().endswith(".gif"):
            self.convert_gif(path)
            return
        self.player.set_state(self.Gst.State.NULL)
        try:
            shutil.move(self.temp_path, path)
        except OSError as exc:
            self.header.set_subtitle(f"저장 실패: {exc}")
            return
        self.finish_save(path)

    def convert_gif(self, path):
        """Convert in the background with a generated palette for decent colours."""
        self.converting = True
        self.save_btn.set_sensitive(False)
        self.header.set_subtitle("GIF로 변환하는 중…")
        proc = subprocess.Popen(gif_command(self.temp_path, path), stdin=subprocess.DEVNULL)

        def done(_pid, status):
            self.converting = False
            if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 and os.path.exists(path):
                self.finish_save(path)
            else:
                self.save_btn.set_sensitive(True)
                self.header.set_subtitle("GIF 변환에 실패했습니다")
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid, done)

    def finish_save(self, path):
        self.saved = True
        print(path, flush=True)
        notify_saved("화면 녹화를 저장했습니다", path, "video-x-generic")
        self.destroy()

    def on_destroy(self, *_):
        GLib.source_remove(self.progress_timer)
        self.player.set_state(self.Gst.State.NULL)
        if not self.saved:
            try:
                os.unlink(self.temp_path)
            except OSError:
                pass
        self.on_close()


# ---------------------------------------------------------------------------
# Capture flows
# ---------------------------------------------------------------------------

class App:
    def __init__(self, report_fd=None):
        self.exit_code = 0
        self.keep_alive = False
        self.report_fd = report_fd
        self.pending = set()
        # Dialog options to reopen with after a screenshot; None outside the dialog flow.
        self.dialog_settings = None

    def report(self, path):
        """Print the saved path; when detached, also release the waiting parent."""
        if path:
            print(path, flush=True)
        if self.report_fd is not None:
            os.write(self.report_fd, (path or "").encode() + b"\n")
            os.close(self.report_fd)
            self.report_fd = None

    def quit(self, code=None):
        if code is not None:
            self.exit_code = code
        Gtk.main_quit()

    # -- target selection --------------------------------------------------

    def grab_screen(self, interactive=False):
        """Whole-desktop pixbuf, or None (after notifying / reopening) on failure."""
        try:
            return portal_screenshot(interactive)
        except PortalCancelled:
            pass
        except GLib.Error as exc:
            notify(APP_NAME, f"화면을 캡처하지 못했습니다: {exc.message}", "dialog-error")
        if self.dialog_settings:
            self.show_dialog()
        else:
            self.quit(1)
        return None

    def select_area(self, callback):
        """Let the user drag out an area on the live screen, then callback(rect or None)."""
        SelectionOverlay(callback).present_and_grab()

    # -- screenshot --------------------------------------------------------

    def screenshot(self, target, delay=0, output=None, clipboard=True, preview=True):
        def capture(rect=None):
            if target == "window":
                # Windows can't be listed on Wayland; GNOME's own picker hands back the window.
                shot = self.grab_screen(interactive=True)
                rect = shot and (0, 0, shot.get_width(), shot.get_height())
            else:
                shot = self.grab_screen()
            if shot is None:
                return False
            if target == "all":
                rect = (0, 0, shot.get_width(), shot.get_height())
            elif target == "screen":
                rect = pointer_monitor_rect()
            self.save_shot(clamp_rect(rect, shot.get_width(), shot.get_height()), shot,
                           output, clipboard, preview)
            return False

        def chosen(rect):
            if rect is None:
                self.save_shot(None, None, output, clipboard, preview)
            else:
                countdown(delay, lambda: capture(rect), rect)

        if target == "area":
            GLib.timeout_add(1, lambda: (self.select_area(chosen), False)[1])
        else:
            countdown(delay, capture)

    def save_shot(self, rect, frozen, output, clipboard, preview):
        if rect is None:
            if self.dialog_settings:
                self.show_dialog()
            else:
                self.quit(1)
            return
        x, y, w, h = rect
        pixbuf = without_alpha(frozen.new_subpixbuf(x, y, w, h))
        path = output or default_output(video=False)
        if preview:
            # Nothing is written yet; the preview window saves on request.
            self.report(None)
        else:
            pixbuf.savev(path, "png", [], [])
            self.report(path)
            notify_saved("스크린샷을 저장했습니다", path, "camera-photo",
                         "클립보드에도 복사되었습니다." if clipboard else "")
        if not (clipboard or preview):
            self.quit(0)
            return

        # Stay alive while the preview is open and/or we own the clipboard.
        if clipboard:
            self.pending.add("clipboard")
            self.hold_clipboard(pixbuf)
        if preview:
            self.pending.add("preview")
            PreviewWindow(pixbuf, path, clipboard, lambda: self.preview_closed()).show_all()

    def preview_closed(self):
        if self.dialog_settings:
            self.show_dialog()
        else:
            self.release("preview")

    def show_dialog(self, video=False):
        self.pending.discard("preview")
        self.pending.add("dialog")
        kwargs = dict(self.dialog_settings or {})
        kwargs.setdefault("video", video)
        CaptureDialog(self, **kwargs).show_all()

    def dialog_closed(self):
        self.dialog_settings = None
        self.release("dialog")

    def release(self, what):
        self.pending.discard(what)
        if not self.pending:
            self.quit(0)

    def hold_clipboard(self, pixbuf):
        """Own the clipboard until another app replaces it (like xclip does)."""
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_image(pixbuf)
        cb.store()
        def on_owner_change(_cb, event):
            # Another client owns the selection now: its window is foreign (or unknown).
            owner = event.owner
            if owner is None or owner.get_window_type() == Gdk.WindowType.FOREIGN:
                self.release("clipboard")

        cb.connect("owner-change", on_owner_change)
        GLib.timeout_add_seconds(3600, lambda: self.release("clipboard"))

    # -- screencast --------------------------------------------------------

    def record(self, target, pointer=True, audio=False, delay=0, output=None, fps=30, preview=True):
        if read_pid() is not None:
            notify(APP_NAME, "이미 녹화 중입니다.", "dialog-warning")
            self.quit(1)
            return

        def chosen(rect):
            start = lambda: (self.start_recording(target, rect, pointer, audio, output, fps,  # noqa: E731
                                                  preview), False)[1]
            # Pick the area first, then count down over it before recording starts.
            if rect is None or delay <= 0:
                start()
            else:
                countdown(delay, start, None if target == "window" else rect)

        def go():
            if target == "window":
                chosen((0, 0, 0, 0))  # the portal asks which window
            elif target == "screen":
                chosen(pointer_monitor_rect())
            elif target == "all":
                chosen((0, 0, *root_size()))
            else:
                self.select_area(chosen)
            return False
        GLib.timeout_add(1, go)

    def start_recording(self, target, rect, pointer, audio, output, fps, preview):
        if rect is None:
            if self.dialog_settings:
                self.show_dialog()
            else:
                self.quit(1)
            return
        window = target == "window"
        self.screencast = ScreenCast()
        try:
            self.screencast.start(window, pointer, None if window else monitor_key(rect))
        except (PortalCancelled, GLib.Error, KeyError, IndexError) as exc:
            if not isinstance(exc, PortalCancelled):
                notify(APP_NAME, f"화면 녹화를 시작하지 못했습니다: {exc}", "dialog-error")
            self.start_recording(target, None, pointer, audio, output, fps, preview)
            return

        # Crop the monitor stream down to the chosen area (stream coords are relative to it).
        sw, sh = self.screencast.props.get("size", (0, 0))
        crop = None
        frame = None
        if window or target == "all" or not sw:
            w, h = sw, sh
        else:
            px, py = self.screencast.props.get("position", (0, 0))
            s = scale_factor()
            x, y, w, h = rect
            x, y = x // s - px, y // s - py
            left, top = max(x, 0), max(y, 0)
            w = min(x + w // s, sw) - left
            h = min(y + h // s, sh) - top
            w, h = w - w % 2, h - h % 2  # I420 needs even dimensions
            if w <= 0 or h <= 0:
                left, top, w, h = 0, 0, sw - sw % 2, sh - sh % 2
            crop = (left, top, sw - left - w, sh - top - h)
            frame = ((left + px) * s, (top + py) * s, w * s, h * s)

        self.rec_target = output or default_output(video=True)
        self.rec_preview = preview and VideoPreviewWindow.available()
        if self.rec_preview:
            # Record to a scratch file; the preview decides where (or whether) it is kept.
            cache = os.path.join(GLib.get_user_cache_dir(), APP_ID)
            os.makedirs(cache, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="recording-", suffix=".mp4", dir=cache)
            os.close(fd)
        elif self.rec_target.lower().endswith(".gif"):
            fd, path = tempfile.mkstemp(prefix="recording-", suffix=".mp4")
            os.close(fd)
        else:
            path = self.rec_target
        audio_device = None
        if audio:
            sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
            audio_device = f"{sink}.monitor" if sink else None
        cmd = gst_record_command(self.screencast.fd, self.screencast.node, crop, fps, audio_device, path)

        self.rec_path = path
        self.rec_size = (w or 1280, h or 720)
        self.rec_frame = RecordingFrame(frame) if frame else None
        if self.rec_frame is not None:
            self.rec_frame.show_all()
        self.rec_started = GLib.get_monotonic_time()
        self.recorder = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, pass_fds=(self.screencast.fd,))
        self.keep_alive = True
        write_pid()
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.recorder.pid, self.on_recorder_exit)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.stop_recording)
        self.setup_indicator()
        notify("화면 녹화를 시작했습니다",
               "상단 표시줄의 녹화 표시를 클릭하거나 Ctrl+Shift+Alt+R을 누르면 중지됩니다.", "media-record")

    def setup_indicator(self):
        self.indicator = RecordingIndicator(self.stop_recording)
        GLib.timeout_add_seconds(1, self.tick)
        self.tick()

    def tick(self):
        if self.recorder.poll() is not None:
            return False
        secs = (GLib.get_monotonic_time() - self.rec_started) // 1_000_000
        self.indicator.set_elapsed(f"{secs // 60:02d}:{secs % 60:02d}")
        return True

    def stop_recording(self, *_):
        if self.recorder.poll() is not None:
            # Recording is over (only signals get here now): close windows, which also
            # discards an unsaved recording, and exit.
            for win in Gtk.Window.list_toplevels():
                if isinstance(win, (VideoPreviewWindow, CaptureDialog)):
                    win.destroy()
            self.quit(0)
            return True
        # gst-launch -e turns SIGINT into EOS, which finalises the MP4.
        self.recorder.send_signal(signal.SIGINT)
        return True

    def on_recorder_exit(self, _pid, status):
        clear_pid()
        self.screencast.close()
        self.indicator.close()
        if self.rec_frame is not None:
            self.rec_frame.destroy()
            self.rec_frame = None
        if not (os.path.exists(self.rec_path) and os.path.getsize(self.rec_path) > 0):
            notify(APP_NAME, "녹화에 실패했습니다.", "dialog-error")
            self.quit(1)
            return
        if self.rec_preview:
            self.pending.add("preview")
            VideoPreviewWindow(self.rec_path, self.rec_target, self.rec_size,
                               lambda: self.preview_closed()).show_all()
        else:
            if self.rec_path != self.rec_target:
                ok = subprocess.run(gif_command(self.rec_path, self.rec_target)).returncode == 0
                os.unlink(self.rec_path)
                if not ok:
                    notify(APP_NAME, "GIF 변환에 실패했습니다.", "dialog-error")
                    self.quit(1)
                    return
            print(self.rec_target)
            notify_saved("화면 녹화를 저장했습니다", self.rec_target, "video-x-generic")
            self.quit(0)


# ---------------------------------------------------------------------------
# Recording pid file (used by `snapshot stop`)
# ---------------------------------------------------------------------------

def read_pid():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def clear_pid():
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main dialog (GNOME screenshot UI look-alike)
# ---------------------------------------------------------------------------

DIALOG_CSS = b"""
.snapshot-window { background-color: transparent; }
.snapshot-panel {
    background-color: #242428;
    border-radius: 22px;
    padding: 16px 18px 18px 18px;
    color: #ffffff;
}
.snapshot-panel label { color: #ffffff; }
.snapshot-title { font-weight: bold; font-size: 15px; }
.snapshot-panel button {
    background-image: none;
    background-color: #36363b;
    border: none;
    box-shadow: none;
    text-shadow: none;
    -gtk-icon-shadow: none;
    color: #ffffff;
    outline-color: transparent;
}
.snapshot-panel button:hover { background-color: #44444a; }
.snapshot-panel button:checked,
.snapshot-panel button.active { background-color: #3584e4; }
.snapshot-panel button:checked:hover { background-color: #4a92ea; }
.snapshot-panel button image { color: #ffffff; }
.target {
    border-radius: 14px;
    padding: 14px 0 10px 0;
    min-width: 124px;
}
.target label { font-size: 13px; }
.chip { border-radius: 999px; min-width: 36px; min-height: 36px; padding: 0; }
.chip.delay label { font-weight: bold; font-size: 14px; }
.mode button { min-width: 44px; min-height: 36px; padding: 0; }
.mode button:first-child { border-radius: 999px 0 0 999px; }
.mode button:last-child { border-radius: 0 999px 999px 0; }
.close { border-radius: 999px; min-width: 28px; min-height: 28px; padding: 0; }
.snapshot-panel button.shutter {
    min-width: 44px;
    min-height: 44px;
    padding: 0;
    border-radius: 999px;
    background-color: #ffffff;
    border: 3px solid #242428;
    box-shadow: 0 0 0 3px #ffffff;
}
.snapshot-panel button.shutter:hover { background-color: #e6e6e6; }
.snapshot-panel button.shutter.recording { background-color: #e01b24; }
.snapshot-panel button.shutter.recording:hover { background-color: #c01c28; }
"""

DELAYS = (0, 3, 5, 10)
_css_loaded = False


class CaptureDialog(Gtk.Window):
    """Floating dark panel modelled on the GNOME Shell screenshot UI."""

    BOTTOM_MARGIN = 80

    def __init__(self, app, video=False, target="screen", pointer=False, delay=0, audio=False):
        super().__init__(title=APP_NAME)
        self.app = app
        self.set_resizable(False)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_icon_name(APP_ID)
        self.connect("delete-event", lambda *_: app.dialog_closed())
        self.connect("size-allocate", self.place)
        self.connect("key-press-event", self.on_key)
        # Losing focus (e.g. a click outside the panel) dismisses it. Take focus
        # explicitly on map, since a shortcut launch may not hand it to us.
        self.connect("map-event", self.take_focus)
        self.connect("notify::is-active", self.on_active_changed)

        # Transparent window so the panel's rounded corners show.
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_app_paintable(True)
        global _css_loaded
        if not _css_loaded:
            provider = Gtk.CssProvider()
            provider.load_from_data(DIALOG_CSS)
            Gtk.StyleContext.add_provider_for_screen(self.get_screen(), provider,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            _css_loaded = True
        self.get_style_context().add_class("snapshot-window")

        # The whole panel is a drag handle since there is no title bar.
        handle = Gtk.EventBox()
        handle.connect("button-press-event", self.on_drag)
        self.add(handle)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        panel.get_style_context().add_class("snapshot-panel")
        handle.add(panel)

        # Header: title + close
        header = Gtk.Box(spacing=8)
        self.title_label = Gtk.Label(xalign=0)
        self.title_label.get_style_context().add_class("snapshot-title")
        header.pack_start(self.title_label, True, True, 0)
        close = self.icon_button("window-close-symbolic", "닫기", "close")
        close.connect("clicked", lambda *_: self.close_dialog())
        header.pack_end(close, False, False, 0)
        panel.pack_start(header, False, False, 0)

        # Targets
        target_box = Gtk.Box(spacing=10, homogeneous=True)
        self.target_buttons = {}
        group = None
        for key, label, icon in (("area", "선택 영역", "edit-select-all-symbolic"),
                                 ("window", "창", "focus-windows-symbolic"),
                                 ("screen", "화면", "video-display-symbolic")):
            btn = Gtk.RadioButton.new_from_widget(group)
            btn.set_mode(False)
            btn.get_style_context().add_class("target")
            group = group or btn
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DND)
            image.set_pixel_size(32)
            inner.pack_start(image, False, False, 0)
            inner.pack_start(Gtk.Label(label=label), False, False, 0)
            btn.add(inner)
            target_box.pack_start(btn, True, True, 0)
            self.target_buttons[key] = btn
        self.target_buttons[target].set_active(True)
        panel.pack_start(target_box, False, False, 0)

        # Bottom bar: options | shutter | mode
        bar = Gtk.Box(spacing=8)
        options = Gtk.Box(spacing=8)
        self.pointer_chk = self.icon_button("input-mouse-symbolic", "마우스 포인터 표시", "chip", toggle=True)
        self.pointer_chk.set_active(pointer)
        # The portal screenshot never includes the pointer, so this only affects recordings.
        self.pointer_chk.set_no_show_all(True)
        self.pointer_chk.get_child().show()
        options.pack_start(self.pointer_chk, False, False, 0)
        self.audio_chk = self.icon_button("audio-volume-high-symbolic", "시스템 소리 녹음", "chip", toggle=True)
        self.audio_chk.set_active(audio)
        self.audio_chk.get_child().show()
        self.audio_chk.set_no_show_all(True)
        options.pack_start(self.audio_chk, False, False, 0)
        self.delay = delay if delay in DELAYS else DELAYS[0]
        self.delay_btn = Gtk.Button()
        self.delay_btn.get_style_context().add_class("chip")
        self.delay_btn.get_style_context().add_class("delay")
        self.delay_btn.set_tooltip_text("지연 시간")
        self.delay_btn.set_valign(Gtk.Align.CENTER)
        # Timer icon when off, otherwise just the number of seconds in the same round chip.
        self.delay_stack = Gtk.Stack()
        timer_icon = Gtk.Image.new_from_icon_name("alarm-symbolic", Gtk.IconSize.BUTTON)
        timer_icon.show()
        self.delay_stack.add_named(timer_icon, "off")
        self.delay_label = Gtk.Label()
        self.delay_label.show()
        self.delay_stack.add_named(self.delay_label, "on")
        self.delay_btn.add(self.delay_stack)
        self.delay_btn.connect("clicked", self.cycle_delay)
        options.pack_start(self.delay_btn, False, False, 0)
        bar.pack_start(options, False, False, 0)

        self.capture_btn = Gtk.Button()
        self.capture_btn.get_style_context().add_class("shutter")
        self.capture_btn.set_valign(Gtk.Align.CENTER)
        self.capture_btn.connect("clicked", self.on_capture)
        bar.set_center_widget(self.capture_btn)

        mode_box = Gtk.Box()
        mode_box.get_style_context().add_class("mode")
        self.shot_btn = Gtk.RadioButton.new_from_widget(None)
        self.video_btn = Gtk.RadioButton.new_from_widget(self.shot_btn)
        for btn, icon, tip in ((self.shot_btn, "camera-photo-symbolic", "스크린샷"),
                               (self.video_btn, "camera-video-symbolic", "화면 녹화")):
            btn.set_mode(False)
            btn.set_tooltip_text(tip)
            btn.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            mode_box.pack_start(btn, False, False, 0)
        self.video_btn.set_active(video)
        self.video_btn.connect("toggled", lambda *_: self.sync())
        mode_box.set_valign(Gtk.Align.CENTER)
        bar.pack_end(mode_box, False, False, 0)
        panel.pack_start(bar, False, False, 0)

        self.update_delay_label()
        self.sync()

    def icon_button(self, icon, tooltip, css, toggle=False):
        btn = Gtk.ToggleButton() if toggle else Gtk.Button()
        btn.add(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        btn.set_tooltip_text(tooltip)
        btn.set_valign(Gtk.Align.CENTER)
        btn.get_style_context().add_class(css)
        return btn

    def place(self, *_):
        """Pin the panel to the bottom centre of the monitor under the pointer (once)."""
        if getattr(self, "placed", False):
            return
        self.placed = True
        display = Gdk.Display.get_default()
        _, px, py = display.get_default_seat().get_pointer().get_position()[:3]
        work = display.get_monitor_at_point(px, py).get_workarea()
        width, height = self.get_size()
        self.move(work.x + (work.width - width) // 2,
                  work.y + work.height - height - self.BOTTOM_MARGIN)

    def on_drag(self, _w, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return False

    def cycle_delay(self, *_):
        self.delay = DELAYS[(DELAYS.index(self.delay) + 1) % len(DELAYS)]
        self.update_delay_label()

    def update_delay_label(self):
        self.delay_label.set_text(str(self.delay))
        self.delay_stack.set_visible_child_name("on" if self.delay else "off")
        self.delay_btn.set_tooltip_text(f"지연 시간: {self.delay}초" if self.delay else "지연 시간: 없음")
        ctx = self.delay_btn.get_style_context()
        (ctx.add_class if self.delay else ctx.remove_class)("active")

    def take_focus(self, *_):
        window = self.get_window()
        window.focus(GdkX11.x11_get_server_time(window))
        return False

    def on_active_changed(self, *_):
        if self.is_active():
            self.had_focus = True
        elif getattr(self, "had_focus", False):
            self.close_dialog()

    def close_dialog(self):
        self.destroy()
        self.app.dialog_closed()

    def sync(self):
        video = self.video_btn.get_active()
        self.audio_chk.set_visible(video)
        self.pointer_chk.set_visible(video)
        self.title_label.set_text("화면 녹화" if video else "스크린샷")
        self.capture_btn.set_tooltip_text("녹화 시작" if video else "캡처")
        ctx = self.capture_btn.get_style_context()
        (ctx.add_class if video else ctx.remove_class)("recording")

    def on_key(self, _w, event):
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape:
            self.close_dialog()
        elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.on_capture()
        elif key == Gdk.KEY_s:
            self.target_buttons["area"].set_active(True)
        elif key == Gdk.KEY_c:
            self.target_buttons["screen"].set_active(True)
        elif key == Gdk.KEY_w:
            self.target_buttons["window"].set_active(True)
        elif key == Gdk.KEY_v:
            (self.shot_btn if self.video_btn.get_active() else self.video_btn).set_active(True)
        else:
            return False
        return True

    def on_capture(self, *_):
        target = next(k for k, b in self.target_buttons.items() if b.get_active())
        pointer = self.pointer_chk.get_active()
        delay = self.delay
        video = self.video_btn.get_active()
        audio = self.audio_chk.get_active()
        self.app.pending.discard("dialog")
        self.app.dialog_settings = {"target": target, "pointer": pointer, "delay": delay,
                                    "video": video, "audio": audio}
        self.destroy()

        # Wait for the dialog to disappear from the screen before grabbing.
        def go():
            if video:
                self.app.record(target, pointer=pointer, audio=audio, delay=delay)
            else:
                self.app.screenshot(target, delay=delay)
            return False
        GLib.timeout_add(250, go)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(prog="snapshot", description="Screenshot and screencast tool (GNOME Wayland)")
    sub = p.add_subparsers(dest="cmd")

    ui = sub.add_parser("ui", help="open the capture dialog (default)")
    ui.add_argument("--video", action="store_true", help="start in screencast mode")

    shot = sub.add_parser("shot", help="take a screenshot")
    shot.add_argument("target", choices=TARGETS)
    shot.add_argument("-p", "--pointer", action="store_true", help=argparse.SUPPRESS)  # unsupported on Wayland
    shot.add_argument("-d", "--delay", type=float, default=0)
    shot.add_argument("-o", "--output")
    shot.add_argument("--no-clipboard", action="store_true")
    shot.add_argument("--no-preview", action="store_true", help="do not show the result window")

    rec = sub.add_parser("record", help="start a screencast")
    rec.add_argument("target", choices=TARGETS)
    rec.add_argument("--no-pointer", action="store_true")
    rec.add_argument("-a", "--audio", action="store_true", help="record system audio")
    rec.add_argument("-d", "--delay", type=float, default=0)
    rec.add_argument("-o", "--output")
    rec.add_argument("--fps", type=int, default=30)
    rec.add_argument("--no-preview", action="store_true", help="save directly without the preview window")

    sub.add_parser("stop", help="stop the running screencast")
    ns = sub.add_parser("notify-saved", help=argparse.SUPPRESS)
    ns.add_argument("path")
    ns.add_argument("summary")
    ns.add_argument("icon")
    ns.add_argument("extra", nargs="?", default="")
    sub.add_parser("toggle-record", help="stop if recording, otherwise open the dialog in screencast mode")
    return p.parse_args(argv)


def detach(argv):
    """Run the capture in a new process so the shell gets control back once the file
    is saved, while the child keeps the preview and clipboard alive. (A plain fork
    won't do: GTK has already started GDBus threads, which a forked child lacks.)"""
    r, w = os.pipe()
    env = dict(os.environ, SNAPSHOT_REPORT_FD=str(w))
    subprocess.Popen([sys.executable, os.path.abspath(__file__), *argv], pass_fds=(w,), env=env,
                     start_new_session=True, stdin=subprocess.DEVNULL)
    os.close(w)
    data = b""
    while chunk := os.read(r, 4096):
        data += chunk
    return 0 if data else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    cmd = args.cmd or "ui"

    if cmd == "notify-saved":
        return run_notify_saved(args.path, args.summary, args.icon, args.extra)

    if cmd in ("stop", "toggle-record"):
        pid = read_pid()
        if pid is not None:
            os.kill(pid, signal.SIGUSR1)
            return 0
        if cmd == "stop":
            print("녹화 중이 아닙니다.", file=sys.stderr)
            return 1
        cmd, args.video = "ui", True

    if not check_session():
        return 1
    report_fd = None
    if cmd == "shot":
        if "SNAPSHOT_REPORT_FD" not in os.environ:
            return detach(argv)
        report_fd = int(os.environ.pop("SNAPSHOT_REPORT_FD"))
    Notify.init(APP_NAME)
    app = App(report_fd)

    if cmd == "ui":
        app.show_dialog(video=getattr(args, "video", False))
    elif cmd == "shot":
        app.screenshot(args.target, delay=args.delay,
                       output=args.output, clipboard=not args.no_clipboard,
                       preview=not args.no_preview)
    elif cmd == "record":
        app.record(args.target, pointer=not args.no_pointer, audio=args.audio,
                   delay=args.delay, output=args.output, fps=args.fps, preview=not args.no_preview)

    Gtk.main()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
