#!/usr/bin/env python3
import sys, os, time, subprocess
from collections import Counter
import colorsys

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstApp', '1.0')
    from gi.repository import Gst, GstApp, GLib
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
except ImportError:
    print("Dependencies missing! Ensure python-gobject, python-dbus, and gst-plugin-pipewire are installed.")
    sys.exit(1)

# Disable background wallpaper sync service to prevent conflicts
print("Disabling background wallpaper sync...")
subprocess.run(["systemctl", "--user", "stop", "plasma-backlight-sync"], check=False, stderr=subprocess.DEVNULL)

DBusGMainLoop(set_as_default=True)
Gst.init(None)

bus = dbus.SessionBus()
portal = bus.get_object('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop')
screencast = dbus.Interface(portal, 'org.freedesktop.portal.ScreenCast')
request_iface = 'org.freedesktop.portal.Request'

loop = GLib.MainLoop()

sender_name = bus.get_unique_name()[1:].replace('.', '_')
token_counter = 0

session_path = None
pw_fd = None
node_id = None
pipeline = None

last_color_time = 0

# Color Smoothing Variables
current_rgb = None

def set_keyboard_color(hex_color):
    try:
        subprocess.run(["asusctl", "aura", "effect", "static", "--colour", hex_color], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: pass

def update_smoothed_color(target_r, target_g, target_b):
    global current_rgb
    
    if current_rgb is None:
        current_rgb = [target_r, target_g, target_b]
    else:
        # Calculate difference distance in color
        diff = ((current_rgb[0] - target_r)**2 + (current_rgb[1] - target_g)**2 + (current_rgb[2] - target_b)**2)**0.5
        
        # Dynamically scale alpha (responsiveness rate) based on difference size.
        # Minimal shift (quiet scenes) -> small alpha (extremely smooth & slow transitions, no jitter).
        # Major shift (explosions, camera cuts, color flash) -> high alpha (immediate reaction).
        if diff <= 15:
            alpha = 0.08
        elif diff >= 150:
            alpha = 0.85
        else:
            alpha = 0.08 + (diff - 15) * (0.85 - 0.08) / (150 - 15)
            
        current_rgb[0] = current_rgb[0] * (1 - alpha) + target_r * alpha
        current_rgb[1] = current_rgb[1] * (1 - alpha) + target_g * alpha
        current_rgb[2] = current_rgb[2] * (1 - alpha) + target_b * alpha
        
    r, g, b = int(current_rgb[0]), int(current_rgb[1]), int(current_rgb[2])
    hex_color = f"{r:02x}{g:02x}{b:02x}"
    set_keyboard_color(hex_color)

def on_new_sample(appsink):
    global last_color_time
    now = time.time()
    
    # Cap at ~20 FPS for smoothness without overwhelming asusctl
    if now - last_color_time < 0.05: 
        return Gst.FlowReturn.OK
        
    sample = appsink.emit('pull-sample')
    buf = sample.get_buffer()
    
    success, map_info = buf.map(Gst.MapFlags.READ)
    if success:
        data = map_info.data
        pixels = []
        for i in range(0, len(data), 3):
            pixels.append((data[i], data[i+1], data[i+2]))
            
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
        # We quantize all bright pixels to calculate the area (size) of each color
        AREA_BIN_SIZE = 24
        bin_counts = Counter()
        for r, g, b, h, l, s in bright_pixels:
            qr = (r // AREA_BIN_SIZE) * AREA_BIN_SIZE
            qg = (g // AREA_BIN_SIZE) * AREA_BIN_SIZE
            qb = (b // AREA_BIN_SIZE) * AREA_BIN_SIZE
            bin_counts[(qr, qg, qb)] += 1
            
        # Score each pixel by combining its saturation, brightness, and its color area count
        scored_pixels = []
        for r, g, b, h, l, s in bright_pixels:
            brightness_factor = 1.0 - abs(l - 0.5) * 2.0
            brightness_factor = max(0.0, brightness_factor)
            
            qr = (r // AREA_BIN_SIZE) * AREA_BIN_SIZE
            qg = (g // AREA_BIN_SIZE) * AREA_BIN_SIZE
            qb = (b // AREA_BIN_SIZE) * AREA_BIN_SIZE
            area_factor = (bin_counts[(qr, qg, qb)] / len(bright_pixels)) ** 0.5
            
            score = s * brightness_factor * area_factor
            scored_pixels.append((score, r, g, b))
            
        scored_pixels.sort(key=lambda x: x[0], reverse=True)
        
        # Filter to top scored pixels (vibrancy combined with spatial size)
        top_vibrant = [(r, g, b) for score, r, g, b in scored_pixels if score > 0.005]
        if not top_vibrant:
            vibrant_color = closest_pixel if closest_pixel else (0, 0, 0)
        else:
            CLUSTER_BIN_SIZE = 32
            quantized = [ (r//CLUSTER_BIN_SIZE*CLUSTER_BIN_SIZE, g//CLUSTER_BIN_SIZE*CLUSTER_BIN_SIZE, b//CLUSTER_BIN_SIZE*CLUSTER_BIN_SIZE) for r, g, b in top_vibrant[:100] ]
            most_common = Counter(quantized).most_common(1)[0][0]
            
            bin_r, bin_g, bin_b = most_common
            matching = [p for p in top_vibrant[:100] 
                        if p[0]//CLUSTER_BIN_SIZE*CLUSTER_BIN_SIZE == bin_r and 
                           p[1]//CLUSTER_BIN_SIZE*CLUSTER_BIN_SIZE == bin_g and 
                           p[2]//CLUSTER_BIN_SIZE*CLUSTER_BIN_SIZE == bin_b]
            
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
        
        update_smoothed_color(opt_color[0], opt_color[1], opt_color[2])
        last_color_time = now
            
        buf.unmap(map_info)
    return Gst.FlowReturn.OK

def on_start_response(response, results):
    global node_id, pw_fd, pipeline
    if response != 0:
        print("Failed to start session. User may have canceled the prompt.")
        loop.quit()
        return
        
    streams = results.get('streams', [])
    if not streams:
        print("No streams returned")
        loop.quit()
        return
        
    node_id = int(streams[0][0])
    
    # Open PipeWire Remote
    fd_object = screencast.OpenPipeWireRemote(session_path, {})
    pw_fd = fd_object.take()
    print("PipeWire stream received. Launching ambient lighting...")
    
    # Start GStreamer
    pipeline_str = f"pipewiresrc fd={pw_fd} path={node_id} ! videoconvert ! videoscale ! video/x-raw,width=32,height=32,format=RGB ! appsink name=sink drop=true max-buffers=1"
    pipeline = Gst.parse_launch(pipeline_str)
    
    appsink = pipeline.get_by_name('sink')
    appsink.set_property('emit-signals', True)
    appsink.connect('new-sample', on_new_sample)
    
    pipeline.set_state(Gst.State.PLAYING)

def on_select_response(response, results):
    if response != 0:
        print("Failed to select sources")
        loop.quit()
        return
        
    global token_counter
    token_counter += 1
    handle_token = f"u{token_counter}"
    handle_path = f"/org/freedesktop/portal/desktop/request/{sender_name}/{handle_token}"
    
    bus.add_signal_receiver(on_start_response, signal_name='Response', dbus_interface=request_iface, path=handle_path)
    screencast.Start(session_path, '', {'handle_token': handle_token})

def on_create_response(response, results):
    global session_path, token_counter
    if response != 0:
        print("Failed to create session")
        loop.quit()
        return
        
    session_path = results.get('session_handle')
    print("Session created. Awaiting monitor selection...")
    
    token_counter += 1
    handle_token = f"u{token_counter}"
    handle_path = f"/org/freedesktop/portal/desktop/request/{sender_name}/{handle_token}"
    
    bus.add_signal_receiver(on_select_response, signal_name='Response', dbus_interface=request_iface, path=handle_path)
    screencast.SelectSources(session_path, {'handle_token': handle_token, 'multiple': False, 'types': dbus.UInt32(1)})

import signal

def cleanup_and_exit(signum=None, frame=None):
    if pipeline:
        pipeline.set_state(Gst.State.NULL)
    print("\nExiting. Restarting background wallpaper sync...")
    subprocess.run(["systemctl", "--user", "start", "plasma-backlight-sync"], check=False)
    sys.exit(0)

def start():
    global token_counter
    token_counter += 1
    handle_token = f"u{token_counter}"
    handle_path = f"/org/freedesktop/portal/desktop/request/{sender_name}/{handle_token}"
    
    print("Requesting PipeWire screen cast via XDG Desktop Portal...")
    bus.add_signal_receiver(on_create_response, signal_name='Response', dbus_interface=request_iface, path=handle_path)
    screencast.CreateSession({'handle_token': handle_token, 'session_handle_token': 'ambient_session'})
    
    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)
    signal.signal(signal.SIGHUP, cleanup_and_exit)
    
    try:
        loop.run()
    except Exception as e:
        print(f"Error: {e}")
        cleanup_and_exit()

if __name__ == '__main__':
    start()
