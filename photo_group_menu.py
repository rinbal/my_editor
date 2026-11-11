# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the photo commands are called, and what each would do now.

The commands have one name each (LEFT, SPLIT, ...), run by
photo_group_editing.PhotoEditing.run. Format > Photo lists all of them
(dimmed where they cannot run, the menu bar's rule) and a picture's
context menu only those that can (EDITING.md 2.2 of the photo grid
design); state_of says for the picture selected whether each can run and
what it is called then: Move Up of a picture that stands alone is "Into
the Group Above" when a group is above it. chip_words says what a chip on
a picture, or the bar's button, does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

import photo_group_actions as actions
import photo_group_overlay as overlay
import photo_group_view
from i18n import _
from photo_group_actions import BESIDE, GAP, SLOT, Picture, Place

LEFT, RIGHT, UP, DOWN, FIRST, LAST = "left", "right", "up", "down", "first", "last"
LARGE, TAKE_OUT, SPLIT, JOIN_ABOVE, JOIN_BELOW = "large", "take_out", "split", "join_above", \
    "join_below"
CAPTION, DESCRIPTION, CROP, ADD, REMOVE, MOVE_MODE = "caption", "description", "crop", "add", \
    "remove", "move_mode"
COMMANDS = (LEFT, RIGHT, UP, DOWN, FIRST, LAST, LARGE, TAKE_OUT, SPLIT, JOIN_ABOVE, JOIN_BELOW,
            CAPTION, DESCRIPTION, CROP, ADD, REMOVE, MOVE_MODE)
# Format > Photo, section by section (Remove last, apart).
MENU_SECTIONS = ((LEFT, RIGHT, UP, DOWN, FIRST, LAST, MOVE_MODE), (LARGE,),
                 (TAKE_OUT, SPLIT, JOIN_ABOVE, JOIN_BELOW), (CAPTION, DESCRIPTION, CROP),
                 (ADD,), (REMOVE,))


def context_sections(grouped: bool, narrow: bool):
    """A picture's context menu, section by section: in a group the moves
    along it (up and down in a narrow column), alone the hops past the
    words; the menu shows only what can run."""
    if not grouped:
        moves = (UP, DOWN)
    else:
        moves = (UP, DOWN, FIRST, LAST) if narrow else (LEFT, RIGHT, FIRST, LAST)
    return (moves, (LARGE,), (TAKE_OUT, SPLIT, JOIN_ABOVE, JOIN_BELOW),
            (CAPTION, DESCRIPTION, CROP), (REMOVE,))


def base_titles() -> Dict[str, str]:
    """Each command's title while no picture says otherwise."""
    return {LEFT: _("Move Left"), RIGHT: _("Move Right"), UP: _("Move Up"),
            DOWN: _("Move Down"), FIRST: _("Move to Start"), LAST: _("Move to End"),
            LARGE: _("Show Large"), TAKE_OUT: _("Take Out of Group"),
            SPLIT: _("Split Group Here"), JOIN_ABOVE: _("Join the Group Above"),
            JOIN_BELOW: _("Join the Group Below"), CAPTION: _("Caption…"),
            DESCRIPTION: _("Description…"), CROP: _("Crop…"),
            ADD: _("Add Photos…"), REMOVE: _("Remove"),
            MOVE_MODE: _("Move With Arrow Keys")}


@dataclass
class PhotoState:
    """What the photo commands would do now, for the menus: the picture,
    its header ("Photo 2 of 4"), and per command whether it can run, its
    title and a hint (a second line, shown as the item's tooltip)."""

    picture: Optional[Picture] = None
    header: str = ""
    can: Dict[str, bool] = field(default_factory=dict)
    titles: Dict[str, str] = field(default_factory=base_titles)
    hints: Dict[str, str] = field(default_factory=dict)
    large: bool = False


def hop_title(picture: Picture, down: bool, place: Optional[Place]) -> str:
    """Move Up (Down) by where it takes ``picture``."""
    if place is None or (place.kind == SLOT and place.at == picture.first and picture.grouped):
        return _("Move Down") if down else _("Move Up")
    if place.kind == SLOT:
        return _("Move Into the Group Below") if down else _("Move Into the Group Above")
    if place.kind == BESIDE:
        return _("Move Next to the Photo Below") if down else _("Move Next to the Photo Above")
    return _("Move Below the Next Paragraph") if down else _("Move Above the Paragraph Before")


def state_of(photos) -> PhotoState:
    """What each command would do to ``photos``' current picture (a
    photo_group_editing.PhotoEditing)."""
    picture = photos.current()
    state = PhotoState(picture, can={name: False for name in COMMANDS})
    if picture is None:
        # From the words: a new group at the caret (the slash menu's
        # Photo Group…).
        state.can[ADD] = photos.free_place() is not None
        return state
    doc = photos.document()
    grouped, narrow = picture.grouped, photos.narrow()
    first, last = picture.index == 0, picture.index == picture.count - 1
    state.header = photos.place_words(picture) if grouped else _("Photo")
    hops = {UP: photos.hop(picture, False), DOWN: photos.hop(picture, True)}
    joins = {JOIN_ABOVE: actions.neighbour(doc, picture, False),
             JOIN_BELOW: actions.neighbour(doc, picture, True)}
    state.can.update({
        LEFT: grouped and not narrow and not first, RIGHT: grouped and not narrow and not last,
        FIRST: grouped and not first, LAST: grouped and not last,
        UP: hops[UP] is not None, DOWN: hops[DOWN] is not None,
        LARGE: True, TAKE_OUT: grouped and photos.take_out_place(picture) is not None,
        SPLIT: grouped and not last,
        JOIN_ABOVE: joins[JOIN_ABOVE] is not None, JOIN_BELOW: joins[JOIN_BELOW] is not None,
        # Crop… needs the picture itself: not a stand-in (an SVG drawing,
        # a file not there, one on its way).
        CAPTION: True, DESCRIPTION: True, ADD: True, REMOVE: True,
        CROP: photo_group_view.resource_sizes(doc)(picture.unit.name) is not None,
        MOVE_MODE: grouped and picture.count > 1})
    titles = state.titles
    titles[UP] = hop_title(picture, False, hops[UP])
    titles[DOWN] = hop_title(picture, True, hops[DOWN])
    for name, other in joins.items():
        if other is not None and not other.grouped:
            titles[name] = (_("Join the Photo Above") if name == JOIN_ABOVE
                            else _("Join the Photo Below"))
    below = actions.beyond(doc, picture, True)
    state.hints[TAKE_OUT] = (_("Then it stands alone under the next paragraph")
                             if below is not None and below.kind == GAP
                             else _("Then it stands alone above the paragraph before"))
    state.hints[SPLIT] = _("Write text between the photos")
    already = grouped and photo_group_view.own_row(doc, picture.unit)
    state.large = picture.unit.wide or bool(already)
    if grouped:
        state.hints[LARGE] = (_("Already the whole width") if already
                              else _("A row of its own across the whole width"))
    else:
        titles[LARGE] = _("Wide")
        state.hints[LARGE] = _("Show wider than the text")
    return state


def chip_words(photos, picture: Picture, chip: str) -> str:
    """What the ``chip`` (photo_group_overlay's HANDLE, MORE, ALT, BIG or
    ADD, the bar's button) on ``picture`` does, for its tooltip."""
    if chip == overlay.HANDLE:
        return _("Drag to move it, or click to move it with the arrow keys")
    if chip == overlay.MORE:
        return _("All commands for {photo}").format(photo=photos.place_words(picture))
    if chip == overlay.ALT:
        alt = photos.described(picture.unit)
        return (_("Description: {words}").format(words=alt) if alt
                else _("Description missing: for people who can’t see the photo"))
    if chip == overlay.BIG:
        if not picture.grouped:
            return (_("Show as wide as the text") if picture.unit.wide
                    else _("Show wider than the text"))
        if photo_group_view.own_row(photos.document(), picture.unit):
            return _("Already the whole width")
        return (_("Show normal again") if picture.unit.wide
                else _("Show large: a row of its own across the whole width"))
    return _("Add photos to this group. Or drag them onto it.")
