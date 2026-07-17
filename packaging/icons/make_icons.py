#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the app icon set from a single drawn master image.

Run from the repo root with the project venv:

    .venv/bin/python packaging/icons/make_icons.py

Outputs (into this directory):
    icon.png       1024x1024 master
    icon-256.png   256x256   (Linux launcher + in-app window icon)
    icon.ico       multi-size Windows icon (16..256)
    icon.icns      macOS icon (built via iconutil when available)

This is a placeholder design: the editor's signature feature is multi-color
text, so the icon shows colored "lines of text" on a dark editor tile. Replace
the draw_master() body (or drop in your own icon.png) and re-run to rebrand.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

# Accent palette mirrors constants.TEXT_COLORS (the app's text colors).
BG_TOP = (45, 45, 48)       # #2D2D30
BG_BOTTOM = (30, 30, 30)    # #1E1E1E
LINES = [
    ((229, 57, 53), 0.62),    # red
    ((251, 140, 0), 0.78),    # orange
    ((67, 160, 71), 0.50),    # green
    ((30, 136, 229), 0.70),   # blue
    ((142, 36, 170), 0.42),   # purple
    ((212, 212, 212), 0.66),  # light gray (plain text)
]
CARET = (249, 168, 37)      # golden yellow caret


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def draw_master(px=1024, ss=4):
    """Draw the master icon at `px`, supersampled by `ss` for clean edges."""
    s = px * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Vertical gradient background.
    for y in range(s):
        t = y / (s - 1)
        r = round(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = round(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = round(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (s, y)], fill=(r, g, b, 255))

    # Clip to a rounded tile.
    img.putalpha(_rounded_mask((s, s), radius=int(s * 0.225)))

    # Colored text lines.
    draw = ImageDraw.Draw(img)
    left = int(s * 0.20)
    line_h = int(s * 0.072)
    gap = int(s * 0.062)
    total = len(LINES) * line_h + (len(LINES) - 1) * gap
    top = (s - total) // 2
    max_w = int(s * 0.60)
    y = top
    for color, frac in LINES:
        w = int(max_w * frac)
        draw.rounded_rectangle(
            [left, y, left + w, y + line_h],
            radius=line_h // 2,
            fill=color + (255,),
        )
        y += line_h + gap

    # Blinking caret at the end of the last line.
    last_w = int(max_w * LINES[-1][1])
    caret_x = left + last_w + int(s * 0.03)
    caret_top = top + (len(LINES) - 1) * (line_h + gap) - int(line_h * 0.25)
    draw.rounded_rectangle(
        [caret_x, caret_top, caret_x + int(s * 0.018), caret_top + int(line_h * 1.5)],
        radius=int(s * 0.009),
        fill=CARET + (255,),
    )

    return img.resize((px, px), Image.LANCZOS)


def build_icns(master, out):
    """Build a .icns via macOS iconutil; skip gracefully off macOS."""
    iconutil = shutil.which("iconutil")
    if not iconutil:
        print("  iconutil not found (not macOS); skipping icon.icns")
        return
    iconset = HERE / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    specs = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    for size, name in specs:
        master.resize((size, size), Image.LANCZOS).save(iconset / name)
    subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(iconset)
    print(f"  wrote {out.name}")


def main():
    master = draw_master(1024)
    master.save(HERE / "icon.png")
    print("  wrote icon.png (1024)")

    master.resize((256, 256), Image.LANCZOS).save(HERE / "icon-256.png")
    print("  wrote icon-256.png")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(HERE / "icon.ico", sizes=ico_sizes)
    print("  wrote icon.ico")

    build_icns(master, HERE / "icon.icns")
    print("done.")


if __name__ == "__main__":
    sys.exit(main())
