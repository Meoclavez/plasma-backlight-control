#!/usr/bin/env python3
"""Keep the ASUS keyboard backlight in sync with the KDE Plasma wallpaper.

Usage:
  plasma-backlight-sync.py                 run the daemon
  plasma-backlight-sync.py --analyze IMG…  print the colours chosen for images
"""
import configparser
import json
import math
import os
import signal
import sys
from urllib.parse import unquote

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from backlight_core import (  # noqa: E402
    AuraKeyboard, ColorCache, add_signal_handler, analyze_files, analyze_image, led_rgb, load_config,
    load_thumbnail, log, oklab_to_rgb, release_memory, to_hex,
)

POLL_SECONDS = 2          # Plasma 6 does not signal slideshow rotations
DEBOUNCE_MS = 300         # coalesce bursts of changes (e.g. slideshow start-up)
FADE_STEP_MS = 33
FILL_PRESERVE_ASPECT_CROP = 2
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".jxl", ".gif")
APPLETSRC = os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc")
KDEGLOBALS = os.path.expanduser("~/.config/kdeglobals")


def _is_image(name):
    return name.lower().endswith(IMAGE_EXTS)


def _color_scheme_is_dark():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(KDEGLOBALS)
        r, g, b = (int(v) for v in parser["Colors:Window"]["BackgroundNormal"].split(",")[:3])
    except (KeyError, ValueError, configparser.Error):
        return False
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5


def _best_package_image(images_dir, screen):
    """Pick the image whose WxH file name is closest to the screen resolution."""
    try:
        names = sorted(n for n in os.listdir(images_dir) if _is_image(n))
    except OSError:
        return None
    if not names:
        return None

    def distance(name):
        try:
            w, h = (int(v) for v in os.path.splitext(name)[0].split("x"))
            # aspect ratio first (portrait phone variants exist), then pixel count
            return (round(abs(math.log((w / h) / (screen[0] / screen[1]))), 2),
                    abs(math.log((w * h) / (screen[0] * screen[1]))))
        except (ValueError, ZeroDivisionError):
            return (float("inf"), 0.0)

    return os.path.join(images_dir, min(names, key=distance))


def resolve_wallpaper_path(url, screen):
    if not url:
        return None
    path = url[7:] if url.startswith("file://") else url
    if not os.path.exists(path):
        path = unquote(path)
    if os.path.isdir(path):
        # Plasma wallpaper package: contents/images[_dark]/<W>x<H>.<ext>
        variants = ["images_dark", "images"] if _color_scheme_is_dark() else ["images"]
        for variant in variants:
            found = _best_package_image(os.path.join(path, "contents", variant), screen)
            if found:
                return found
        try:
            images = sorted(n for n in os.listdir(path) if _is_image(n))
        except OSError:
            images = []
        return os.path.join(path, images[0]) if images else None
    return path if os.path.isfile(path) else None


def _wallpaper_from_config():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(APPLETSRC)
    except configparser.Error as e:
        log(f"Error reading {APPLETSRC}: {e}")
        return None
    for section in parser.sections():
        if ("org.kde.image" in section or "org.kde.slideshow" in section) and "Image" in parser[section]:
            return parser[section]["Image"]
    return None


