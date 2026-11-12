# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The names Einreichungen says (M-4 item 7): the editors of the member
list and the people taking part, by the name their own profile (kind 0)
gives, read once each while the app runs.

The window's own accounts and the people it knows name them first
(``known``); a profile is read only for a key the submissions show
(``name`` asks for it), each once, and :attr:`ProfileNames.learned`
fires when a name came in, so the list and the detail say it. A profile
that could not be read says nothing (lessons 36): the words then say
"the editorial team", never an address (lesson 26).
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal

from ..metadata import profile_fields
from ..standup import formats

# The longest name kept from a profile (a profile may say anything).
NAME_MOST = 64


def name_in(fields: dict) -> str:
    """The name a profile gives: its ``display_name``, else its ``name``,
    trimmed to one line of at most :data:`NAME_MOST` characters."""
    for key in ("display_name", "name"):
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:NAME_MOST]
    return ""


class ProfileNames(QObject):
    """See the module docstring. ``read(pubkey, answer)`` reads a
    profile's fields (``metadata.profile_fields``)."""

    learned = Signal()

    def __init__(self, *, known: Callable[[str], str], read: Callable,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._known, self._read = known, read
        self._names: Dict[str, str] = {}

    @classmethod
    def on_relays(cls, pool, directory, *, known: Callable[[str], str],
                  parent: Optional[QObject] = None) -> "ProfileNames":
        def read(pubkey: str, answer) -> None:
            profile_fields(pool, directory, pubkey, "", answer, parent=parent)
        return cls(known=known, read=read, parent=parent)

    def name(self, pubkey: str) -> str:
        """``pubkey``'s name, or ``""`` (its profile is read once then)."""
        found = self._known(pubkey) if pubkey else ""
        if found or not formats.is_hex64(pubkey):
            return found
        if pubkey not in self._names:
            self._names[pubkey] = ""
            self._read(pubkey, lambda fields, key=pubkey: self._take(key, fields))
        return self._names[pubkey]

    def _take(self, pubkey: str, fields: Optional[dict]) -> None:
        name = name_in(fields or {})
        if name:
            self._names[pubkey] = name
            self.learned.emit()


__all__ = ["NAME_MOST", "ProfileNames", "name_in"]
