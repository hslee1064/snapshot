#!/usr/bin/python3
"""Snapshot - a GNOME-style screenshot / screencast tool for Ubuntu (X11).

Targets: selection (area), window, screen (monitor under pointer), all (every monitor).
Screenshots are grabbed with ffmpeg x11grab (so the pointer can be included),
frozen on an overlay for selection, saved as PNG and copied to the clipboard.
Screencasts are recorded with ffmpeg x11grab (+ optional PulseAudio monitor) to MP4.
"""

import argparse
import datetime
import os
import shutil
import signal
import subprocess
import sys
import tempfile

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Wnck", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Notify, Wnck  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402
    except (ValueError, ImportError):
        AppIndicator = None

APP_ID = "snapshot"
APP_NAME = "Snapshot"
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
    if video:
        folder = os.path.join(xdg_dir("VIDEOS", "~/Videos"), "Screencasts")
        name = f"Screencast from {now}.mp4"
    else:
        folder = os.path.join(xdg_dir("PICTURES", "~/Pictures"), "Screenshots")
        name = f"Screenshot from {now}.png"
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def display_name():
    return os.environ.get("DISPLAY", ":0")


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


def notify(summary, body="", icon="camera-photo"):
    try:
        n = Notify.Notification.new(summary, body, icon)
        n.set_hint("desktop-entry", GLib.Variant("s", APP_ID))
        n.show()
    except GLib.Error:
        pass


def check_session():
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or not os.environ.get("DISPLAY"):
        msg = ("Snapshot은 X11(Ubuntu on Xorg) 세션에서만 동작합니다.\n"
               "로그인 화면 우측 하단 톱니바퀴에서 'Ubuntu on Xorg'를 선택하세요.")
        print(msg, file=sys.stderr)
        notify(APP_NAME, msg, "dialog-error")
        return False
    if not shutil.which("ffmpeg"):
        print("ffmpeg가 필요합니다: sudo apt install ffmpeg", file=sys.stderr)
        return False
    return True


def grab_root(path, pointer):
    """Grab the whole X root window into a PNG using ffmpeg."""
    w, h = root_size()
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab",
           "-draw_mouse", "1" if pointer else "0",
           "-video_size", f"{w}x{h}", "-i", f"{display_name()}+0,0",
           "-frames:v", "1", path]
    subprocess.run(cmd, check=True)


def frame_extents(xid):
    """Return _GTK_FRAME_EXTENTS (left, right, top, bottom) for CSD windows."""
    try:
        from Xlib import display as xdisplay
        d = xdisplay.Display()
        atom = d.intern_atom("_GTK_FRAME_EXTENTS")
        prop = d.create_resource_object("window", xid).get_full_property(atom, 0)
        d.close()
        if prop and len(prop.value) == 4:
            return tuple(int(v) for v in prop.value)
    except Exception:
        pass
    return 0, 0, 0, 0


def visible_windows():
    """Visible top-level windows, topmost first, as (title, (x, y, w, h)) in physical px."""
    screen = Wnck.Screen.get_default()
    screen.force_update()
    ws = screen.get_active_workspace()
    allowed = {Wnck.WindowType.NORMAL, Wnck.WindowType.DIALOG,
               Wnck.WindowType.UTILITY, Wnck.WindowType.MENU}
    result = []
    for win in reversed(screen.get_windows_stacked()):
        if win.is_minimized() or win.get_window_type() not in allowed:
            continue
        if ws is not None and not win.is_visible_on_workspace(ws):
            continue
        x, y, w, h = win.get_geometry()
        left, right, top, bottom = frame_extents(win.get_xid())
        rect = (x + left, y + top, w - left - right, h - top - bottom)
        if rect[2] > 0 and rect[3] > 0:
            result.append((win.get_name(), rect))
    return result


def clamp_rect(rect, width, height):
    x, y, w, h = rect
    x2, y2 = min(x + w, width), min(y + h, height)
    x, y = max(x, 0), max(y, 0)
    return x, y, max(x2 - x, 0), max(y2 - y, 0)


