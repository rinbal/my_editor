# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first message to the editorial team, after the receipt (plan
10.3): sealed by the account (one signer prompt), wrapped to the
submission's room, sent with a session; only the server's ``seq`` says
it is there. It was part of :mod:`.standup_submit`.

The submission is in by then, so a message the connection or the
server's trouble stopped before it arrived is sent again quietly, with
backoff, the same wrap and no new prompt (lessons 36); only when those
tries are spent does the handoff say it was not sent, with Send Again.
One that may have arrived is never sent twice.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from ..failures import retry_quietly
from ..later import answer_later
from ..signed_api import signer_failure_code
from ..standup import conversation, formats
from ..standup.drafting import Sealed
from ..standup.errors import PASSING, Code, StandupError


def send_first_message(account, sealed: Sealed, text: str, on_signed: Callable[[], None],
                       on_sent: Callable[[], None],
                       on_failure: Callable[..., None], *,
                       later: Callable[[int, Callable[[], None]], None]) -> None:
    """``on_failure(error)``: not sent, with what stopped it: built here
    (``refused``), the signer app (its refusal or its silence, as a
    submission's is read), or the server (its error, a limit's wait in
    ``retry_after``). Nothing answers once ``account`` is let go: the
    signer app may answer after it, and then nothing is posted (no new
    session for an account left)."""
    room = sealed.p_sub
    try:
        rumor = conversation.message_rumor(room=room, submission_id=sealed.submission_id,
                                           text=text, sender=account.writer,
                                           created_at=int(time.time()))
    except formats.FormatError as exc:
        answer_later(on_failure, StandupError(Code.REFUSED, message=str(exc)[:200]))
        return

    def post(wrap: dict, answer: Callable[[Optional[StandupError]], None]) -> None:
        if not account.stopped:
            account.api.send_message(sealed.submission_id, wrap, kind_hint="message",
                                     on_success=lambda _seq: answer(None),
                                     on_failure=answer)

    def passing(error: Optional[StandupError]) -> bool:
        return error is not None and error.code in PASSING and not error.maybe_stored

    def posted(error: Optional[StandupError]) -> None:
        if account.stopped:
            return
        if error is None:
            on_sent()
        else:
            on_failure(error)

    def wrapped(wrap: dict) -> None:
        if account.stopped:
            return
        on_signed()
        retry_quietly(lambda answer: post(wrap, answer),
                      settled=lambda error: not passing(error), on_done=posted, later=later)

    def unsealed(code: str, why: str) -> None:
        if not account.stopped:
            on_failure(StandupError(code, message=str(why)[:200]))

    conversation.seal_and_wrap(
        account.signer, rumor, sealed.s_sub, room, on_success=wrapped,
        on_failure=lambda why: unsealed(signer_failure_code(why), why),
        on_refused=lambda why: unsealed(Code.REFUSED, why))


__all__ = ["send_first_message"]
