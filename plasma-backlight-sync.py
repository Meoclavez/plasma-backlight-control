#!/usr/bin/env python3
import sys
import colorsys
import subprocess
import time
import os
import configparser
from collections import Counter
from PIL import Image

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

def resolve_plasma_wallpaper_path(path):
    if not path:
        return None
    if path.startswith("file://"):
        path = path[7:]
    
    if os.path.isdir(path):
        # Plasma wallpaper themes usually have contents/images
        images_dir = os.path.join(path, "contents", "images")
        if os.path.isdir(images_dir):
            images = [f for f in os.listdir(images_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            if images:
                return os.path.join(images_dir, images[0])
        # Or maybe it's just a folder of images for slideshow
        images = [f for f in os.listdir(path) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        if images:
            return os.path.join(path, images[0])
    
    return path if os.path.isfile(path) else None

def get_current_wallpaper():
    """Get the current wallpaper path. Try dbus first to support slideshows, fallback to config."""
    try:
        # Avoid python dbus properties deadlock by using a separate qdbus process
        output = subprocess.check_output(['qdbus6', 'org.kde.plasmashell', '/PlasmaShell', 'org.kde.PlasmaShell.wallpaper', '0'], text=True)
        for line in output.split('\n'):
            if line.startswith('Image:'):
                val = line.split(':', 1)[1].strip()
                path = resolve_plasma_wallpaper_path(val)
                if path:
                    return path
    except Exception as e:
        print(f"Error querying wallpaper dbus: {e}")

    # Fallback to config file
    config_path = os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc")
    if not os.path.exists(config_path):
        return None
        
    try:
        config = configparser.ConfigParser(strict=False)
        config.read(config_path)
        
        for section in config.sections():
            if "org.kde.image" in section and "Image" in config[section]:
                path = config[section]["Image"]
                return resolve_plasma_wallpaper_path(path)
    except Exception as e:
        print(f"Error querying wallpaper config: {e}")
        
    return None

def get_highlight_color(image_path):
    """Analyze the image: get average color, ensure it exists in the wallpaper, or generate a matching color."""
    try:
        img = Image.open(image_path).convert('RGB')
        # Resize to 64x64 for speed and noise reduction
        img = img.resize((64, 64))
        
        try:
            pixels = list(img.get_flattened_data())
        except AttributeError:
            pixels = list(img.getdata())
            
        def rgb_dist(p1, p2):
            return ((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2 + (p1[2] - p2[2])**2)**0.5

        # 1. Convert all pixels to HSL to perform brightness-aware analysis
        pixels_hls = []
        for r, g, b in pixels:
            h, l, s = colorsys.rgb_to_hls(r/255.0, g/255.0, b/255.0)
            pixels_hls.append((r, g, b, h, l, s))

        # Calculate overall screen brightness (average lightness of all pixels)
        screen_brightness = sum(p[4] for p in pixels_hls) / len(pixels_hls)

        # 2. Filter out extremely dark/black pixels to avoid camera noise & compression artifacts
        # dominating the average (e.g. preventing subtle dark red/blue tones from taking over)
        bright_pixels = [p for p in pixels_hls if p[4] >= 0.08]
        if not bright_pixels:
            bright_pixels = pixels_hls  # Fallback if the whole screen is pitch black

        # 3. Calculate a weighted average of the bright parts of the screen
        # We weight each pixel by its lightness and square of saturation to emphasize highly vibrant elements
        has_saturation = any(p[5] >= 0.15 for p in bright_pixels)
        
        total_weight = 0
        weighted_r = 0.0
        weighted_g = 0.0
        weighted_b = 0.0
        
        for r, g, b, h, l, s in bright_pixels:
            if has_saturation:
                weight = l * (s ** 2.0)  # Emphasize highly saturated colors (e.g. pink nebulas)
            else:
                weight = l
            weighted_r += r * weight
            weighted_g += g * weight
            weighted_b += b * weight
            total_weight += weight
            
        if total_weight > 0:
            avg_r = int(weighted_r / total_weight)
            avg_g = int(weighted_g / total_weight)
            avg_b = int(weighted_b / total_weight)
        else:
            avg_r, avg_g, avg_b = 0, 0, 0
        avg_color = (avg_r, avg_g, avg_b)
        
        # 4. Check if the average color is in the bright screen area (min distance to actual pixels)
        closest_pixel = None
        min_dist = float('inf')
        for p in bright_pixels:
            d = rgb_dist(avg_color, p[0:3])
            if d < min_dist:
                min_dist = d
                closest_pixel = p[0:3]
                
        # 5. Generate the matching vibrant/dominant color from the bright parts of the screen
        scored_pixels = []
        for r, g, b, h, l, s in bright_pixels:
            # Score highly for high saturation and moderate brightness
            brightness_factor = 1.0 - abs(l - 0.5) * 2.0
            brightness_factor = max(0.0, brightness_factor)
            score = s * brightness_factor
            scored_pixels.append((score, r, g, b))
            
        scored_pixels.sort(key=lambda x: x[0], reverse=True)
        
        # Filter to top vibrant pixels
        top_vibrant = [(r, g, b) for score, r, g, b in scored_pixels if score > 0.15]
        if not top_vibrant:
            vibrant_color = closest_pixel if closest_pixel else (0, 0, 0)
        else:
            BIN_SIZE = 32
            quantized = [ (r//BIN_SIZE*BIN_SIZE, g//BIN_SIZE*BIN_SIZE, b//BIN_SIZE*BIN_SIZE) for r, g, b in top_vibrant[:100] ]
            most_common = Counter(quantized).most_common(1)[0][0]
            
            bin_r, bin_g, bin_b = most_common
            matching = [p for p in top_vibrant[:100] 
                        if p[0]//BIN_SIZE*BIN_SIZE == bin_r and 
                           p[1]//BIN_SIZE*BIN_SIZE == bin_g and 
                           p[2]//BIN_SIZE*BIN_SIZE == bin_b]
            
            if matching:
                avg_v_r = sum(p[0] for p in matching) // len(matching)
                avg_v_g = sum(p[1] for p in matching) // len(matching)
                avg_v_b = sum(p[2] for p in matching) // len(matching)
                vibrant_color = (avg_v_r, avg_v_g, avg_v_b)
            else:
                vibrant_color = top_vibrant[0]
                
        # 6. Smooth Blend Guard between Weighted Average and Vibrant Color:
        # Instead of a hard switch, we calculate a continuous blend factor based on the saturation (avg_s)
        # of the average color and its proximity to the real pixels (min_dist).
        avg_h, avg_l, avg_s = colorsys.rgb_to_hls(avg_r/255.0, avg_g/255.0, avg_b/255.0)
        
        # Proximity confidence factor (snaps to vibrant if average color does not exist in screen)
        if min_dist > 35.0:
            proximity_factor = max(0.0, 1.0 - (min_dist - 35.0) / 35.0)
        else:
            proximity_factor = 1.0
            
        # Overall confidence in using the average color
        average_confidence = avg_s * proximity_factor
        
        # Smoothly interpolate blend based on confidence
        if average_confidence <= 0.05:
            blend = 0.0
        elif average_confidence >= 0.18:
            blend = 1.0
        else:
            blend = (average_confidence - 0.05) / (0.18 - 0.05)
            
        final_color = (
            int(vibrant_color[0] * (1.0 - blend) + avg_color[0] * blend),
            int(vibrant_color[1] * (1.0 - blend) + avg_color[1] * blend),
            int(vibrant_color[2] * (1.0 - blend) + avg_color[2] * blend)
        )
        
        # 7. Optimize color for keyboard backlight:
        # Scale the overall target brightness (lightness) based on screen_brightness to prevent
        # the keyboard from staying bright in dark scenes.
        r_f, g_f, b_f = final_color[0]/255.0, final_color[1]/255.0, final_color[2]/255.0
        h, l, s = colorsys.rgb_to_hls(r_f, g_f, b_f)
        
        original_s = s
        
        # Dynamically scale the minimum/target lightness cap
        # Pitch black -> min lightness 0.05. Bright screen (>=0.30) -> min lightness 0.45.
        min_l = max(0.05, 0.45 * min(1.0, screen_brightness / 0.30))
        
        if l < min_l:
            l = min_l
            
        # Ensure minimum saturation of 0.25 (unless the image is highly black/white/grayscale)
        if s < 0.25 and original_s > 0.05:
            s = 0.25
            
        r_opt, g_opt, b_opt = colorsys.hls_to_rgb(h, l, s)
        opt_color = (int(r_opt*255), int(g_opt*255), int(b_opt*255))
        
        return f"{opt_color[0]:02x}{opt_color[1]:02x}{opt_color[2]:02x}"
            
    except Exception as e:
        print(f"Error processing image {image_path}: {e}")
        
    return "ffffff" # Default to white on error

def set_keyboard_color(hex_color):
    """Set the keyboard backlight color using asusctl."""
    try:
        print(f"Setting keyboard color to {hex_color}...")
        subprocess.run(["asusctl", "aura", "effect", "static", "--colour", hex_color], check=False)
    except Exception as e:
        print(f"Error setting keyboard color: {e}")

def main():
    print("Starting Plasma Backlight Sync Daemon...")
    DBusGMainLoop(set_as_default=True)
    
    last_wallpaper = [None]
    
    def update_wallpaper(*args, **kwargs):
        current_wallpaper = get_current_wallpaper()
        if current_wallpaper and current_wallpaper != last_wallpaper[0]:
            print(f"Wallpaper changed: {current_wallpaper}")
            color = get_highlight_color(current_wallpaper)
            set_keyboard_color(color)
            last_wallpaper[0] = current_wallpaper

    try:
        bus = dbus.SessionBus()
        
        # Initial check
        update_wallpaper()
        
        # Listen for the wallpaperChanged signal on PlasmaShell
        bus.add_signal_receiver(
            update_wallpaper,
            dbus_interface="org.kde.PlasmaShell",
            signal_name="wallpaperChanged"
        )
        
        # Also listen to generic properties changed just in case wallpaper is treated as a property
        bus.add_signal_receiver(
            update_wallpaper,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged"
        )
        
        print("Listening for wallpaper changes via D-Bus...")
        loop = GLib.MainLoop()
        loop.run()
    except Exception as e:
        print(f"Failed to setup DBus listener: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
