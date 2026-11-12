# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A limit's wait (contract 15: a 429 and its Retry-After), the same on
every screen that offers to try again (RG-M2r 7, RG-M2r2a 5): the words
of when trying again helps (:func:`wait_words`, :func:`limit_words`, one
sentence with the limit, lessons 36), and a timer that shows the state
again once the wait is over (:class:`WaitTimer`), so a dimmed Try Again
comes back by itself, with words that say it works now, shown and not
announced (RG-M2r3 14): the person who waited for it sees it there."""

from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QDateTime, QLocale, QObject, QTimer

import i18n
from i18n import _

from ..standup import words
from ..standup.errors import Code


def wait_words(until: Optional[float], now: float, today: str, tomorrow: str,
               over: str = "", **names: str) -> str:
    """When a limit's wait is over, in the language's clock time, rounded
    up to the minute shown (``today`` or ``tomorrow``, untranslated, with
    ``{time}`` and ``names``); once it is over, ``over`` (that trying
    again works now); "" when there is no wait (or it is over and there
    is no ``over``)."""
    if until is None:
        return ""
    if until <= now:
        return _(over).format(**names) if over else ""
    shown = QDateTime.fromSecsSinceEpoch(int(math.ceil(until / 60) * 60))
    locale = i18n.display_locale()
    clock = locale.toString(shown.time(), locale.timeFormat(QLocale.FormatType.ShortFormat))
    later_day = shown.date() > QDateTime.fromSecsSinceEpoch(int(now)).date()
    return _(tomorrow if later_day else today).format(time=clock, **names)


def limit_words(publication: str, until: Optional[float], now: float) -> str:
    """A request met a limit of ``publication``'s server: one sentence,
    with when trying again helps, as a run says it."""
    refusal = words.refusal(Code.RATE_LIMITED)
    return wait_words(until, now, words.LIMIT_AT, words.LIMIT_TOMORROW_AT, words.LIMIT_OVER,
                      publication=publication) or _(refusal).format(publication=publication)


def server_limit_words(until: Optional[float], now: float) -> str:
    """A request met a limit of the server itself (the publications'
    list): one sentence, with when trying again helps."""
    return wait_words(until, now, words.SERVER_LIMIT_AT, words.SERVER_LIMIT_TOMORROW_AT,
                      words.SERVER_LIMIT_OVER) or _(words.SERVER_LIMIT_LATER)


class WaitTimer(QTimer):
    """Calls ``over`` once the soonest of the waits followed is over."""

    def __init__(self, over: Callable[[], None], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.setSingleShot(True)
        self.timeout.connect(over)

    def follow(self, *untils: Optional[float], now: float) -> bool:
        """Follow the waits ``untils`` (clock times; None for none);
        whether one is not over yet."""
        ahead = [at for at in untils if at is not None and at > now]
        if ahead:
            self.start(max(1, int(math.ceil((min(ahead) - now) * 1000))))
        else:
            self.stop()
        return bool(ahead)


__all__ = ["WaitTimer", "limit_words", "server_limit_words", "wait_words"]
