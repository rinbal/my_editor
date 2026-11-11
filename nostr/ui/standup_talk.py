# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Messages and reactions on their way to a submission's conversation
(plan 7.3, 10.7; editorial contract 9, 12.4).

:class:`Talk` is the one owner of what was sent and has no ``seq`` yet
(lesson 7): each :class:`Outgoing` is kept by its rumor's id until the
server stored it, whatever screen is open. It is sealed by the account
(the one signer prompt), wrapped to the submission's room and posted
with the session; only the server's ``seq`` says it is there (plan
10.7: "Wird gesendet …" until then).

- The connection, or the server's own trouble, before anything
  arrived: sent again quietly, the same wrap, with backoff (lessons 36,
  :func:`~nostr.failures.retry_quietly`). Only when those tries are
  spent it says "Nicht gesendet", keeps the text, and offers one retry.
- No answer after it went out: it may have arrived, so it is never
  sent twice. It says "Noch nicht bestätigt" with Erneut prüfen, which
  reads the conversation again: there, it is stored; not there after a
  whole read, it was not sent and may go again.
- A reaction (or its taking back) goes the same way; one that did not
  go leaves the bubble as it was and says "Reaktion nicht gesendet"
  with Erneut.

Nothing answers once the account is let go (lesson 31).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from i18n import N_, _

from ..failures import SIGNER_DECLINED, SIGNER_SILENT, retry_quietly
from ..signed_api import signer_failure_code
from ..standup import conversation, formats
from ..standup.errors import PASSING, Code, StandupError, cause_words
from .standup_account import live

NOT_SENT = N_("Not sent.")
NOT_CONFIRMED = N_("Not confirmed yet.")
SENDING = N_("Sending …")
REACTION_NOT_SENT = N_("Reaction not sent.")
MESSAGE_EMPTY = N_("Write a message to send it.")
MESSAGE_TOO_LONG = N_("This message is too long to send. Shorten it.")
MESSAGE_LIMIT = N_("The server takes no more messages from you right now: send it again "
                   "later.")
MESSAGE_REFUSED = N_("The server didn’t take it: try again later.")
NOT_BUILT = N_("MyEditor couldn’t put it together.")

_SENTENCES = {Code.SIGNER_DECLINED: SIGNER_DECLINED, Code.SIGNER_UNREACHABLE: SIGNER_SILENT,
              Code.RATE_LIMITED: MESSAGE_LIMIT, Code.REFUSED: NOT_BUILT,
              **{code: cause_words(code) for code in PASSING}}


class Sending(Enum):
    SIGNING = "signing"             # the signer app is asked to seal it
    SENDING = "sending"
    FAILED = "failed"               # not sent: the text is kept, one retry
    UNCONFIRMED = "unconfirmed"     # it may have arrived: check, never send twice


@dataclass
class Outgoing:
    """One message, reaction or taking back that has no ``seq`` yet."""

    key: str                        # its rumor's id
    sid: str
    kind: str                       # conversation.HINT_MESSAGE, _REACTION or _RETRACTION
    text: str                       # the message, or the reaction's emoji ("" taking back)
    rumor: dict = field(repr=False)
    target: str = ""                # the message a reaction is on
    wrap: Optional[dict] = field(default=None, repr=False)
    state: Sending = Sending.SIGNING
    sentence: str = ""              # why it did not go (untranslated)

    @property
    def on_its_way(self) -> bool:
        return self.state is not Sending.FAILED

    @property
    def is_message(self) -> bool:
        return self.kind == conversation.HINT_MESSAGE


def failure_sentence(error: StandupError) -> str:
    return _SENTENCES.get(error.code, MESSAGE_REFUSED)


