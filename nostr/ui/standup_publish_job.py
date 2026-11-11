# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signature and the relays of an article a publish run publishes
(M-4 M13, Q1), moved out of :mod:`.standup_publish`: a
:class:`HeldPublish` waits for :meth:`~HeldPublish.release` between the
signature and the relays, so the run reads the submission again in
between; :class:`SignedHere` signs this one article with the key kept on
this computer, read from the keychain for it and never stored.

:func:`read_relays` reads the article's address on every relay it goes
to, right before it is signed (12.4, Before the writer's work goes out;
RG-B5-d): the server's index may not know a guest's work that the
writer's own publication on another instance released at that address.
An event there that credits someone else sends nothing anywhere; the
article goes only to the relays that answered completely with nothing
in the way, and a relay that did not answer is left out quietly.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Mapping, Optional, Sequence

from i18n import _

from ..outbox.lookup import fetch_replaceable
from ..outbox.policy import dedupe_relays, relays_from
from ..publish_pipeline import mentioned_pubkeys
from ..publisher import PublishJob
from ..signer_limits import signable, through_signer_app
from ..standup import formats, publishing


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


def read_relays(*, relay_pool, relay_directory, profile, unsigned_event: Mapping,
                entitled_relays=(), on_done: Callable[[List[str], bool], None],
                parent=None) -> None:
    """``on_done(clear, someone_elses)``: the relays ``unsigned_event``
    goes to that answered the read of its address completely with nothing
    there but the writer's own earlier version, and whether one holds an
    event there that credits someone else (then nothing goes anywhere).
    ``parent`` keeps the reads while they run."""
    author = str(profile.user_pubkey).lower()
    d = formats.tag_value(dict(unsigned_event), "d") or ""
    address = f"{formats.ARTICLE_KIND}:{author}:{d}"

    def planned(plan) -> None:
        relays = dedupe_relays(plan.targets)
        found: Dict[str, Optional[bool]] = {}

        def answered(relay: str, lookup) -> None:
            held = lookup.event
            found[relay] = None if relay not in lookup.answered else (
                held is None or publishing.writer_may_release(
                    publishing.held_credit(held, address), address_taken=False, writer=author))
            if len(found) == len(relays):
                on_done([r for r in relays if found.get(r)],
                        any(ok is False for ok in found.values()))

        if not relays:
            on_done([], False)
        for relay in relays:
            fetch_replaceable(relay_pool, [relay], kind=formats.ARTICLE_KIND, author=author,
                              d_tag=d, parent=parent,
                              on_done=lambda lookup, relay=relay: answered(relay, lookup))

    relay_directory.publish_plan(author, planned,
                                 mentioned=mentioned_pubkeys(dict(unsigned_event)),
                                 entitled=relays_from(entitled_relays))


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


__all__ = ["HeldPublish", "SignedHere", "offers_here", "publish_job", "read_relays"]
