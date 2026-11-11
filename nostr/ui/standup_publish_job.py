# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signature and the relays of an article a publish run publishes
(M-4 M13, Q1), moved out of :mod:`.standup_publish`: a
:class:`HeldPublish` waits for :meth:`~HeldPublish.release` between the
signature and the relays, so the run reads the submission again in
between; :class:`SignedHere` signs this one article with the key kept on
this computer, read from the keychain for it and never stored.

:class:`AddressRead` reads the article's address on every relay it goes
to (12.4, Before the writer's work goes out; RG-B5-d): the server's
index may not know a guest's work that the writer's own publication on
another instance released there. What each relay sent and whether it
ended its stored events is :func:`..standup.release.release`'s to weigh.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, QTimer

from i18n import _

from ..outbox.policy import dedupe_relays, relays_from
from ..publish_pipeline import mentioned_pubkeys
from ..publisher import PublishJob
from ..signer_limits import signable, through_signer_app
from ..standup import formats

# Events kept from one relay's read; a relay that sends more is read as
# not finished (it is left out), whatever it sent before counts.
_MOST_EVENTS = 25


class HeldPublish(PublishJob):
    """A PublishJob whose signed event waits for :meth:`release` before it
    goes to the relays: the submission is read again in between (12.4)."""

    def __init__(self, *args, only: Optional[Sequence[str]] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._held = True
        self._only = None if only is None else set(only)

    def _on_plan(self, plan) -> None:
        """Only to the relays the address was read on (:func:`read_relays`)."""
        super()._on_plan(plan)
        if self._only is not None and self._targets is not None:
            self._targets = [url for url in self._targets if url in self._only]

    def release(self) -> None:
        self._held = False
        self._send_when_ready()

    def _send_when_ready(self) -> None:
        if not self._held:
            super()._send_when_ready()


class SignedHere(HeldPublish):
    """Q1: this one article signed with the key kept on this computer.
    The key is read from the keychain into the signer for this signature
    and dropped with the job; the account's way of signing stays as it
    is, and nothing is stored."""

    def __init__(self, *args, secret: bytes, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        from ..local_signer import LocalSigner
        self._here = LocalSigner(secret, parent=self)

    def start(self) -> None:
        self._say(_("Signing on this computer…"))
        self._relay_directory.publish_plan(self._author, self._on_plan,
                                           mentioned=self._mentioned,
                                           entitled=relays_from(self._entitled))
        self._here.sign_event(self._unsigned, on_success=self._on_signed,
                              on_failure=self._fail)


def offers_here(profile, event: Mapping, key_here: Callable[[], bool]) -> bool:
    """Whether signing ``event`` with the key kept here is offered (Q1):
    only through a signer app that cannot sign it, only with the key
    here."""
    return through_signer_app(profile) and not signable(profile, dict(event)) and key_here()


class AddressRead(QObject):
    """One REQ for the writer's articles at ``d`` (kinds 30023, the
    writer as author, ``#d``) on ``relays``; ``on_done(reads)`` once,
    with ``{relay: (complete, events)}``: complete when the relay ended
    its stored events (EOSE), not when it closed the request, could not
    be reached or the time was up."""

    def __init__(self, pool, relays: Sequence[str], *, author: str, d: str,
                 on_done: Callable[[Dict[str, Tuple[bool, List[dict]]]], None],
                 timeout_ms: int = 6_000, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._on_done, self._finished = on_done, False
        self._reads: Dict[str, Tuple[bool, List[dict]]] = {r: (False, []) for r in relays}
        self._ended: set = set()
        if not relays:
            self._sub = None
            QTimer.singleShot(0, self._finish)
            return
        self._sub = pool.subscribe(list(relays), [{"kinds": [formats.ARTICLE_KIND],
                                                   "authors": [author], "#d": [d],
                                                   "limit": _MOST_EVENTS}])
        self.destroyed.connect(self._sub.close)
        self.destroyed.connect(self._sub.deleteLater)
        self._sub.relay_event.connect(self._event)
        self._sub.relay_eose.connect(lambda url: self._end(url, True))
        self._sub.relay_closed.connect(lambda url, _why: self._end(url, False))
        self._sub.relay_failed.connect(lambda url, _why: self._end(url, False))
        QTimer.singleShot(timeout_ms, self, self._finish)

    def _event(self, url: str, event: dict) -> None:
        if self._finished or url not in self._reads or url in self._ended:
            return
        complete, events = self._reads[url]
        if len(events) < _MOST_EVENTS:
            events.append(event)
        else:
            self._end(url, False)

    def _end(self, url: str, complete: bool) -> None:
        if self._finished or url not in self._reads or url in self._ended:
            return
        self._ended.add(url)
        self._reads[url] = (complete, self._reads[url][1])
        if self._ended >= set(self._reads):
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._sub is not None:
            self._sub.close()
        reads, self._reads = self._reads, {}
        try:
            self._on_done(reads)
        finally:
            self.setParent(None)
            self.deleteLater()


def read_relays(*, relay_pool, relay_directory, profile, unsigned_event: Mapping,
                entitled_relays=(), on_done: Callable[[Dict[str, Tuple[bool, List[dict]]]], None],
                parent=None) -> None:
    """The address of ``unsigned_event`` read on every relay it goes to
    (its publish plan, as :class:`HeldPublish` routes it): ``on_done``
    once, with :class:`AddressRead`'s reads. ``parent`` keeps the read
    while it runs."""
    author = str(profile.user_pubkey).lower()
    d = formats.tag_value(dict(unsigned_event), "d") or ""
    relay_directory.publish_plan(
        author, lambda plan: AddressRead(relay_pool, dedupe_relays(plan.targets), author=author,
                                         d=d, on_done=on_done, parent=parent),
        mentioned=mentioned_pubkeys(dict(unsigned_event)), entitled=relays_from(entitled_relays))


def publish_job(*, here: bool, **kwargs):
    """The signature and the relays (HeldPublish); ``here`` signs this one
    article with the key kept on this computer (Q1), read from the
    keychain for it and never stored anywhere else."""
    if here:
        from ..key_vault import KeyVault
        secret = KeyVault().load(kwargs["profile"].user_pubkey)
        if secret is not None:
            return SignedHere(secret=secret, **kwargs)
    return HeldPublish(**kwargs)


__all__ = ["AddressRead", "HeldPublish", "SignedHere", "offers_here", "publish_job",
           "read_relays"]
