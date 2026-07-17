#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

from constants import APP_DISPLAY_NAME, TEXT_COLORS


def welcome_html() -> str:
    """Build the HTML shown in the first-run Welcome tab."""
    colors = " ".join(
        f'<span style="color:{color.name()}">{name}</span>'
        for name, color in TEXT_COLORS.items()
    )
    return f"""
    <h1>Welcome to {APP_DISPLAY_NAME}</h1>
    <p>A minimal, distraction-free text editor - just you and your words.</p>
    <h2>Get started</h2>
    <ul>
        <li><b>Ctrl+N</b> - new file</li>
        <li><b>Ctrl+O</b> - open a file</li>
        <li><b>Ctrl+S</b> - save</li>
        <li><b>Ctrl+F</b> - find</li>
        <li><b>Ctrl+Shift+T</b> - switch between dark and light theme</li>
        <li>Help &gt; Keyboard Shortcuts - for the full list</li>
    </ul>
    <p>Colors, right where you need them: {colors}</p>
    <p>That's it. This tab is just a note like any other - close it, edit it, or make it your own.</p>
    """
