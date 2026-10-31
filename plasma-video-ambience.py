#!/usr/bin/env python3
"""Real-time screen ambience: mirror the screen's accent colour on the keyboard.

Captures the screen through the XDG ScreenCast portal (PipeWire) and pauses the
wallpaper sync service while running. The portal grant is remembered, so later
runs start without the monitor-selection dialog.
"""
import os
import signal
import subprocess
import sys
import threading
import time

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from PIL import Image
except ImportError:
    print("Dependencies missing! Ensure python-gobject, python-dbus, python-pillow and gst-plugin-pipewire are installed.")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from backlight_core import (  # noqa: E402
    CACHE_DIR, AuraKeyboard, add_signal_handler, analyze_image, led_rgb, load_config, log,
)

SYNC_SERVICE = "plasma-backlight-sync"
FRAME_W, FRAME_H = 64, 36   # analysis frame (16:9)
ANALYSIS_FPS = 30
OUTPUT_HZ = 30
TOKEN_PATH = os.path.join(CACHE_DIR, "screencast-restore-token")

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"
CURSOR_HIDDEN = 1
PERSIST_UNTIL_REVOKED = 2


def _systemctl_user(*args):
    return subprocess.run(["systemctl", "--user", *args], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def _smoothing_alpha(diff, dt):
    """Adaptive EMA factor: calm scenes blend slowly, cuts and flashes snap.

    Tuned per 60 fps frame; rescaled for the actual tick interval.
    """
    if diff <= 15:
        alpha = 0.08
    elif diff >= 150:
        alpha = 0.85
    else:
        alpha = 0.08 + (diff - 15) * (0.85 - 0.08) / (150 - 15)
    return 1.0 - (1.0 - alpha) ** (dt * 60.0)


class Ambience:
    def __init__(self):
        self.cfg = load_config()
        self.kbd = AuraKeyboard()
        self.bus = dbus.SessionBus()
        portal = self.bus.get_object(PORTAL_BUS, PORTAL_PATH)
        self.screencast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")
        self.cursor_modes = int(portal.Get("org.freedesktop.portal.ScreenCast", "AvailableCursorModes",
                                           dbus_interface=dbus.PROPERTIES_IFACE))
        self.loop = GLib.MainLoop()
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.tokens = 0
        self.session_path = None
        self.pipeline = None
        self._lock = threading.Lock()
        self._target = None         # latest LED colour computed by the streaming thread
        self._last_analysis = 0.0
        self._current = None        # smoothed float RGB shown on the keyboard
        self._last_tick = time.monotonic()

    # ---------------------------------------------------------------- portal

    def _request(self, method, callback, *args, options=None):
        """Call a portal method and route its Request::Response to callback."""
        self.tokens += 1
        token = f"ambience{os.getpid()}_{self.tokens}"
        path = f"{PORTAL_PATH}/request/{self.sender}/{token}"
        match = []

        def on_response(response, results):
            match[0].remove()
            callback(int(response), results)

        match.append(self.bus.add_signal_receiver(
            on_response, signal_name="Response", dbus_interface=REQUEST_IFACE, path=path))
        options = dict(options or {}, handle_token=token)
        method(*args, options)

    def start(self):
        log("Requesting PipeWire screen cast via XDG Desktop Portal...")
        self._request(self.screencast.CreateSession, self._on_created,
                      options={"session_handle_token": f"ambience{os.getpid()}"})

    def _on_created(self, response, results):
        if response != 0:
            return self.quit("Failed to create screen cast session")
        self.session_path = str(results["session_handle"])
        self.bus.add_signal_receiver(self._on_session_closed, signal_name="Closed",
                                     dbus_interface=SESSION_IFACE, path=self.session_path)
        options = {"multiple": False, "types": dbus.UInt32(1),
                   "persist_mode": dbus.UInt32(PERSIST_UNTIL_REVOKED)}
        if self.cursor_modes & CURSOR_HIDDEN:
            options["cursor_mode"] = dbus.UInt32(CURSOR_HIDDEN)
        token = self._load_token()
        if token:
            options["restore_token"] = token
        else:
            log("Awaiting monitor selection...")
        self._request(self.screencast.SelectSources, self._on_selected, self.session_path, options=options)

    def _on_selected(self, response, results):
        if response != 0:
            return self.quit("Failed to select sources")
        self._request(self.screencast.Start, self._on_started, self.session_path, "")

    def _on_started(self, response, results):
        if response != 0:
            return self.quit("Screen cast was not started (prompt cancelled?)")
        streams = results.get("streams", [])
        if not streams:
            return self.quit("Portal returned no streams")
        if "restore_token" in results:
            self._save_token(str(results["restore_token"]))
        node_id = int(streams[0][0])
        fd = self.screencast.OpenPipeWireRemote(self.session_path, {}).take()
        log("PipeWire stream received. Launching ambient lighting...")
        self._start_pipeline(fd, node_id)

    def _on_session_closed(self, *_args):
        self.session_path = None
        self.quit("Portal session closed by compositor/user")

    @staticmethod
    def _load_token():
        try:
            with open(TOKEN_PATH) as f:
                return f.read().strip() or None
        except OSError:
            return None

    @staticmethod
    def _save_token(token):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(TOKEN_PATH, "w") as f:
                f.write(token)
        except OSError as e:
            log(f"Could not store screen cast restore token: {e}")

    # ---------------------------------------------------------------- capture

    def _start_pipeline(self, fd, node_id):
        # The portal hands out a node id, not an object serial, so the
        # deprecated path= is still the correct way to target it.
        self.pipeline = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={node_id} ! videoconvert ! videoscale add-borders=false ! "
            f"video/x-raw,format=RGB,width={FRAME_W},height={FRAME_H} ! "
            "appsink name=sink emit-signals=true drop=true max-buffers=1 sync=false")
        self.pipeline.get_by_name("sink").connect("new-sample", self._on_new_sample)
        gst_bus = self.pipeline.get_bus()
        gst_bus.add_signal_watch()
        gst_bus.connect("message", self._on_gst_message)
        self.pipeline.set_state(Gst.State.PLAYING)
        GLib.timeout_add(1000 // OUTPUT_HZ, self._tick)

    def _on_new_sample(self, sink):
        # Streaming thread: always pull so GStreamer never queues frames.
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        now = time.monotonic()
        if now - self._last_analysis < 1.0 / ANALYSIS_FPS:
            return Gst.FlowReturn.OK
        self._last_analysis = now

        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            frame = bytes(info.data)
        finally:
            buf.unmap(info)
        stride = len(frame) // height
        img = Image.frombuffer("RGB", (width, height), frame, "raw", "RGB", stride, 1)
        rgb = led_rgb(*analyze_image(img), self.cfg)
        with self._lock:
            self._target = rgb
        return Gst.FlowReturn.OK

    def _tick(self):
        # Main thread: smooth toward the latest target and write only on change.
        now = time.monotonic()
        dt, self._last_tick = now - self._last_tick, now
        with self._lock:
            target = self._target
        if target is None:
            return True
        if self._current is None:
            self._current = list(target)
        else:
            diff = sum((c - t) ** 2 for c, t in zip(self._current, target)) ** 0.5
            alpha = _smoothing_alpha(diff, dt)
            self._current = [c + (t - c) * alpha for c, t in zip(self._current, target)]
        self.kbd.set_rgb(tuple(round(c) for c in self._current))
        return True

    def _on_gst_message(self, _bus, message):
        if message.type == Gst.MessageType.EOS:
            self.quit("GStreamer: end of stream")
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self.quit(f"GStreamer error: {err} | {debug}")

    # ---------------------------------------------------------------- lifecycle

    def quit(self, reason=None):
        if reason:
            log(f"{reason}. Exiting...")
        if self.loop.is_running():
            self.loop.quit()
        return GLib.SOURCE_REMOVE

    def shutdown(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.session_path:
            try:
                self.bus.get_object(PORTAL_BUS, self.session_path).Close(dbus_interface=SESSION_IFACE)
            except dbus.DBusException:
                pass
            self.session_path = None

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            add_signal_handler(sig, self.quit)
        self.start()
        self.loop.run()


def main():
    DBusGMainLoop(set_as_default=True)
    Gst.init(None)

    sync_was_active = _systemctl_user("is-active", "--quiet", SYNC_SERVICE) == 0
    if sync_was_active:
        log("Pausing background wallpaper sync...")
        _systemctl_user("stop", SYNC_SERVICE)
    ambience = None
    try:
        ambience = Ambience()
        ambience.run()
    except dbus.DBusException as e:
        log(f"Portal error: {e}")
    finally:
        if ambience:
            ambience.shutdown()
        if sync_was_active:
            log("Restarting background wallpaper sync...")
            _systemctl_user("start", SYNC_SERVICE)


if __name__ == "__main__":
    main()
