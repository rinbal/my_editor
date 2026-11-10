# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One submission on its way, for the window (plan 10.3):
:class:`SubmissionRun`, :class:`~nostr.standup.submitting.Submitting`
with its progress as a Qt signal, and what the status bar says of runs
whose sheet is closed or whose account was let go. The words come from
what the server holds (``Progress.held``, lesson 32)."""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from i18n import _, ngettext

from ..standup import words
from ..standup.drafting import ArticleDraft, Choice
from ..standup.submitting import (
    HELD_UNKNOWN_FIRST, HELD_WORDS, MessageStatus, Phase, Ports, Progress, Submitting,
)


class SubmissionRun(QObject):
    """One submission on its way: :class:`Submitting` with its progress as
    a Qt signal, for the sheet and for the window. ``draft`` and
    ``choice`` name the work and where it goes: one run per both."""

    changed = Signal(object)            # Progress

    def __init__(self, draft: ArticleDraft, choice: Choice, *, writer: str, round_: int,
                 ports: Ports, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.draft = draft
        self._started = False
        self._let_go = False
        self._behind: Optional[Callable[[Progress], None]] = None
        self.submitting = Submitting(draft, choice, writer=writer, round_=round_,
                                     ports=ports, on_change=self.changed.emit)

    @property
    def progress(self) -> Progress:
        return self.submitting.progress

    @property
    def choice(self) -> Choice:
        return self.submitting.choice

    @property
    def finished(self) -> bool:
        """Ended, its first message included: a run whose message is still
        being signed or sent, or did not go while no sheet showed it, is
        shown again for its work (Send Again there) until :meth:`let_go`."""
        return ended(self.progress) and (self._let_go or self.progress.message
                                          in (MessageStatus.NONE, MessageStatus.SENT))

    def let_go(self) -> None:
        """A sheet showing it in, its message not sent, was closed: the
        person saw Send Again and chose not to; the text may be submitted
        elsewhere."""
        self._let_go = True

    def behind_the_sheet(self, on_change: Optional[Callable[[Progress], None]]) -> None:
        """What follows its progress while no sheet shows it (the status
        bar's words), or ``None`` while a sheet shows it again: one at a
        time, so a sheet closed twice says each change once."""
        if self._behind is not None:
            self.changed.disconnect(self._behind)
        self._behind = on_change
        if on_change is not None:
            self.changed.connect(on_change)

    def start(self) -> None:
        """Once: a run shown again (one per work) goes on where it is."""
        if not self._started:
            self._started = True
            self.submitting.start()

    def cancel(self) -> None:
        self.submitting.cancel()

    def retry(self) -> None:
        self.submitting.retry()

    def retry_message(self) -> None:
        self.submitting.retry_message()


def ended(progress: Progress) -> bool:
    """In, failed or cancelled: nothing of the submission goes on (its
    first message may)."""
    return progress.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED)


def stopped_words(runs: Sequence[SubmissionRun]) -> str:
    """What the status bar says when the account of ``runs`` is let go
    while some are on their way, or ``""``. The words come from what the
    server holds (``Progress.held``, lesson 32, the same fact a failure
    says): nothing, a request it may hold (the submit sheet, opened again
    with that account, finds out), the revision with a picture still
    going up, or, once in, a first message being signed or sent (one that
    did not go was said then). The run furthest from known is named."""
    going = [run for run in runs if not ended(run.progress)]
    if going:
        run = min(going, key=lambda r: HELD_UNKNOWN_FIRST.index(r.progress.held))
        text = HELD_WORDS[run.progress.held].stopped
        others = len(going) - 1
    else:
        talking = [run for run in runs if run.progress.message
                   in (MessageStatus.SIGNING, MessageStatus.SENDING)]
        if not talking:
            return ""
        run, text, others = talking[0], words.STOPPED_MESSAGE, 0
    said = _(text).format(publication=run.choice.name)
    if others:
        said += " " + ngettext("{count} other submission stopped as well.",
                               "{count} other submissions stopped as well.",
                               others).format(count=others)
    return said


def background_message_words(progress: Progress, publication: str) -> str:
    """With the sheet closed, what the status bar says of the first
    message: why the signer app asks now, or that it did not go; ``""``
    otherwise (it is sent, or there is none)."""
    if progress.message is MessageStatus.SIGNING:
        return _(words.MESSAGE_SIGNING_IN_THE_BACKGROUND).format(publication=publication)
    if progress.message is MessageStatus.FAILED:
        return _(words.MESSAGE_NOT_SENT_IN_THE_BACKGROUND).format(publication=publication)
    return ""


__all__ = ["SubmissionRun", "background_message_words", "ended", "stopped_words"]