class Talk:
    """See the module docstring. ``room(sid)`` gives the submission's
    room (its secret and its pubkey) or None while it is not open;
    ``api_for(sid)`` the client of its server; ``stored(out, seq)`` is
    told once the server stored one; ``reread(sid, on_done)`` reads the
    conversation again and answers the gift wrap ids it holds, or None
    when it could not be read; ``changed(sid)`` after every change."""

    def __init__(self, account, *, room: Callable[[str], Optional[Tuple[bytes, str]]],
                 api_for: Callable[[str], object],
                 stored: Callable[[Outgoing, int], None],
                 reread: Callable[[str, Callable[[Optional[Dict[str, int]]], None]], None],
                 changed: Callable[[str], None],
                 later: Callable[[int, Callable[[], None]], None],
                 clock: Callable[[], float] = time.time) -> None:
        self._account = account
        self._room = room
        self._api_for = api_for
        self._stored = stored
        self._reread = reread
        self._changed = changed
        self._later = later
        self._clock = clock
        self._outgoing: Dict[str, Outgoing] = {}

    def stop(self) -> None:
        self._outgoing = {}

    def outgoing(self, sid: str) -> List[Outgoing]:
        return [out for out in self._outgoing.values() if out.sid == sid]

    def reaction_on(self, sid: str, target: str) -> Optional[Outgoing]:
        """The reaction (or taking back) for the message ``target`` that
        has no ``seq`` yet."""
        return next((out for out in self._outgoing.values()
                     if out.sid == sid and out.target == target), None)

    # -- sending ---------------------------------------------------------------

    def send(self, sid: str, text: str) -> str:
        """Send ``text`` to the conversation of ``sid``; ``""``, or why it
        cannot be sent as it is (untranslated: empty, too long, a secret
        key in it), said in the composer's place with the text kept."""
        room = self._room(sid)
        if room is None:
            return NOT_BUILT
        try:
            rumor = conversation.message_rumor(room=room[1], submission_id=sid, text=text,
                                               sender=self._account.writer,
                                               created_at=int(self._clock()))
        except formats.SecretKeyFound as found:
            return str(found)
        except formats.FormatError:
            return MESSAGE_TOO_LONG if text.strip() else MESSAGE_EMPTY
        self._start(Outgoing(key=rumor["id"], sid=sid, kind=conversation.HINT_MESSAGE,
                             text=text, rumor=rumor))
        return ""

    def react(self, sid: str, target: str, target_author: str, emoji: str,
              standing: Optional[Tuple[str, str]]) -> None:
        """React to the message ``target`` with ``emoji``; the same one
        again takes it back (plan 7.3). ``standing`` is this person's
        stored reaction to it, ``(rumor id, emoji)``. One at a time per
        message: while one is on its way, another is not started."""
        room = self._room(sid)
        waiting = self.reaction_on(sid, target)
        if room is None or (waiting is not None and waiting.on_its_way):
            return
        if waiting is not None:
            del self._outgoing[waiting.key]         # one that failed: this one replaces it
        now = int(self._clock())
        if standing is not None and standing[1] == emoji:
            rumor = conversation.retraction_rumor(room=room[1], submission_id=sid,
                                                  reaction_id=standing[0],
                                                  sender=self._account.writer, created_at=now)
            kind, text = conversation.HINT_RETRACTION, ""
        else:
            rumor = conversation.reaction_rumor(room=room[1], submission_id=sid,
                                                target_id=target, target_author=target_author,
                                                emoji=emoji, sender=self._account.writer,
                                                created_at=now)
            kind, text = conversation.HINT_REACTION, emoji
        self._start(Outgoing(key=rumor["id"], sid=sid, kind=kind, text=text, rumor=rumor,
                             target=target))

    def retry(self, key: str) -> None:
        """Erneut senden for one that did not go (the same rumor, and the
        same wrap when it was sealed); Erneut prüfen for one that may
        have arrived."""
        out = self._outgoing.get(key)
        if out is None:
            return
        if out.state is Sending.FAILED:
            self._start(out)
        elif out.state is Sending.UNCONFIRMED:
            self._check(out)

    def dismiss(self, key: str) -> None:
        """A reaction that did not go is let be."""
        out = self._outgoing.get(key)
        if out is not None and out.state is Sending.FAILED and not out.is_message:
            del self._outgoing[key]
            self._changed(out.sid)

    # -- the steps -------------------------------------------------------------

    def _show(self, out: Outgoing, state: Sending, sentence: str = "") -> None:
        out.state, out.sentence = state, sentence
        self._changed(out.sid)

    def _start(self, out: Outgoing) -> None:
        self._outgoing[out.key] = out
        if out.wrap is not None:
            self._post(out)
            return
        room = self._room(out.sid)
        if room is None:
            self._show(out, Sending.FAILED, NOT_BUILT)
            return
        self._show(out, Sending.SIGNING)
        account = self._account

        def wrapped(wrap: dict) -> None:
            if self._outgoing.get(out.key) is out:
                out.wrap = wrap
                self._post(out)

        def unsealed(code: str) -> None:
            if self._outgoing.get(out.key) is out:
                self._show(out, Sending.FAILED, _SENTENCES.get(code, NOT_BUILT))

        conversation.seal_and_wrap(
            account.signer, out.rumor, room[0], room[1], on_success=live(account, wrapped),
            on_failure=live(account, lambda why: unsealed(signer_failure_code(why))),
            on_refused=live(account, lambda _why: unsealed(Code.REFUSED)), clock=self._clock)

    def _post(self, out: Outgoing) -> None:
        self._show(out, Sending.SENDING)
        account, api = self._account, self._api_for(out.sid)

        def post(answer: Callable[[object], None]) -> None:
            if not account.stopped:
                api.send_message(out.sid, out.wrap, kind_hint=out.kind,
                                 on_success=answer, on_failure=answer)

        def posted(result: object) -> None:
            if account.stopped or self._outgoing.get(out.key) is not out:
                return
            if not isinstance(result, StandupError):
                self._done(out, result)
            elif result.maybe_stored:
                # It may have arrived: never sent again before a check.
                self._show(out, Sending.UNCONFIRMED)
            else:
                self._show(out, Sending.FAILED, failure_sentence(result))

        retry_quietly(post, settled=lambda result: not (
            isinstance(result, StandupError) and result.code in PASSING
            and not result.maybe_stored), on_done=posted, later=self._later)

    def _done(self, out: Outgoing, seq: int) -> None:
        del self._outgoing[out.key]
        self._stored(out, seq)

    def _check(self, out: Outgoing) -> None:
        """Erneut prüfen: the conversation read again. The wrap there: it
        is stored. Not there, after a whole read: it was not sent."""
        def read(wraps: Optional[Dict[str, int]]) -> None:
            if self._outgoing.get(out.key) is not out:
                return
            if wraps is None:
                self._changed(out.sid)              # could not be read: still unconfirmed
            elif out.wrap["id"] in wraps:
                self._done(out, wraps[out.wrap["id"]])
            else:
                self._show(out, Sending.FAILED, cause_words(Code.OFFLINE))

        self._reread(out.sid, read)


