# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Syntax highlighting starts off.

The setting is not persisted, so the default is what the user meets on
every launch, not just the first. It lives in three places: the flag the
editor reads, the menu item's check state, and the header checkbox. They
have to agree, because two of them are what the user is looking at and
the third is what is actually happening.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = pathlib.Path(__file__).resolve().parent.parent


def assignments(path, target):
    """Every literal assigned to ``target`` in the file, in order."""
    tree = ast.parse((ROOT / path).read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for lhs in node.targets:
            if (
                isinstance(lhs, ast.Attribute)
                and lhs.attr == target
                and isinstance(node.value, ast.Constant)
            ):
                found.append(node.value.value)
    return found


def calls_with_bool(path, attr, method):
    """Booleans passed to ``self.<attr>.<method>(...)``, in order."""
    tree = ast.parse((ROOT / path).read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == method
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == attr
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, bool)
        ):
            found.append(node.args[0].value)
    return found


def test_the_editor_flag_starts_off():
    values = assignments("main_window.py", "syntax_highlighting")
    assert values, "syntax_highlighting is never assigned a literal"
    assert values[0] is False


def test_the_header_checkbox_starts_unchecked():
    values = calls_with_bool("widgets.py", "syntax_highlight_checkbox", "setChecked")
    assert values, "the checkbox never gets a literal state"
    assert values[0] is False


def test_the_menu_item_starts_unchecked():
    values = calls_with_bool("main_window.py", "act_toggle_syntax_hl", "setChecked")
    assert values, "the menu action never gets a literal state"
    assert values[0] is False


def test_it_matches_line_numbers_which_were_already_off():
    # The two live side by side and are the same kind of decision.
    assert assignments("main_window.py", "show_line_numbers")[0] is False


def test_the_toggle_still_exists_so_this_is_a_default_not_a_removal():
    source = (ROOT / "main_window.py").read_text()
    assert "_toggle_syntax_highlighting" in source
    assert "Ctrl+Shift+H" in source