# ---------------------------------------------------------------------------
# Selection overlay
# ---------------------------------------------------------------------------

class SelectionOverlay(Gtk.Window):
    """Fullscreen overlay over a frozen screenshot to pick an area or a window.

    Calls on_done(rect) with a physical-pixel rect, or on_done(None) when cancelled.
    """

    def __init__(self, background, mode, on_done):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.bg = background
        self.mode = mode
        self.on_done = on_done
        self.scale = scale_factor()
        self.windows = visible_windows() if mode == "window" else []
        self.start = None
        self.current = None
        self.hover = None
        self.finished = False

        w, h = root_size()
        self.move(0, 0)
        self.set_default_size(w // self.scale, h // self.scale)
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
        cursor = Gdk.Cursor.new_for_display(
            Gdk.Display.get_default(),
            Gdk.CursorType.CROSSHAIR if self.mode == "area" else Gdk.CursorType.HAND2)
        seat = Gdk.Display.get_default().get_default_seat()
        # Retry briefly: the grab can fail until the popup is actually mapped.
        def try_grab(attempt=[0]):
            status = seat.grab(gdk_win, Gdk.SeatCapabilities.ALL, True, cursor, None, None)
            attempt[0] += 1
            if status != Gdk.GrabStatus.SUCCESS and attempt[0] < 20:
                return True
            if self.mode == "window":
                self.update_hover(*self.pointer_pos())
            return False
        GLib.timeout_add(25, try_grab)

    def pointer_pos(self):
        _, x, y, _ = self.get_window().get_pointer()
        return x, y

    # -- geometry ----------------------------------------------------------

    def selection_logical(self):
        if self.mode == "window":
            if self.hover is None:
                return None
            x, y, w, h = self.hover[1]
            s = self.scale
            return x / s, y / s, w / s, h / s
        if self.start is None or self.current is None:
            return None
        (x1, y1), (x2, y2) = self.start, self.current
        return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)

    def update_hover(self, x, y):
        px, py = x * self.scale, y * self.scale
        hover = None
        for title, (wx, wy, ww, wh) in self.windows:
            if wx <= px < wx + ww and wy <= py < wy + wh:
                hover = (title, (wx, wy, ww, wh))
                break
        if hover != self.hover:
            self.hover = hover
            self.area.queue_draw()

    # -- drawing -----------------------------------------------------------

    def on_draw(self, widget, cr):
        cr.save()
        cr.scale(1 / self.scale, 1 / self.scale)
        Gdk.cairo_set_source_pixbuf(cr, self.bg, 0, 0)
        cr.paint()
        cr.restore()

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
            if self.mode == "window" and self.hover:
                label = f"{self.hover[0]}  ·  {label}"
            self.draw_label(cr, label, x, y + h + 8, height)
        else:
            hint = ("드래그해서 영역을 선택하세요 · Esc 취소" if self.mode == "area"
                    else "캡처할 창을 클릭하세요 · Esc 취소")
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
        elif event.button == 1 and self.mode == "area":
            self.start = self.current = (event.x, event.y)
        return True

    def on_motion(self, widget, event):
        if self.mode == "window":
            self.update_hover(event.x, event.y)
        elif self.start is not None:
            self.current = (event.x, event.y)
        widget.queue_draw()
        return True

    def on_release(self, widget, event):
        if event.button != 1:
            return True
        if self.mode == "window":
            self.update_hover(event.x, event.y)
            if self.hover:
                self.finish(self.hover[1])
            return True
        if self.start is None:
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
        elif event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.mode == "window" and self.hover:
            self.finish(self.hover[1])
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
    """Shows the captured image with quick actions, like GNOME's screenshot result."""

    MAX_W, MAX_H = 960, 640

    def __init__(self, pixbuf, path, copied, on_close):
        super().__init__(title="스크린샷")
        self.pixbuf = pixbuf
        self.path = path
        self.on_close = on_close
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_icon_name("applets-screenshooter")
        self.connect("destroy", lambda *_: self.on_close())
        self.connect("key-press-event", self.on_key)

        header = Gtk.HeaderBar(title="스크린샷을 저장했습니다", show_close_button=True)
        header.set_subtitle("클립보드에 복사됨" if copied else os.path.basename(path))
        self.set_titlebar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_border_width(12)
        self.add(box)

        w, h = pixbuf.get_width(), pixbuf.get_height()
        ratio = min(self.MAX_W / w, self.MAX_H / h, 1.0)
        thumb = pixbuf.scale_simple(max(int(w * ratio), 1), max(int(h * ratio), 1),
                                    GdkPixbuf.InterpType.BILINEAR)
        image = Gtk.Image.new_from_pixbuf(thumb)
        frame = Gtk.EventBox()
        frame.add(image)
        frame.set_tooltip_text("더블클릭하면 이미지 뷰어로 엽니다 · 끌어다 놓기 가능")
        frame.connect("button-press-event", self.on_image_click)
        # Drag the file out into other apps (chat, browser, file manager).
        frame.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.COPY)
        frame.drag_source_add_uri_targets()
        frame.drag_source_set_icon_pixbuf(thumb.scale_simple(
            max(thumb.get_width() // 4, 1), max(thumb.get_height() // 4, 1), GdkPixbuf.InterpType.BILINEAR))
        frame.connect("drag-data-get", lambda _w, _c, data, *_: data.set_uris([GLib.filename_to_uri(path)]))
        box.pack_start(frame, True, True, 0)

        info = Gtk.Label(label=f"{path}  ·  {w} × {h}", xalign=0, selectable=True)
        info.set_ellipsize(3)  # Pango.EllipsizeMode.END
        info.get_style_context().add_class("dim-label")
        box.pack_start(info, False, False, 0)

        actions = Gtk.Box(spacing=8)
        for label, cb in (("열기", self.open_image), ("폴더에서 보기", self.open_folder),
                          ("복사", self.copy), ("삭제", self.delete)):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, f=cb: f())
            actions.pack_start(btn, False, False, 0)
        close = Gtk.Button(label="닫기")
        close.get_style_context().add_class("suggested-action")
        close.connect("clicked", lambda *_: self.destroy())
        actions.pack_end(close, False, False, 0)
        box.pack_start(actions, False, False, 0)
        close.grab_focus()

    def on_image_click(self, _w, event):
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self.open_image()

    def on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
        elif event.keyval == Gdk.KEY_c and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.copy()
        elif event.keyval == Gdk.KEY_Delete:
            self.delete()
        else:
            return False
        return True

    def open_image(self):
        subprocess.Popen(["xdg-open", self.path])
        self.destroy()

    def open_folder(self):
        # Ask the file manager to select the file; fall back to opening the folder.
        try:
            subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                            "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                            f"array:string:{GLib.filename_to_uri(self.path)}", "string:"],
                           check=True, capture_output=True, timeout=5)
        except (subprocess.SubprocessError, FileNotFoundError):
            subprocess.Popen(["xdg-open", os.path.dirname(self.path)])
        self.destroy()

    def copy(self):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_image(self.pixbuf)
        cb.store()
        self.get_titlebar().set_subtitle("클립보드에 복사됨")

    def delete(self):
        try:
            Gio.File.new_for_path(self.path).trash(None)
        except GLib.Error:
            os.unlink(self.path)
        notify("스크린샷을 휴지통으로 옮겼습니다", self.path, "user-trash")
        self.destroy()


# ---------------------------------------------------------------------------
# Capture flows
# ---------------------------------------------------------------------------

class App:
    def __init__(self, report_fd=None):
        self.exit_code = 0
        self.keep_alive = False
        self.report_fd = report_fd

    def report(self, path):
        """Print the saved path; when detached, also release the waiting parent."""
        print(path, flush=True)
        if self.report_fd is not None:
            os.write(self.report_fd, path.encode() + b"\n")
            os.close(self.report_fd)
            self.report_fd = None

    def quit(self, code=None):
        if code is not None:
            self.exit_code = code
        Gtk.main_quit()

    # -- target selection --------------------------------------------------

    def choose_target(self, target, pointer, callback):
        """Freeze the screen, let the user choose, then callback(rect, frozen_pixbuf)."""
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        try:
            grab_root(tmp.name, pointer)
            frozen = GdkPixbuf.Pixbuf.new_from_file(tmp.name)
        except (subprocess.CalledProcessError, GLib.Error) as exc:
            notify(APP_NAME, f"화면을 캡처하지 못했습니다: {exc}", "dialog-error")
            self.quit(1)
            return
        finally:
            os.unlink(tmp.name)

        if target == "all":
            callback((0, 0, frozen.get_width(), frozen.get_height()), frozen)
        elif target == "screen":
            callback(clamp_rect(pointer_monitor_rect(), frozen.get_width(), frozen.get_height()), frozen)
        else:
            overlay = SelectionOverlay(frozen, target, lambda rect: callback(rect, frozen))
            overlay.present_and_grab()

    # -- screenshot --------------------------------------------------------

    def screenshot(self, target, pointer=True, delay=0, output=None, clipboard=True, preview=True):
        def go():
            self.choose_target(target, pointer, lambda rect, frozen: self.save_shot(rect, frozen, output, clipboard, preview))
            return False
        GLib.timeout_add(max(int(delay * 1000), 1), go)

    def save_shot(self, rect, frozen, output, clipboard, preview):
        if rect is None:
            self.quit(1)
            return
        x, y, w, h = rect
        pixbuf = frozen.new_subpixbuf(x, y, w, h).copy()
        path = output or default_output(video=False)
        pixbuf.savev(path, "png", [], [])
        self.report(path)
        if not preview:
            notify("스크린샷을 저장했습니다",
                   f"{path}\n클립보드에도 복사되었습니다." if clipboard else path, "camera-photo")
        if not (clipboard or preview):
            self.quit(0)
            return

        # Stay alive while the preview is open and/or we own the clipboard.
        self.pending = set()
        if clipboard:
            self.pending.add("clipboard")
            self.hold_clipboard(pixbuf)
        if preview:
            self.pending.add("preview")
            PreviewWindow(pixbuf, path, clipboard, lambda: self.release("preview")).show_all()

    def release(self, what):
        self.pending.discard(what)
        if not self.pending:
            self.quit(0)

    def hold_clipboard(self, pixbuf):
        """Own the clipboard until another app replaces it (like xclip does)."""
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_image(pixbuf)
        cb.store()
        seen = {"ours": False}

        def on_owner_change(_cb, event):
            # First event is our own set_image; a foreign owner arrives as None.
            if event.owner is None and seen["ours"]:
                self.release("clipboard")
            seen["ours"] = True

        cb.connect("owner-change", on_owner_change)
        GLib.timeout_add_seconds(3600, lambda: self.release("clipboard"))

    # -- screencast --------------------------------------------------------

    def record(self, target, pointer=True, audio=False, delay=0, output=None, fps=30):
        if read_pid() is not None:
            notify(APP_NAME, "이미 녹화 중입니다.", "dialog-warning")
            self.quit(1)
            return

        def go():
            self.choose_target(target, False,
                               lambda rect, _frozen: self.start_recording(rect, pointer, audio, output, fps))
            return False
        GLib.timeout_add(max(int(delay * 1000), 1), go)

    def start_recording(self, rect, pointer, audio, output, fps):
        if rect is None:
            self.quit(1)
            return
        x, y, w, h = rect
        w, h = w - w % 2, h - h % 2  # yuv420p needs even dimensions
        path = output or default_output(video=True)
        cmd = ["ffmpeg", "-loglevel", "error", "-y",
               "-thread_queue_size", "512", "-f", "x11grab",
               "-draw_mouse", "1" if pointer else "0", "-framerate", str(fps),
               "-video_size", f"{w}x{h}", "-i", f"{display_name()}+{x},{y}"]
        if audio:
            sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
            if sink:
                cmd += ["-thread_queue_size", "512", "-f", "pulse", "-i", f"{sink}.monitor"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        cmd += ["-movflags", "+faststart", path]

        self.rec_path = path
        self.rec_started = GLib.get_monotonic_time()
        self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.keep_alive = True
        write_pid()
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.ffmpeg.pid, self.on_ffmpeg_exit)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.stop_recording)
        self.setup_indicator()
        notify("화면 녹화를 시작했습니다", "상단 표시줄의 Snapshot 아이콘이나 'snapshot stop'으로 중지하세요.",
               "media-record")

    def setup_indicator(self):
        self.indicator = None
        if AppIndicator is None:
            return
        ind = AppIndicator.Indicator.new(APP_ID, "media-record",
                                         AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        self.stop_item = Gtk.MenuItem(label="녹화 중지 (00:00)")
        self.stop_item.connect("activate", lambda *_: self.stop_recording())
        menu.append(self.stop_item)
        menu.show_all()
        ind.set_menu(menu)
        ind.set_secondary_activate_target(self.stop_item)
        self.indicator = ind
        GLib.timeout_add_seconds(1, self.tick)
        self.tick()

    def tick(self):
        if self.ffmpeg.poll() is not None:
            return False
        secs = (GLib.get_monotonic_time() - self.rec_started) // 1_000_000
        text = f"{secs // 60:02d}:{secs % 60:02d}"
        self.stop_item.set_label(f"녹화 중지 ({text})")
        self.indicator.set_label(text, "00:00")
        return True

    def stop_recording(self, *_):
        if self.ffmpeg.poll() is None:
            try:
                self.ffmpeg.stdin.write(b"q")
                self.ffmpeg.stdin.flush()
            except (BrokenPipeError, OSError):
                self.ffmpeg.terminate()
        return True

    def on_ffmpeg_exit(self, _pid, status):
        clear_pid()
        if self.indicator is not None:
            self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        if os.path.exists(self.rec_path) and os.path.getsize(self.rec_path) > 0:
            print(self.rec_path)
            notify("화면 녹화를 저장했습니다", self.rec_path, "video-x-generic")
            self.quit(0)
        else:
            notify(APP_NAME, "녹화에 실패했습니다.", "dialog-error")
            self.quit(1)


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

class CaptureDialog(Gtk.Window):
    def __init__(self, app, video=False):
        super().__init__(title=APP_NAME)
        self.app = app
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_icon_name("applets-screenshooter")
        self.set_border_width(18)
        self.connect("delete-event", lambda *_: app.quit(1))
        self.connect("key-press-event", self.on_key)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add(box)

        # Target buttons
        target_box = Gtk.Box(spacing=8, homogeneous=True)
        self.target_buttons = {}
        group = None
        for key, label, icon in (("area", "선택 영역", "edit-select-all-symbolic"),
                                 ("screen", "화면", "video-display-symbolic"),
                                 ("window", "창", "focus-windows-symbolic")):
            btn = Gtk.RadioButton.new_from_widget(group)
            btn.set_mode(False)
            group = group or btn
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            inner.set_border_width(10)
            inner.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DND), False, False, 0)
            inner.pack_start(Gtk.Label(label=label), False, False, 0)
            btn.add(inner)
            target_box.pack_start(btn, True, True, 0)
            self.target_buttons[key] = btn
        box.pack_start(target_box, False, False, 0)

        # Screenshot / screencast switch
        mode_box = Gtk.Box(spacing=0)
        mode_box.get_style_context().add_class("linked")
        mode_box.set_halign(Gtk.Align.CENTER)
        self.shot_btn = Gtk.RadioButton.new_with_label_from_widget(None, "📷  스크린샷")
        self.video_btn = Gtk.RadioButton.new_with_label_from_widget(self.shot_btn, "🎥  화면 녹화")
        for b in (self.shot_btn, self.video_btn):
            b.set_mode(False)
            mode_box.pack_start(b, False, False, 0)
        self.video_btn.set_active(video)
        self.video_btn.connect("toggled", lambda *_: self.sync())
        box.pack_start(mode_box, False, False, 0)

        # Options
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.pointer_chk = Gtk.CheckButton(label="마우스 포인터 표시")
        self.pointer_chk.set_active(False)
        grid.attach(self.pointer_chk, 0, 0, 2, 1)
        self.audio_chk = Gtk.CheckButton(label="시스템 소리 녹음")
        grid.attach(self.audio_chk, 0, 1, 2, 1)
        grid.attach(Gtk.Label(label="지연(초)", xalign=0), 0, 2, 1, 1)
        self.delay_spin = Gtk.SpinButton.new_with_range(0, 60, 1)
        grid.attach(self.delay_spin, 1, 2, 1, 1)
        box.pack_start(grid, False, False, 0)

        self.capture_btn = Gtk.Button()
        self.capture_btn.get_style_context().add_class("suggested-action")
        self.capture_btn.connect("clicked", self.on_capture)
        box.pack_start(self.capture_btn, False, False, 0)

        hint = Gtk.Label(label="Enter 캡처 · S/C/W 대상 · V 모드 전환 · Esc 닫기")
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)
        self.sync()

    def sync(self):
        video = self.video_btn.get_active()
        self.audio_chk.set_sensitive(video)
        self.capture_btn.set_label("녹화 시작" if video else "캡처")

    def on_key(self, _w, event):
        key = Gdk.keyval_to_lower(event.keyval)
        if key == Gdk.KEY_Escape:
            self.app.quit(1)
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
        delay = self.delay_spin.get_value()
        video = self.video_btn.get_active()
        audio = self.audio_chk.get_active()
        self.destroy()

        # Wait for the dialog to disappear from the screen before grabbing.
        def go():
            if video:
                self.app.record(target, pointer=pointer, audio=audio, delay=delay)
            else:
                self.app.screenshot(target, pointer=pointer, delay=delay)
            return False
        GLib.timeout_add(250, go)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(prog="snapshot", description="Screenshot and screencast tool (X11)")
    sub = p.add_subparsers(dest="cmd")

    ui = sub.add_parser("ui", help="open the capture dialog (default)")
    ui.add_argument("--video", action="store_true", help="start in screencast mode")

    shot = sub.add_parser("shot", help="take a screenshot")
    shot.add_argument("target", choices=TARGETS)
    shot.add_argument("-p", "--pointer", action="store_true", help="include mouse pointer")
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

    sub.add_parser("stop", help="stop the running screencast")
    sub.add_parser("toggle-record", help="stop if recording, otherwise open the dialog in screencast mode")
    return p.parse_args(argv)


def detach():
    """Fork before GTK/DBus start threads so the shell gets control back once the
    file is saved, while the child keeps the preview and clipboard alive."""
    r, w = os.pipe()
    if os.fork() != 0:
        os.close(w)
        data = b""
        while chunk := os.read(r, 4096):
            data += chunk
        os._exit(0 if data else 1)
    os.close(r)
    os.setsid()
    return w


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cmd = args.cmd or "ui"

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
    report_fd = detach() if cmd == "shot" else None
    Notify.init(APP_NAME)
    app = App(report_fd)

    if cmd == "ui":
        CaptureDialog(app, video=getattr(args, "video", False)).show_all()
    elif cmd == "shot":
        app.screenshot(args.target, pointer=args.pointer, delay=args.delay,
                       output=args.output, clipboard=not args.no_clipboard,
                       preview=not args.no_preview)
    elif cmd == "record":
        app.record(args.target, pointer=not args.no_pointer, audio=args.audio,
                   delay=args.delay, output=args.output, fps=args.fps)

    Gtk.main()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