def shown_reactions(reactions: Dict[str, str], me: str,
                    waiting: Optional[Outgoing]) -> Tuple[Dict[str, str], bool]:
    """A bubble's reactions (person to emoji) with this person's on its
    way laid over them, and whether that one is still dimmed: the chip
    appears at once, dimmed until the server stored it; one that failed
    is not laid over (the bubble is as it was)."""
    if waiting is None or not waiting.on_its_way:
        return dict(reactions), False
    shown = dict(reactions)
    if waiting.kind == conversation.HINT_RETRACTION:
        shown.pop(me, None)
    else:
        shown[me] = waiting.text
    return shown, True


def footer_words(out: Outgoing) -> Tuple[str, str]:
    """What stands under a bubble on its way (translated): its line, and
    the label of its one action (``""``: none)."""
    if out.state is Sending.FAILED:
        title = _(NOT_SENT) if out.is_message else _(REACTION_NOT_SENT)
        sentence = _(out.sentence) if out.sentence else ""
        action = _("Send Again") if out.is_message else _("Again")
        return (f"{title} {sentence}".strip(), action)
    if out.state is Sending.UNCONFIRMED:
        return _(NOT_CONFIRMED), _("Check Again")
    return _(SENDING), ""


__all__ = ["MESSAGE_EMPTY", "MESSAGE_TOO_LONG", "NOT_CONFIRMED", "NOT_SENT", "Outgoing",
           "REACTION_NOT_SENT", "SENDING", "Sending", "Talk", "failure_sentence",
           "footer_words", "shown_reactions"]
