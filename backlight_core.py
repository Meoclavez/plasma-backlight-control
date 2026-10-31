"""Shared colour analysis and ASUS Aura output for the Plasma backlight daemons.

Pipeline: image -> (visible crop) -> small thumbnail -> median-cut palette ->
OKLab scoring (chroma x area x usable lightness) -> hue-family average ->
LED mapping (saturation boost, channel gamma, scene-adaptive brightness).
"""
import colorsys
import configparser
import ctypes
import gc
import json
import math
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields

from PIL import Image, ImageOps

import dbus

ALGO_VERSION = 2
ANALYSIS_SIZE = 96       # long side of the thumbnail that gets analysed
PALETTE_SIZE = 20        # median-cut clusters
NEUTRAL_CHROMA = 0.025   # OKLab chroma below which an image counts as greyscale
HUE_WINDOW = math.radians(30)

CONFIG_PATH = os.path.expanduser("~/.config/plasma-backlight-sync.conf")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                         "plasma-backlight-sync")

ASUSD_BUS = "xyz.ljones.Asusd"
AURA_ROOT = "/xyz/ljones/aura"
AURA_IFACE = "xyz.ljones.Aura"


def log(msg):
    print(msg, flush=True)


def add_signal_handler(signum, handler):
    """Run handler on the GLib main loop when signum arrives.

    PyGObject >= 3.52 moved this to GLibUnix and deprecated GLib.unix_signal_add.
    """
    import gi
    from gi.repository import GLib
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        return GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signum, handler)
    except (ImportError, ValueError):
        return GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, handler)


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    gamma: float = 1.8              # LED channel gamma; 1.0 sends raw sRGB values
    saturation: float = 1.15        # HSV saturation multiplier before gamma
    min_brightness: float = 0.15    # LED level for pitch-black images/scenes
    max_brightness: float = 1.0     # LED level for bright images/scenes
    transition_ms: int = 800        # wallpaper cross-fade duration (0 = instant)


_LIMITS = {
    "gamma": (0.5, 3.5),
    "saturation": (0.0, 3.0),
    "min_brightness": (0.0, 1.0),
    "max_brightness": (0.0, 1.0),
    "transition_ms": (0, 10000),
}


def load_config(path=CONFIG_PATH):
    """Read the optional [backlight] section; missing or invalid keys keep defaults."""
    cfg = Config()
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as e:
        log(f"Ignoring unreadable config {path}: {e}")
        return cfg
    if not parser.has_section("backlight"):
        return cfg
    section = parser["backlight"]
    for f in fields(Config):
        if f.name not in section:
            continue
        try:
            value = f.type(float(section[f.name]))
        except ValueError:
            log(f"Config: invalid {f.name}={section[f.name]!r}, keeping {getattr(cfg, f.name)}")
            continue
        lo, hi = _LIMITS[f.name]
        setattr(cfg, f.name, min(max(value, lo), hi))
    if cfg.min_brightness > cfg.max_brightness:
        cfg.min_brightness = cfg.max_brightness
    return cfg


# --------------------------------------------------------------------------- colour maths

