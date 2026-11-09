# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reduce Motion, for every animation the app plays.

Someone who turned motion down in the system's settings (macOS Reduce
motion, Windows Animation effects, GTK's enable-animations) gets the same
states and the same words, without the movement: what would travel or
scale is where it ends at once, and a change the eye should still notice
cross-fades instead. The handoff of a submission to a publication will
be built on that swap.

How the preference is read, kept and read again lives with the other
native preferences in native_appearance.py. Here is what an animation does
with it:

    animation.setDuration(motion.duration(160))                   # lifts
    fade.setDuration(motion.duration(320, cross_fade=True))       # travels

No animation asks it yet: the handoff of a submission, still to come, will
be the first. The drafts preview (drafts_preview.py) predates it and does
not ask: its fades need no reducing, its geometry swap still moves.
"""
from __future__ import annotations

from . import native_appearance

# How long a state change cross-fades when motion is reduced: nothing
# travels, the states cross-fade in 120 ms, the words stay the same.
CROSS_FADE_MS = 120


def reduce_motion() -> bool:
    """True when the system asks for less motion. Cheap to ask before every
    animation: the answer is kept until the app comes back to the front."""
    return native_appearance.reduce_motion_preference()


def duration(ms: int, *, cross_fade: bool = False) -> int:
    """How long an animation designed to take ``ms`` milliseconds runs.

    As designed, unless motion is reduced. Then movement takes no time at
    all (it shows where it ends), and with ``cross_fade`` the change is a
    ``CROSS_FADE_MS`` fade instead, for a step the person should still see
    happen, such as a card settling into place.
    """
    if not reduce_motion():
        return max(0, int(ms))
    return CROSS_FADE_MS if cross_fade else 0
