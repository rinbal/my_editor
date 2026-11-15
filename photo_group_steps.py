# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The steps of Undo the photo commands made, so Undo and Redo of one
select its picture again and name it (PhotoEditing.came_back).

A step is kept by Qt's count of steps where it ends, with the count
before it, as the editor keeps its names (HtmlEditor._record_step). The
editor tells every step it names (recorded). A photo step just made that
the step named now holds is that step from then on, kept where it ends,
under the step's name: a slash command around Add Photos (its step began
before, with taking "/photo" away). A photo step
the named one replaces, undone before it, is gone, as the editor forgets
its name.
"""

from __future__ import annotations

from typing import Dict, NamedTuple, Optional, Tuple


class Step(NamedTuple):
    """A step of Undo a command made: Qt's count before it, its name, and
    where its picture stood before and after it."""

    before: int
    name: str
    start: int
    end: int


class PhotoSteps:
    """The photo steps of one editor's document."""

    def __init__(self, editor) -> None:
        self._editor = editor
        self._kept: Dict[int, Step] = {}
        # Where the step kept last ends, while the next step named may be it.
        self._open: Optional[int] = None

    def keep(self, before: int, name: str, start: int, end: int) -> None:
        """The step that just ended (Qt's count ``before`` it) is a photo step."""
        after = self._editor.document().availableUndoSteps()
        self._kept[after] = Step(before, name, start, end)
        self._open = after

    def drop(self, after: int) -> None:
        """The step that ended at Qt's count ``after`` is gone."""
        self._kept.pop(after, None)
        if self._open == after:
            self._open = None
        self._editor.forget_step(after)

    def recorded(self, before: int, after: int) -> None:
        """The editor named the step from Qt's count ``before`` to ``after``."""
        just_made = self._kept.get(self._open) if self._open is not None else None
        self._kept = {key: step for key, step in self._kept.items()
                      if not before < key <= after}
        if just_made is not None and before <= just_made.before and self._open <= after:
            self._kept[after] = just_made._replace(before=before)
            self._open = after
        else:
            self._open = None

    def came_back(self, count: int, redo: bool) -> Optional[Tuple[int, str]]:
        """Undo (``redo`` False) or Redo was done at Qt's count ``count``:
        where the picture of the photo step it took back or brought again
        stands now, and the step's name as the editor has it; None for
        another step."""
        self._open = None
        named = self._editor.named_step
        if redo:
            for after, step in self._kept.items():
                found = named(after)
                if step.before == count and found is not None and found[0] == count:
                    return step.end, found[1]
            return None
        step = self._kept.get(count)
        found = named(count)
        if step is None or found is None or found[0] != step.before:
            return None
        return step.start, found[1]
