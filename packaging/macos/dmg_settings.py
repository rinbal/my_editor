# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""dmgbuild settings for the MyEditor drag-to-Applications disk image.

Used by packaging/macos/build_dmg.sh:

    dmgbuild -s packaging/macos/dmg_settings.py \
        -D app=dist/MyEditor.app \
        -D background=packaging/macos/dmg_background.tiff \
        "Install MyEditor" dist/my-editor-<ver>-macos-<arch>.dmg

dmgbuild is pure Python: it writes the Finder window layout (background, icon
positions, the Applications symlink) directly into the image's .DS_Store, so it
styles the window WITHOUT driving Finder/AppleScript and runs on headless CI
runners. That is why the old plain-hdiutil script could not style the window and
this one can.

The icon_locations below MUST match the tile centers in make_dmg_background.py
so the app and Applications icons land on the drawn tiles with the arrow
between them.
"""

import os.path

# -- Values passed in with -D on the command line -------------------------
application = defines["app"]                 # e.g. dist/MyEditor.app
appname = os.path.basename(application)       # e.g. MyEditor.app
background = defines["background"]            # the .tiff (or .png fallback)

# -- Disk image -----------------------------------------------------------
format = "UDZO"                               # compressed, read-only
files = [application]
symlinks = {"Applications": "/Applications"}

# -- Finder window --------------------------------------------------------
default_view = "icon-view"
window_rect = ((360, 180), (660, 440))        # ((x, y), (width, height))
icon_size = 128
text_size = 13

icon_locations = {
    appname: (190, 204),
    "Applications": (470, 204),
}

# A clean window: no toolbar, sidebar, path bar or status bar, so only the
# styled background and the two icons show.
show_toolbar = False
show_pathbar = False
show_sidebar = False
show_status_bar = False
show_tab_view = False
show_icon_preview = False
