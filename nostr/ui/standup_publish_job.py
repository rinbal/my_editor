# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The signature and the relays of an article a publish run publishes
(M-4 M13, Q1), moved out of :mod:`.standup_publish`: a
:class:`HeldPublish` waits for :meth:`~HeldPublish.release` between the
signature and the relays, so the run reads the submission again in
between; :class:`SignedHere` signs this one article with the key kept on
this computer, read from the keychain for it and never stored.
"""

from __future__ import annotations

from typing import Callable, Mapping

from i18n import _

from ..outbox.policy import relays_from
from ..publisher import PublishJob
from ..signer_limits import signable, through_signer_app


class HeldPublish(PublishJob):
    """A PublishJob whose signed event waits for :meth:`release` before it
    goes to the relays: the submission is read again in between (12.4)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._held = True

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


__all__ = ["HeldPublish", "SignedHere", "offers_here", "publish_job"]
