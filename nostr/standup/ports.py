# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The requests of a submission on its way (:mod:`.submitting`), as one
value the window wires (nostr/ui/standup_submit.py, StandupWriter._ports)
and the tests fake. Each is asked by the run and answered through the
callbacks it hands over; none says anything on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .drafting import EncryptedPicture, Sealed
from .entries import RecordResult
from .errors import StandupError
from .rounds import RoundAnswer
from .trust import KeyNotConfirmed, RedaktionKey

UploadDone = Callable[[Mapping[str, Tuple[str, ...]]], None]


@dataclass(frozen=True)
class Ports:
    """Every request, as the window wires it (tests: fakes).

    - ``confirm_key(on_key, on_refused)``: trust.confirm_key for the
      receiving server.
    - ``upload(pictures, on_progress(fraction), on_done({sha256: servers}),
      on_failure(words))`` returns a callable that stops it.
    - ``held(sid)``: SubmissionsList.entry, the list's entry for a sid,
      whose secrets a new revision 1 of it keeps (contract 2).
    - ``record(entry, on_done(RecordResult))``: SubmissionsList.record.
    - ``forget(entry)``: SubmissionsList.forget, for an entry recorded for
      a request that was cancelled before it was sent, or that the
      server refused for its round.
    - ``submit(body, on_sending(), on_success(receipt check),
      on_failure(StandupError))`` returns a callable that drops it.
    - ``lookup(sealed, on_done(status), on_failure(StandupError))``:
      watcher.resolve_unconfirmed for ``sealed.entry``, its receipt checked
      against ``sealed.body``, what was sent.
    - ``send_message(sealed, text, on_signed(), on_sent(), on_failure(wait))``:
      ``on_signed`` once the signer sealed it, before it is sent; ``wait``
      the seconds a limit asks for (a 429's Retry-After), or None.
    - ``choose_round(entry, on_done(RoundAnswer))``: contract 2 from step
      1 for the work of ``entry``, the entry of a revision 1 the server
      refused as ``stale_revision`` (rounds.ask_round with ``stored``).
    - ``later(ms, callback)``; ``clock()``.
    """

    confirm_key: Callable[[Callable[[RedaktionKey], None],
                           Callable[[KeyNotConfirmed], None]], None]
    upload: Callable[[Sequence[EncryptedPicture], Callable[[float], None], UploadDone,
                      Callable[[str], None]], Callable[[], None]]
    held: Callable[[str], Optional[Any]]
    record: Callable[[Any, Callable[[RecordResult], None]], None]
    forget: Callable[[Any], None]
    submit: Callable[[Mapping[str, Any], Callable[[], None], Callable[[Any], None],
                      Callable[[StandupError], None]], Callable[[], None]]
    lookup: Callable[[Any, Callable[[Any], None], Callable[[StandupError], None]], None]
    send_message: Callable[[Sealed, str, Callable[[], None], Callable[[], None],
                            Callable[[], None]], None]
    choose_round: Callable[[Any, Callable[[RoundAnswer], None]], None]
    later: Callable[[int, Callable[[], None]], None]
    clock: Callable[[], float]


__all__ = ["Ports", "UploadDone"]
