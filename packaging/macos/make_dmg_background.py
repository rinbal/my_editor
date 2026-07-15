#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the professional drag-to-Applications background for the macOS DMG.

Run on a Mac with Pillow installed (tiffutil, used for the Retina variant, ships
with macOS). The committed images are what the build uses; CI does NOT
regenerate them, so this only needs fonts present on the machine you run it on:

    python3 packaging/macos/make_dmg_background.py

Produces, in this directory:
    dmg_background.png    660 x 440, standard resolution
    dmg_background.tiff   HiDPI (1x + 2x) so the window stays crisp on Retina

dmg_settings.py prefers the .tiff and falls back to the .png. The icon centers
below MUST stay in sync with icon_locations in dmg_settings.py so the arrow and
the soft tiles line up with the app and Applications icons Finder paints on top.
"""

import os
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Layout in standard-resolution points; every value is multiplied by SCALE when
# rendering the 2x Retina variant.
W, H = 660, 440
APP_C = (190, 204)      # app icon center        (keep in sync with dmg_settings.py)
APPS_C = (470, 204)     # Applications icon center
# Tile drawn around each icon center: the icon sits in the upper part, leaving
# room in the lower part for the name label Finder paints under the icon.
TILE_HW = 88            # tile half width
TILE_TOP = 78           # tile extends this far above the icon center
TILE_BOT = 102          # ... and this far below (label lands in here)
TILE_R = 28             # tile corner radius

INK = (29, 29, 31)
GRAY = (122, 122, 130)
FAINT = (228, 228, 233)
BLUE = (30, 136, 229)   # matches the blue text-line in the app icon
# Brand accent bars, echoing the colored lines in the app icon.
BRAND = [
    (229, 57, 53),      # red
    (251, 140, 0),      # orange
    (67, 160, 71),      # green
    (30, 136, 229),     # blue
    (142, 36, 170),     # purple
]


def _font(size, bold=False):
    candidates = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"]
    )
    candidates.append("/System/Library/Fonts/Helvetica.ttc")
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _vertical_gradient(size, top, bottom):
    w, h = size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        base.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return base.resize((w, h))


def _centered(draw, cx, y, text, font, fill):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (right - left) / 2, y - (bottom - top) / 2 - top), text, font=font, fill=fill)


def render(scale):
    s = scale

    def px(v):
        return round(v * s)

    img = _vertical_gradient((px(W), px(H)), (251, 251, 253), (240, 240, 244)).convert("RGBA")

    def tile_box(cx, cy, dy=0):
        return [px(cx - TILE_HW), px(cy - TILE_TOP) + dy, px(cx + TILE_HW), px(cy + TILE_BOT) + dy]

    # Soft drop shadow under both icon tiles, blurred on its own layer.
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for cx, cy in (APP_C, APPS_C):
        sd.rounded_rectangle(tile_box(cx, cy, dy=px(6)), radius=px(TILE_R), fill=(0, 0, 0, 45))
    shadow = shadow.filter(ImageFilter.GaussianBlur(px(9)))
    img = Image.alpha_composite(img, shadow)

    draw = ImageDraw.Draw(img)

    # White rounded tiles that hold the app and Applications icons.
    for cx, cy in (APP_C, APPS_C):
        draw.rounded_rectangle(
            tile_box(cx, cy),
            radius=px(TILE_R),
            fill=(255, 255, 255, 255),
            outline=(233, 233, 238, 255),
            width=max(1, px(1)),
        )

    # Arrow from the app tile to the Applications tile.
    y = px(APP_C[1])
    x0, x1 = px(292), px(372)
    draw.line([(x0, y), (x1 - px(10), y)], fill=BLUE, width=px(9))
    draw.polygon(
        [(x1, y), (x1 - px(20), y - px(15)), (x1 - px(20), y + px(15))],
        fill=BLUE,
    )

    # Header.
    _centered(draw, px(W / 2), px(48), "Install MyEditor", _font(px(31), bold=True), INK)

    # Brand accent: short rounded color bars, echoing the app icon.
    seg_w, seg_h, gap = px(19), px(5), px(7)
    total = len(BRAND) * seg_w + (len(BRAND) - 1) * gap
    bx = px(W / 2) - total / 2
    by = px(80)
    for color in BRAND:
        draw.rounded_rectangle([bx, by, bx + seg_w, by + seg_h], radius=px(2.5), fill=color)
        bx += seg_w + gap

    _centered(draw, px(W / 2), px(106), "Drag the app onto the Applications folder", _font(px(15)), GRAY)

    # Gatekeeper guidance: the app is unsigned, so make the one-time first-open
    # step impossible to miss right here on the install window.
    draw.line([(px(48), px(366)), (px(W - 48), px(366))], fill=FAINT, width=max(1, px(1)))
    tip = _font(px(13))
    _centered(draw, px(W / 2), px(388), "First time you open it: right-click MyEditor in", tip, GRAY)
    _centered(draw, px(W / 2), px(407), "Applications, then choose Open.", tip, GRAY)

    return img.convert("RGB")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    png_path = os.path.join(here, "dmg_background.png")
    tiff_path = os.path.join(here, "dmg_background.tiff")

    render(1).save(png_path)
    print("wrote", png_path)

    # Combine 1x + 2x into a single HiDPI TIFF so Finder renders it crisply on
    # Retina displays. tiffutil ships with macOS; on other systems the build
    # falls back to the standard-resolution PNG.
    with tempfile.TemporaryDirectory() as tmp:
        one = os.path.join(tmp, "1x.png")
        two = os.path.join(tmp, "2x.png")
        render(1).save(one)
        render(2).save(two)
        try:
            subprocess.run(
                ["tiffutil", "-cathidpicheck", one, two, "-out", tiff_path],
                check=True,
                capture_output=True,
            )
            print("wrote", tiff_path)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print("note: tiffutil unavailable, skipped Retina TIFF (%s)" % type(exc).__name__)


if __name__ == "__main__":
    main()
