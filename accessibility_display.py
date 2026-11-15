# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the person asked of every app's display: less motion, more contrast.

macOS has Reduce Motion and Increase Contrast (System Settings >
Accessibility > Display), Windows has "Animation effects" and contrast
themes. What moves here to show a change (a photo group laying itself out
again while a picture is dragged, a picture flashing after Undo) asks
reduce_motion and then changes at once; what is drawn translucent over a
picture asks increase_contrast and is drawn solid.

The answers are read from the system and kept for a few seconds, so
painting can ask every time. Any other system: no such wish is known.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Callable, Dict, Tuple

log = logging.getLogger(__name__)

# How long an answer is kept: a setting changed is followed soon after.
_KEPT_S = 3.0
_answers: Dict[str, Tuple[float, bool]] = {}


def _mac(name: str) -> bool:
    from rubicon.objc import ObjCClass
    from rubicon.objc.runtime import load_library
    load_library("AppKit")
    return bool(getattr(ObjCClass("NSWorkspace").sharedWorkspace, name))


def _windows(action: int) -> bool:
    import ctypes
    value = ctypes.c_int(0)
    ctypes.windll.user32.SystemParametersInfoW(action, 0, ctypes.byref(value), 0)
    return bool(value.value)


def _windows_high_contrast() -> bool:
    import ctypes

    class HighContrast(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
                    ("lpszDefaultScheme", ctypes.c_wchar_p)]

    info = HighContrast(ctypes.sizeof(HighContrast), 0, None)
    ctypes.windll.user32.SystemParametersInfoW(0x0042, info.cbSize, ctypes.byref(info), 0)
    return bool(info.dwFlags & 1)


def _asked(key: str, read: Callable[[], bool]) -> bool:
    now = time.monotonic()
    found = _answers.get(key)
    if found is not None and now - found[0] < _KEPT_S:
        return found[1]
    try:
        answer = read()
    except (ImportError, AttributeError, OSError) as error:
        # No bridge to the system's settings here: no wish is known.
        log.info("%s: %s", key, error)
        answer = False
    _answers[key] = (now, answer)
    return answer


def reduce_motion() -> bool:
    """Whether the person asked for less motion on screen."""
    if sys.platform == "darwin":
        return _asked("motion", lambda: _mac("accessibilityDisplayShouldReduceMotion"))
    if sys.platform == "win32":
        # SPI_GETCLIENTAREAANIMATION: off means no animation is wanted.
        return _asked("motion", lambda: not _windows(0x1042))
    return False


def increase_contrast() -> bool:
    """Whether the person asked for more contrast on screen."""
    if sys.platform == "darwin":
        return _asked("contrast", lambda: _mac("accessibilityDisplayShouldIncreaseContrast"))
    if sys.platform == "win32":
        return _asked("contrast", _windows_high_contrast)
    return False