def _srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def rgb_to_oklab(rgb):
    r, g, b = (_srgb_to_linear(v / 255.0) for v in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _oklab_to_linear(L, a, b):
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def _in_gamut(lin):
    return all(-1e-6 <= v <= 1 + 1e-6 for v in lin)


def oklab_to_rgb(lab):
    """OKLab -> sRGB 0-255, keeping lightness and hue and reducing chroma to fit the gamut."""
    L = min(max(lab[0], 0.0), 1.0)
    a, b = lab[1], lab[2]
    lin = _oklab_to_linear(L, a, b)
    if not _in_gamut(lin):
        lo, hi = 0.0, 1.0
        for _ in range(24):
            mid = (lo + hi) / 2
            if _in_gamut(_oklab_to_linear(L, a * mid, b * mid)):
                lo = mid
            else:
                hi = mid
        lin = _oklab_to_linear(L, a * lo, b * lo)
    return tuple(round(_linear_to_srgb(min(max(v, 0.0), 1.0)) * 255) for v in lin)


def _smoothstep(e0, e1, x):
    t = min(max((x - e0) / (e1 - e0), 0.0), 1.0)
    return t * t * (3 - 2 * t)


def _hue_distance(h1, h2):
    d = abs(h1 - h2) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def to_hex(rgb):
    return "{:02x}{:02x}{:02x}".format(*rgb)


# --------------------------------------------------------------------------- analysis

def _center_box(w, h, aspect):
    if not aspect:
        return (0, 0, w, h)
    if w / h > aspect:
        nw = max(1, round(h * aspect))
        x = (w - nw) // 2
        return (x, 0, x + nw, h)
    nh = max(1, round(w / aspect))
    y = (h - nh) // 2
    return (0, y, w, y + nh)


def load_thumbnail(path, crop_aspect=None):
    """Decode only as much of the image as needed and return a small RGB thumbnail.

    crop_aspect selects the centred region that is visible on screen
    (Plasma's "Scaled and Cropped" fill mode).
    """
    with Image.open(path) as im:
        # JPEG: decode directly at 1/2..1/8 scale instead of full resolution
        im.draft("RGB", (ANALYSIS_SIZE * 4, ANALYSIS_SIZE * 4))
        ImageOps.exif_transpose(im, in_place=True)
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("LA", "PA") else "RGB")
        box = _center_box(*im.size, crop_aspect)
        bw, bh = box[2] - box[0], box[3] - box[1]
        scale = ANALYSIS_SIZE / max(bw, bh)
        size = (max(1, round(bw * scale)), max(1, round(bh * scale)))
        # resizing straight from the source box avoids full-resolution crop copies
        thumb = im.resize(size, Image.Resampling.BOX, box=box, reducing_gap=3.0)
    return thumb if thumb.mode == "RGB" else thumb.convert("RGB")


