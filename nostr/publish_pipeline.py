# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One signed event out to the relays: the steps every publish shares.

Publishing a note or an article (publisher.PublishJob) and asking relays
to delete one (deletion.DeletionJob) are the same pipeline with another
event and other words, so it is written once:

  1. The routing (RelayDirectory.publish_plan: the author's write relays,
     a membership's relay, the read relays of the people the event
     mentions) and the signer (through the session pool) are asked for at
     the same time; while the person approves on their phone, the lookups
     land.
  2. The signer signs the event, and what it returns must be exactly the
     event asked for, validly signed (outbox.writer.signed_matches). A
     signer that answers with another event never gets it published.
  3. Once both are in, the event goes to ``extra_relays`` (where it is
     known to belong, kept first) and to the plan's relays, and each
     relay's answer is reported.
  4. ``send_again()`` sends the same signed event once more (Try Again),
     with no second signer prompt.

Signals, in firing order:
  status_changed(str)            progress, in words
  relay_result(str, bool, str)   each relay's answer (url, ok, message)
  first_accept(str)              the first relay that took it, once per sending
  completed(list)                every answer [(url, ok, message)], also when
                                 no relay took it
  failed(str)                    the signer did not sign, or signed something
                                 else; terminal. The reason is for Details
                                 and the log; ``failure_words`` is the one
                                 sentence to show (lessons 36)

``cancel()`` silences the pipeline: nothing is sent after it, and a
signature that arrives later is not used. Once the event has gone out,
cancelling only silences it.

A subclass gives the words (``_waiting_words``, ``_sending_words``,
``_accepted_words``, ``_done_words``) and may act on the signed event
(``_signed``) and on the relays' answers (``_answered``).
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer, Signal

from i18n import _

from . import failures
from .bunker import signer_failure_words
from .outbox.policy import dedupe_relays, relays_from
from .outbox.writer import signed_matches
from .signer_limits import size_problem

PublishResult = Tuple[str, bool, str]   # (relay_url, ok, message)
Mention = Tuple[str, str]               # (pubkey_hex, relay_hint), hint may be empty

_PUBKEY_HEX = re.compile(r"[0-9a-f]{64}")


def not_taken_words(results: Sequence[PublishResult]) -> str:
    """The one sentence (translated) for an event no relay took: turned
    down by one that answered, or none reached (lessons 36: a relay that
    answered is not blamed on the connection)."""
    turned_down = any(not ok and failures.refused_by_relay(message)
                      for _url, ok, message in results)
    return _(failures.TURNED_DOWN if turned_down else failures.CHECK_CONNECTION)


def same_event(first: dict, second: dict) -> bool:
    """Whether two unsigned events say the same, whenever each was made:
    Try Again may then send the one signed before."""
    def said(event: dict) -> dict:
        return {key: value for key, value in event.items()
                if key not in ("created_at", "id", "sig")}
    return said(first) == said(second)


def answers_detail(results: Sequence[PublishResult]) -> str:
    """Each relay's answer, one per line, for Details and the tooltip."""
    return "\n".join(f"{url}: {message or _('no answer')}" for url, _ok, message in results)


def kept_by_relay(ok: bool, message: str) -> bool:
    """Whether a relay holds the event after it was sent there: it took
    it, had it already ("duplicate:"), or holds a newer version of it
    ("replaced:", as strfry and rnostr answer an older version of an
    addressable event)."""
    return ok or failures.answer_prefix(message) in ("duplicate", "replaced")


def mentioned_pubkeys(event: dict) -> List[Mention]:
    """``(pubkey, relay hint)`` for everyone an event mentions (its ``p``
    tags), in tag order, once each; the hint is "" when the tag has none."""
    out: List[Mention] = []
    seen: set = set()
    for tag in event.get("tags", []):
        if not isinstance(tag, list) or len(tag) < 2 or tag[0] != "p":
            continue
        pubkey = str(tag[1]).lower()
        if pubkey in seen or not _PUBKEY_HEX.fullmatch(pubkey):
            continue
        seen.add(pubkey)
        hint = tag[2] if len(tag) >= 3 and isinstance(tag[2], str) else ""
        out.append((pubkey, hint))
    return out


