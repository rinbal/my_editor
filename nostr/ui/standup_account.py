# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What belongs to one account that submits to STANDUP
(:mod:`.standup_submit`): the client for its server and those of other
servers its list names, its submissions list and read state, the media
keys, the publications' keys, its signer, and its submissions on their
way.

:meth:`Account.read_whole` is the one read of the list of submissions:
read again quietly after a read that missed a page (lessons 36), and
one at a time, so the sheet, a round and Article Details asking at once
hear the same read and the signer is asked for each page once
(RG-M2r3 13). :func:`live` keeps a late answer from an account let go.
"""

from __future__ import annotations

import functools
import time
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject

from .. import failures
from ..einundzwanzig_api.client import session_signer
from ..list_relays import ListQuery, publish_once
from ..standup import trust
from ..standup.client import StandupApi
from ..standup.lists import ReadState, SubmissionsList
from ..standup.opening import MediaKeys
from .standup_run import SubmissionRun


class PoolSigner:
    """The account's signer for sealing a message (conversation.py) and
    for opening a contributor's key wrap (opening.py)."""

    def __init__(self, session_pool, profile) -> None:
        self._pool, self._profile = session_pool, profile
        self._sign = session_signer(session_pool, profile)

    def sign_event(self, unsigned: dict, *, on_success, on_failure) -> None:
        self._sign(unsigned, on_success, on_failure)

    def nip44_decrypt(self, peer: str, ciphertext: str, *, on_success, on_failure) -> None:
        self._pool.get(self._profile, lambda client: client.nip44_decrypt(
            peer, ciphertext, on_success, on_failure), on_failure)


class Account(QObject):
    """What belongs to one account. ``later(ms, callback)`` waits between
    the quiet reads of the list."""

    def __init__(self, profile, *, session_pool, relay_pool, relay_directory, server: str,
                 entitled_relays, nam, later: Callable[[int, Callable[[], None]], None],
                 parent: QObject) -> None:
        super().__init__(parent)
        self.profile = profile
        self.writer = profile.user_pubkey
        self._sign = session_signer(session_pool, profile)
        self._nam = nam
        self._later = later
        self.api = StandupApi(self._sign, server=server, nam=nam, parent=self)
        # Clients of other servers an entry of the list names, made when
        # one is asked about (RG-M2r 9), let go with the account.
        self.clients: Dict[str, StandupApi] = {}
        lists = dict(session_pool=session_pool, relay_directory=relay_directory,
                     query=ListQuery(relay_pool, parent=self),
                     publisher=functools.partial(publish_once, relay_pool), clock=time.time,
                     entitled_relays=entitled_relays, parent=self)
        self.submissions = SubmissionsList(profile, **lists)
        self.read_state = ReadState(profile, **lists)
        self.media_keys = MediaKeys()
        self.epochs = trust.EpochStore()
        self.relay_lookup = trust.relay_key_lookup(relay_pool, relay_directory, parent=self)
        self.destinations = trust.Destinations(self.api, self.relay_lookup, self.epochs,
                                               parent=self)
        self.signer = PoolSigner(session_pool, profile)
        self.runs: List[SubmissionRun] = []
        # Why the list of submissions could not be read, once read for a
        # sheet ("" when it could): said again without asking again.
        self.list_problem = ""
        # Let go (StandupWriter.stop): no answer that comes later reaches
        # its client, its lists or its signer again (lesson 31, live).
        self.stopped = False
        # Try Again asked for the list to be read again (reload).
        self.read_again = False
        # Who waits for the read of the list on its way (read_whole), or
        # None while none is.
        self._reading: Optional[List[Callable]] = None
        # The list is being read for Article Details' link name, or that
        # read could not tell (RG-M2r2a 1): read-only meanwhile.
        self.checking = False
        self.check_failed = False

    def client_for(self, server: str) -> StandupApi:
        """The client of ``server``: the account's own, or, for an entry
        of another server, one of its own, asked about that entry only
        (an entry asked of another server reads as never stored)."""
        if server == self.api.server:
            return self.api
        client = self.clients.get(server)
        if client is None:
            client = self.clients[server] = StandupApi(self._sign, server=server,
                                                       nam=self._nam, parent=self)
        return client

    def read_whole(self, on_done: Callable) -> None:
        """The list of submissions read, again quietly after a read that
        missed a page, until whole or the tries are spent; ``on_done``
        hears that read only, never one per try (lessons 36). One read at
        a time: who asks while one is on its way (Article Details, the
        sheet, a round) hears that one (RG-M2r3 13)."""
        if self._reading is not None:
            self._reading.append(on_done)
            return
        self._reading = [on_done]

        def done(result) -> None:
            waiting, self._reading = self._reading or [], None
            for heard in waiting:
                heard(result)

        failures.retry_quietly(
            lambda answer: self.submissions.load(live(self, answer)),
            settled=lambda result: not result.readable or self.submissions.complete,
            on_done=done, later=self._later)


def live(account: Account, callback: Callable) -> Callable:
    """``callback``, answered only while ``account`` is not let go: a late
    answer (a signer app, a relay, the server) never opens a session,
    posts, signs or reads for it, and never touches an object deleted
    with it (RG-M2r 3)."""
    def guarded(*args) -> None:
        if not account.stopped:
            callback(*args)
    return guarded


__all__ = ["Account", "PoolSigner", "live"]