def analyze_image(img):
    """Return ((L, a, b) accent colour in OKLab, area-weighted scene lightness)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    q = img.quantize(colors=PALETTE_SIZE, method=Image.Quantize.MEDIANCUT)
    palette = q.getpalette()
    counts = q.getcolors(256)
    total = float(sum(c for c, _ in counts))

    clusters = []  # (area, L, a, b, chroma, hue)
    for count, idx in counts:
        L, a, b = rgb_to_oklab(palette[idx * 3: idx * 3 + 3])
        clusters.append((count / total, L, a, b, math.hypot(a, b), math.atan2(b, a)))
    scene_l = sum(c[0] * c[1] for c in clusters)

    def score(c):
        area, L, _, _, chroma, _ = c
        # ignore near-black noise and blown-out highlights
        usable = _smoothstep(0.15, 0.40, L) * (1.0 - _smoothstep(0.90, 0.99, L))
        return (chroma ** 1.5) * (area ** 0.4) * usable

    best = max(clusters, key=score)
    if best[4] < NEUTRAL_CHROMA or score(best) <= 0.0:
        lit = [c for c in clusters if c[1] >= 0.15] or clusters
        w = sum(c[0] for c in lit)
        return tuple(sum(c[0] * c[i] for c in lit) / w for i in (1, 2, 3)), scene_l

    # Average the winning hue family so a subject rendered in several shades
    # gives its representative colour rather than one arbitrary shade.
    family = [(score(c), c) for c in clusters
              if c[4] >= NEUTRAL_CHROMA * 0.75 and _hue_distance(c[5], best[5]) <= HUE_WINDOW]
    wsum = sum(s for s, _ in family)
    lab = tuple(sum(s * c[i] for s, c in family) / wsum for i in (1, 2, 3))
    return lab, scene_l


def led_rgb(lab, scene_l, cfg):
    """Map an analysed accent colour to the RGB triplet sent to the keyboard."""
    r, g, b = oklab_to_rgb(lab)
    h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    s = min(1.0, s * cfg.saturation)
    # Dim with the scene, but let a bright subject on a dark background keep some glow.
    driver = 0.5 * scene_l + 0.5 * lab[0]
    level = cfg.min_brightness + (cfg.max_brightness - cfg.min_brightness) * _smoothstep(0.08, 0.45, driver)
    # Full-value chromaticity, then gamma: LED PWM is linear, sRGB values are not,
    # so secondary channels are pulled down to keep oranges/pastels from washing out.
    return tuple(round(255 * level * (c ** cfg.gamma)) for c in colorsys.hsv_to_rgb(h, s, 1.0))


try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


def release_memory():
    """Hand freed decode buffers back to the OS so the idle daemon stays small."""
    gc.collect()
    if _libc is not None:
        _libc.malloc_trim(0)


class ColorCache:
    """Persistent analysis cache keyed by file identity, crop and algorithm version."""

    MAX_ENTRIES = 10000

    def __init__(self, path=os.path.join(CACHE_DIR, "colors.json")):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(path, crop_aspect):
        st = os.stat(path)
        aspect = f"{crop_aspect:.4f}" if crop_aspect else "full"
        return f"{ALGO_VERSION}|{path}|{st.st_mtime_ns}|{st.st_size}|{aspect}"

    def get(self, key):
        entry = self.data.get(key)
        if entry is None:
            return None
        self.data[key] = self.data.pop(key)  # mark most recently used
        return tuple(entry[0]), entry[1]

    def put(self, key, lab, scene_l):
        self.data[key] = [list(lab), scene_l]
        while len(self.data) > self.MAX_ENTRIES:
            self.data.pop(next(iter(self.data)))
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log(f"Could not write colour cache: {e}")


# --------------------------------------------------------------------------- keyboard output

def _mode_data(rgb):
    r, g, b = rgb
    return dbus.Struct(
        (
            dbus.UInt32(0),   # Mode: Static
            dbus.UInt32(0),   # Speed
            dbus.Struct((dbus.Byte(r), dbus.Byte(g), dbus.Byte(b)), signature="yyy"),
            dbus.Struct((dbus.Byte(0), dbus.Byte(0), dbus.Byte(0)), signature="yyy"),
            dbus.String("Med"),
            dbus.String("Right"),
        ),
        signature="uu(yyy)(yyy)ss",
    )


class AuraKeyboard:
    """Static-colour writer for the asusd Aura D-Bus interface with an asusctl fallback.

    Identical consecutive colours are skipped: asusd rewrites
    /etc/asusd/aura_<device>.ron on every LedModeData change.
    """

    RETRY_SECONDS = 2.0

    def __init__(self):
        self._props = None
        self._retry_at = 0.0
        self.last_rgb = None

    def _connect(self):
        bus = dbus.SystemBus()
        path = f"{AURA_ROOT}/tuf"
        try:
            root = bus.get_object(ASUSD_BUS, AURA_ROOT)
            xml = root.Introspect(dbus_interface="org.freedesktop.DBus.Introspectable")
            nodes = [n.get("name") for n in ET.fromstring(xml).findall("node")]
            if nodes:
                path = f"{AURA_ROOT}/{nodes[0]}"
        except (dbus.DBusException, ET.ParseError):
            pass
        self._props = dbus.Interface(bus.get_object(ASUSD_BUS, path), dbus.PROPERTIES_IFACE)

    def read_rgb(self):
        """Colour currently programmed in asusd, or None."""
        try:
            if self._props is None:
                self._connect()
            data = self._props.Get(AURA_IFACE, "LedModeData")
            return tuple(int(v) for v in data[2])
        except (dbus.DBusException, IndexError, TypeError):
            self._props = None
            return None

    def set_rgb(self, rgb, force=False):
        rgb = tuple(min(max(int(v), 0), 255) for v in rgb)
        if rgb == self.last_rgb and not force:
            return True
        try:
            if self._props is None:
                self._connect()
            self._props.Set(AURA_IFACE, "LedModeData", _mode_data(rgb))
        except dbus.DBusException as e:
            self._props = None
            if not self._fallback(rgb, e):
                return False
        self.last_rgb = rgb
        return True

    def _fallback(self, rgb, err):
        now = time.monotonic()
        if now < self._retry_at:
            return False
        self._retry_at = now + self.RETRY_SECONDS
        log(f"asusd D-Bus write failed ({err.get_dbus_name()}), trying asusctl")
        if subprocess.run(["systemctl", "is-active", "--quiet", "asusd"]).returncode != 0:
            log("asusd is not running; trying to start it (enable it with: sudo systemctl enable --now asusd)")
            subprocess.run(["systemctl", "start", "--no-ask-password", "asusd"], check=False)
        try:
            subprocess.run(["asusctl", "aura", "effect", "static", "-c", to_hex(rgb)],
                           check=True, capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"asusctl fallback failed: {e}")
            return False
        return True


def analyze_files(paths, cfg=None):
    """CLI helper: print accent and LED colours for image files."""
    cfg = cfg or load_config()
    for path in paths:
        try:
            lab, scene_l = analyze_image(load_thumbnail(path))
        except OSError as e:
            print(f"{path}: {e}", file=sys.stderr)
            continue
        print(f"accent #{to_hex(oklab_to_rgb(lab))}  led #{to_hex(led_rgb(lab, scene_l, cfg))}  "
              f"scene L {scene_l:.2f}  {path}")
