#!/usr/bin/env bash
# Install the daemons into ~/.local/bin and (re)start the user service.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

bin="$HOME/.local/bin"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$bin" "$unit_dir"

install -m 644 backlight_core.py "$bin/"
install -m 755 plasma-backlight-sync.py plasma-video-ambience.py "$bin/"
install -m 644 plasma-backlight-sync.service "$unit_dir/"

if [ ! -w /sys/class/leds/asus::kbd_backlight/kbd_rgb_mode ]; then
    echo "Note: direct keyboard writes disabled (asusd only). To enable, see udev/90-asus-kbd-rgb.rules in README.md"
fi

systemctl --user daemon-reload
systemctl --user enable plasma-backlight-sync.service
systemctl --user restart plasma-backlight-sync.service
systemctl --user --no-pager status plasma-backlight-sync.service | head -n 5