class WallpaperSync:
    def __init__(self):
        self.cfg = load_config()
        self.kbd = AuraKeyboard()
        self.cache = ColorCache()
        self.session = dbus.SessionBus()
        self._shell = None
        self._screen = None
        self.current = None       # (path, crop_aspect) last applied
        self.analysis = None      # (lab, scene_l) of the current wallpaper
        self._pending = None      # GLib source id of the debounced apply
        self._fade = None         # GLib source id of the running fade

    # ---------------------------------------------------------------- plasma queries

    def _plasmashell(self):
        if self._shell is None:
            obj = self.session.get_object("org.kde.plasmashell", "/PlasmaShell")
            self._shell = dbus.Interface(obj, "org.kde.PlasmaShell")
        return self._shell

    def _screen_size(self):
        if self._screen is None:
            try:
                geo = json.loads(self._plasmashell().evaluateScript(
                    "print(JSON.stringify(screenGeometry(0)))"))
                self._screen = (int(geo["width"]), int(geo["height"]))
            except (dbus.DBusException, ValueError, KeyError, TypeError):
                return (1920, 1080)  # retried on the next query
        return self._screen

    def query_wallpaper(self):
        """Return (image path, crop aspect or None) for screen 0."""
        url, fill_mode = None, FILL_PRESERVE_ASPECT_CROP
        try:
            props = self._plasmashell().wallpaper(dbus.UInt32(0))
            url = str(props.get("Image", "")) or None
            fill_mode = int(props.get("FillMode", fill_mode))
        except dbus.DBusException:
            self._shell = None  # plasmashell restarted or not up yet
            self._screen = None
            url = _wallpaper_from_config()
        screen = self._screen_size()
        path = resolve_wallpaper_path(url, screen)
        if not path:
            return None
        aspect = screen[0] / screen[1] if fill_mode == FILL_PRESERVE_ASPECT_CROP else None
        return path, aspect

    # ---------------------------------------------------------------- update flow

    def check(self, *_args):
        wallpaper = self.query_wallpaper()
        if wallpaper and wallpaper != self.current:
            if self._pending:
                GLib.source_remove(self._pending)
            self._pending = GLib.timeout_add(DEBOUNCE_MS, self._apply_pending, wallpaper)
        return True

    def _apply_pending(self, wallpaper):
        self._pending = None
        self.apply(wallpaper)
        return False

    def analyze(self, path, aspect):
        try:
            key = ColorCache.key(path, aspect)
        except OSError as e:
            log(f"Cannot read {path}: {e}")
            return None
        cached = self.cache.get(key)
        if cached:
            return cached
        try:
            lab, scene_l = analyze_image(load_thumbnail(path, aspect))
        except Exception as e:  # any decoder error must not kill the daemon
            log(f"Error processing image {path}: {e}")
            return None
        finally:
            release_memory()
        self.cache.put(key, lab, scene_l)
        return lab, scene_l

    def apply(self, wallpaper):
        path, aspect = wallpaper
        analysis = self.analyze(path, aspect)
        self.current = wallpaper
        if analysis is None:
            return
        self.analysis = analysis
        lab, scene_l = analysis
        rgb = led_rgb(lab, scene_l, self.cfg)
        log(f"Wallpaper changed: {path} -> accent #{to_hex(oklab_to_rgb(lab))}, led #{to_hex(rgb)}")
        self.fade_to(rgb)

    def fade_to(self, target):
        if self._fade:
            GLib.source_remove(self._fade)
            self._fade = None
        start = self.kbd.last_rgb or self.kbd.read_rgb()
        steps = self.cfg.transition_ms // FADE_STEP_MS
        if start is None or steps <= 1:
            self.kbd.set_rgb(target)
            return
        state = {"i": 0}

        def step():
            state["i"] += 1
            t = state["i"] / steps
            t = t * t * (3 - 2 * t)  # ease in-out
            self.kbd.set_rgb(tuple(round(s + (e - s) * t) for s, e in zip(start, target)))
            if state["i"] >= steps:
                self._fade = None
                return False
            return True

        self._fade = GLib.timeout_add(FADE_STEP_MS, step)

    def reload(self):
        self.cfg = load_config()
        log(f"Config reloaded: {self.cfg}")
        if self.analysis:
            self.fade_to(led_rgb(*self.analysis, self.cfg))
        return True

    # ---------------------------------------------------------------- main loop

    def run(self):
        self.session.add_signal_receiver(
            self.check, dbus_interface="org.kde.PlasmaShell", signal_name="wallpaperChanged")
        GLib.timeout_add_seconds(POLL_SECONDS, self.check)
        add_signal_handler(signal.SIGHUP, self.reload)
        loop = GLib.MainLoop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            add_signal_handler(sig, loop.quit)

        wallpaper = self.query_wallpaper()
        if wallpaper:
            self.apply(wallpaper)
        log("Listening for wallpaper changes via D-Bus & slideshow timer...")
        loop.run()


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--analyze" and len(sys.argv) > 2:
            analyze_files(sys.argv[2:])
            return
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)

    log("Starting Plasma Backlight Sync Daemon...")
    DBusGMainLoop(set_as_default=True)
    try:
        WallpaperSync().run()
    except dbus.DBusException as e:
        log(f"Failed to set up D-Bus: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