class PublishPipeline(QObject):
    """One signed event out to the relays (see the module's text)."""

    status_changed = Signal(str)
    relay_result = Signal(str, bool, str)
    first_accept = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, *, relay_pool, relay_directory, session_pool, profile,
                 unsigned_event: dict, entitled_relays=(), extra_relays: Sequence[str] = (),
                 mentioned: Optional[Sequence[Mention]] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        author = str(profile.user_pubkey).lower()
        if str(unsigned_event.get("pubkey", "")).lower() != author:
            raise ValueError("unsigned event pubkey does not match the publishing profile")
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        self._profile = profile
        self._author = author
        self._unsigned = unsigned_event
        # Relays this account has standing on beyond its own list (a
        # list, or a callable answering one when asked).
        self._entitled = entitled_relays
        self._extra_relays = list(extra_relays)
        self._mentioned = (list(mentioned) if mentioned is not None
                           else mentioned_pubkeys(unsigned_event))
        self._plan = None
        self._signed_event: Optional[dict] = None
        self._targets: Optional[List[str]] = None
        self._sent = False
        self._sending = False
        self._failed = False
        self._cancelled = False
        self._failure_words = ""

    # -- public API --------------------------------------------------------

    @property
    def signed_event(self) -> Optional[dict]:
        """The event as the signer signed it, once it has."""
        return self._signed_event

    @property
    def unsigned_event(self) -> dict:
        """The event asked to be signed."""
        return self._unsigned

    @property
    def failure_words(self) -> str:
        """Once ``failed`` fired: the one sentence to show, its likely
        cause and what to do, never the reason itself."""
        return self._failure_words

    @property
    def plan(self):
        """Where the event goes (a PublishPlan), once the routing is in."""
        return self._plan

    def start(self) -> None:
        """Ask for the routing and the signature. Once per instance. An
        event the account's signer app could not sign and answer fails
        at once, before anything is asked, with words that say so
        (``signer_limits.size_problem``), on the next turn as a signer's
        refusal would."""
        too_long = size_problem(
            self._profile, event=self._unsigned,
            key_here=lambda: self._session_pool.key_here(self._author))
        if too_long:
            QTimer.singleShot(0, self, lambda: self._fail(too_long, words=too_long))
            return
        self._say(_("Connecting to your signer app…"))
        self._relay_directory.publish_plan(self._author, self._on_plan,
                                           mentioned=self._mentioned,
                                           entitled=relays_from(self._entitled))
        self._session_pool.get(self._profile, on_ready=self._on_signer,
                               on_error=self._fail)

    def send_again(self) -> bool:
        """Send the signed event once more, to the same relays. False when
        nothing was signed yet (start a new pipeline instead), or when the
        relays have not all answered the last sending."""
        if (self._signed_event is None or self._targets is None or self._cancelled
                or self._sending):
            return False
        self._send()
        return True

    def cancel(self) -> None:
        """Send nothing more and say nothing more."""
        self._cancelled = True

    # -- what a subclass says and does ---------------------------------------

    def _waiting_words(self) -> str:
        return _("Waiting for your signer app. Approve the request there…")

    def _sending_words(self, count: int) -> str:
        """Said while it goes out; never how many relays, or which
        (lessons 26 and 36)."""
        return _("Publishing…")

    def _accepted_words(self, url: str) -> str:
        """Said when the first relay took it; "" says nothing."""
        return ""

    def _done_words(self, accepted: int, total: int) -> str:
        """Said once every relay answered; "" says nothing."""
        return ""

    def _signed(self, event: dict) -> None:
        """The signer signed exactly the event asked for."""

    def _answered(self, results: List[PublishResult]) -> None:
        """Every relay answered a sending; runs even when cancelled, since
        the event went out anyway."""

    # -- the pipeline --------------------------------------------------------

    def _say(self, text: str) -> None:
        if text and not self._cancelled:
            self.status_changed.emit(text)

    def _on_plan(self, plan) -> None:
        self._plan = plan
        self._targets = dedupe_relays(self._extra_relays, plan.targets)
        self._send_when_ready()

    def _on_signer(self, client) -> None:
        if self._failed or self._cancelled:
            return
        # Asked again: what the connection's relays carry is known once
        # the signer is (the session pool reads it as it reattaches), and
        # the client would refuse in its own, technical words.
        too_long = size_problem(
            self._profile, event=self._unsigned,
            key_here=lambda: self._session_pool.key_here(self._author))
        if too_long:
            self._fail(too_long, words=too_long)
            return
        self._say(self._waiting_words())
        client.sign_event(self._unsigned, on_success=self._on_signed,
                          on_failure=self._fail)

    def _on_signed(self, event: dict) -> None:
        if self._failed or self._cancelled:
            return
        if not signed_matches(event, self._unsigned, self._author):
            self._fail("signed event does not match the request",
                       words=_("Your signer app signed something other than what was "
                               "asked: nothing was sent, so try again."))
            return
        self._signed_event = event
        self._signed(event)
        if self._plan is None:
            self._say(_("Publishing…"))
        self._send_when_ready()

    def _send_when_ready(self) -> None:
        if (self._sent or self._failed or self._cancelled or self._targets is None
                or self._signed_event is None):
            return
        self._sent = True
        self._send()

    def _send(self) -> None:
        self._sending = True
        self._say(self._sending_words(len(self._targets)))
        job = self._relay_pool.publish(self._targets, self._signed_event)
        job.relay_result.connect(self._on_relay_result)
        job.first_accept.connect(self._on_first_accept)
        job.all_done.connect(self._on_all_done)

    def _on_relay_result(self, url: str, ok: bool, message: str) -> None:
        if not self._cancelled:
            self.relay_result.emit(url, ok, message)

    def _on_first_accept(self, url: str) -> None:
        if self._cancelled:
            return
        self._say(self._accepted_words(url))
        self.first_accept.emit(url)

    def _on_all_done(self, results: List[PublishResult]) -> None:
        self._sending = False
        self._answered(list(results))
        if self._cancelled:
            return
        accepted = sum(1 for _url, ok, _message in results if ok)
        self._say(self._done_words(accepted, len(results)))
        self.completed.emit(list(results))

    def _fail(self, reason: str, *, words: str = "") -> None:
        """Nothing went out: ``reason`` for Details and the log, ``words``
        the sentence to show (the signer's reason in plain words when not
        given)."""
        if self._failed or self._sent or self._cancelled:
            return
        self._failed = True
        reason = reason or "the signer app gave no reason"
        self._failure_words = words or signer_failure_words(reason)
        self.failed.emit(reason)
