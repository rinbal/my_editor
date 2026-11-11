# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Veröffentlichen: the writer publishes an accepted submission under
their own key (plan 8.2, 8.5; editorial contract 7, 11.2 step
``writer_publishes``, 12.4 ``/published``; M-4 M13).

One :class:`AcceptedPublish` per submission, held by the account's
Following (so closing the sheet never stops it, and it goes with the
account). It acts only on the verified newest accept, for the revision
that accept names, which must be the verified revision opened here:

  1. The work's encrypted pictures are listed (``publishing``), and the
     article with their addresses as long as they will be is measured
     against the signer first: nothing private goes up in plain for an
     article the signer could not sign.
  2. Each picture is read (this computer's copy, else the servers its
     opened revision names, then the writer's), decrypted with the key
     its submission's signed revision names, checked against its plain
     hash and put on the writer's media servers. A file the library held
     before is named as it is and never taken down; every file this run
     put up comes down again on any failure before a relay took it.
  3. The article is built with the public addresses (credits, the
     writer's own split, carried tags as the web app does), measured
     again, signed and kept bound to the revision. Before a picture goes
     up and right before signing, the address is read on this server and
     every relay it goes to, the submission again before it goes out
     (12.4; ``release``): changed, or another person's article there,
     and nothing goes out; it goes only to the relays that answered.
  4. Only once a relay took it is it reported (``/published``); Erneut
     sends the same signed article again. Once a relay took it, it is
     public: a report that did not arrive is made again quietly
     (:mod:`.standup_unlisted`, also after a restart), and the run, the
     sheet and the detail say it is public (RG-M4-b 3).

Through a signer app that cannot sign an article this long, with the
key also kept on this computer, :attr:`Stage.OFFER_HERE` offers once
to sign this one article with that key (M-4 Q1), read from the keychain
for the signature, wiped after it and never stored.

This module is longer than most: the run is one state machine whose four
steps share its state; its words, its job and the release rule live in
their own modules already.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from i18n import N_, _

from .. import bech32
from ..publish_pipeline import not_taken_words
from ..publisher import published_at_of
from ..signer_limits import size_problem
from ..standup import formats, opening, publishing
from ..standup.errors import Code, cause_words
from ..standup import following as rules
from ..standup.following import correction_taken
from ..standup.publishing import Accepted
from .standup_account import live
from .standup_publish_job import HeldPublish, SignedHere, offers_here, publish_job, read_relays
from .standup_publish_words import (
    BUSY, BUSY_DETAIL, BUSY_SIGNING, CLEANUP_ASKS, DONE, LINK_RELAYS, NOT_CHECKED,
    NOT_PUBLISHABLE, NOT_PUBLISHED, NOT_THE_SAME, PICTURE_NOT_FOUND, PICTURE_NOT_UP,
    SOMEONE_ELSES, TOO_LONG_ELSEWHERE, TOO_LONG_HERE, Stage,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishPorts:
    """What a run uses, each lent by the account (seams for the tests).

    - ``api``: the submission's StandupClient; ``media_keys``: the
      account's MediaKeys; ``store``: the Media Library's MediaStore.
    - ``fetch(url, on_success, on_failure)``: one address's bytes.
    - ``job(profile, unsigned, here, only)``: a :class:`HeldPublish`
      (``here``: signed with the key kept on this computer, Q1) that goes
      ``only`` to those relays.
    - ``relays_hold(unsigned, on_done)``: :func:`read_relays` for it
      (``{relay: (complete, events)}``).
    - ``key_here()``: whether the account's key is kept on this computer.
    - ``serve(view)``: the server's newer View, for the list and the detail.
    - ``reread(on_detail, on_failure)``: the submission read now
      (``answers.SubmissionDetail``: its View and ``address_taken``).
    - ``held_at(address, on_author, on_failure)``: whom the article the
      server holds at ``address`` credits (None: none there).
    - ``clock()``: seconds.
    - ``confirm(confirms, revision_hash, on_done)``: ``/confirmation``
      (a correction's run confirms it first).
    """

    api: object
    media_keys: object
    store: object
    fetch: Callable
    job: Callable
    key_here: Callable[[], bool]
    serve: Callable
    reread: Callable
    held_at: Callable
    relays_hold: Callable
    clock: Callable[[], float] = time.time
    confirm: Optional[Callable] = None
    # After it is published (M14): ``cleanup(sid, on_asking)``.
    cleanup: Optional[Callable[[str, Callable[[], None]], None]] = None
    # The account's QuietReport: a public work whose report did not arrive.
    unlisted: Optional[object] = None


def run_for(following, account, profile, sid: str, *, store, fetch, session_pool, relay_pool,
            relay_directory, entitled_relays=None, parent=None,
            cleanup=None) -> Optional["AcceptedPublish"]:
    """The one publish run of ``sid``, made when the verified newest
    accept names the verified revision opened here and the writer signs
    (or, for a published work, when that revision is a verified
    correction and the article as it stands in public is known: a
    :class:`CorrectionPublish`); None while it does not (the detail
    offers the action only then). A run of the same revision goes on
    (its signed article is kept); one of another revision is let go."""
    row = following.row(sid)
    view = row.view if row is not None else None
    version = following.reading.latest(sid)
    run = following.publishing.get(sid)
    if run is not None and view is not None and run.revision_hash == view.revision_hash:
        return run
    if (view is None or version is None or version.opened is None
            or view.signer != "writer" or view.writer != profile.user_pubkey
            or version.revision != view.revision or version.revision_hash != view.revision_hash):
        return None
    correction = version.opened.verified.correction
    if correction:
        published = following.reading.published_event(sid)
        accepted = Accepted(revision=view.revision, split_rumor_id=None, actor="", seq=0)
        if view.state != "published" or published is None:
            return None
    else:
        published, accepted = None, following.reading.accepted(sid)
        if accepted is None or accepted.revision != view.revision:
            return None
    if run is not None:
        run.stop()
    api = following.api_for(sid)

    def reread(on_detail, on_failure) -> None:
        api.submission(sid, live(account, on_detail), live(account, on_failure))

    def held_at(address: str, on_author, on_failure) -> None:
        api.article_author(address, live(account, on_author), live(account, on_failure))

    def job(profile, unsigned: dict, here: bool, only):
        return publish_job(here=here, relay_pool=relay_pool, relay_directory=relay_directory,
                           session_pool=session_pool, profile=profile, unsigned_event=unsigned,
                           entitled_relays=entitled_relays or (), only=only)

    def relays_hold(unsigned: dict, on_done) -> None:
        read_relays(relay_pool=relay_pool, relay_directory=relay_directory, profile=profile,
                    unsigned_event=unsigned, entitled_relays=entitled_relays or (),
                    on_done=live(account, on_done), parent=parent)

    def confirm(confirms, revision_hash: str, on_done) -> None:
        following.confirm(sid, confirms=confirms, revision_hash=revision_hash,
                          split_rumor_id=None, on_done=on_done)

    ports = PublishPorts(api=api, media_keys=account.media_keys, store=store, fetch=fetch,
                         job=job, serve=following.serve, reread=reread, held_at=held_at,
                         relays_hold=relays_hold,
                         confirm=confirm,
                         key_here=lambda: session_pool.key_here(profile.user_pubkey),
                         cleanup=cleanup, unlisted=following.unlisted)
    made = dict(sid=sid, profile=profile, version=version, accepted=accepted,
                revision_hash=view.revision_hash,
                publication=following.publication_name(view.publication), ports=ports,
                parent=following)
    run = (CorrectionPublish(published=published, taken=correction_taken(view), **made)
           if correction else AcceptedPublish(**made))
    following.track(sid, run)
    return run


class AcceptedPublish(QObject):
    """See the module docstring. ``version`` is the verified revision
    opened here (``standup_reading.Version``), ``accepted`` the verified
    accept. Where the encrypted files lie is read only from the opened
    revision and the writer's media servers, never the server's record."""

    changed = Signal()

    def __init__(self, *, sid: str, profile, version, accepted: Accepted, revision_hash: str,
                 publication: str, ports: PublishPorts,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.sid, self._profile, self._version = sid, profile, version
        self._accepted, self.revision_hash = accepted, revision_hash
        self._publication, self._ports = publication, ports
        self.writer = str(profile.user_pubkey)
        self.stage = Stage.READY
        # What the submission's state must still be before the relays.
        self.expected_state = "accepted"
        self.title, self.words, self.naddr = "", "", ""
        # Signed, bound to ``revision_hash`` (never named ``event``: QObject's).
        self.signed_article: Optional[dict] = None
        self._job = None
        self._urls: Dict[str, str] = {}
        self._created: List[Tuple[str, Tuple[str, ...]]] = []   # (plain sha256, servers)
        self._waiting: Dict[str, Tuple[Callable, Callable]] = {}
        self._stopped = False
        self._here = False
        self._listening = False
        self._kept_public()

    def _kept_public(self) -> None:
        """A run made for a work kept as public (after a restart, RG-M4-c
        3) starts there: never "Bisher privat …" for a public text."""
        unlisted = self._ports.unlisted
        state = unlisted.state(self.sid) if unlisted is not None else ""
        entry = unlisted.record.entry(self.sid) if state else None
        if entry is None or int(entry.get("revision", 0)) != self._version.revision:
            return
        self.signed_article, self.naddr = dict(entry["event"]), str(entry["naddr"])
        self.stage = Stage.NOT_LISTED if state == rules.NOT_LISTED else Stage.PUBLIC
        self.words = self._public_words(state)

    def _public_words(self, state: str) -> str:
        title = str((self.revision.get("work") or {}).get("title") or "")
        return _(rules.PUBLISHING_WORDS[state][1]).format(title=title,
                                                          publication=self._publication)

    # -- what the sheet calls ---------------------------------------------------

    @property
    def revision(self) -> Mapping:
        return self._version.opened.revision

    @property
    def going(self) -> str:
        """How it stands for the list, the pill and the detail, with the
        sheet closed too (RG-M4-b 3, 9): on its way, public, or "". Once
        public, the record says how (RG-M4-c 3): whether a quiet try is
        left, and nothing once the work is listed."""
        if self.stage in (Stage.PUBLIC, Stage.NOT_LISTED) and self._ports.unlisted is not None:
            return self._ports.unlisted.state(self.sid)
        return {Stage.WORKING: rules.PUBLISHING, Stage.PUBLIC: rules.PUBLIC,
                Stage.NOT_LISTED: rules.NOT_LISTED}.get(self.stage, "")

    def start(self, *, here: bool = False) -> None:
        """Veröffentlichen (or, ``here``, Mit dem Schlüssel auf diesem
        Computer signieren). Once at a time."""
        if self.stage in (Stage.WORKING, Stage.DONE, Stage.NOT_STORED, Stage.PUBLIC,
                          Stage.NOT_LISTED, Stage.SOMEONE_ELSES, Stage.CHANGED):
            return
        self._here = here
        self._urls, self._created = {}, []
        if not self._listening:
            self._listening = True
            self._ports.store.upload_finished.connect(self._uploaded)
            self._ports.store.upload_failed.connect(self._not_uploaded)
        self._say(Stage.WORKING, _(BUSY), _(BUSY_DETAIL))
        try:
            self._entries = publishing.media_to_publish(self.revision)
            before = self._template(publishing.addresses_before(
                self._entries, self._ports.store.target_servers()))
        except publishing.NotPublishable as error:
            _log.info("not publishable: %s", error)
            self._fail(_(NOT_PUBLISHABLE))
            return
        if not here and self._too_long(before):
            return
        if self._entries:               # nothing in plain before the address is read
            self._guard(None, before, lambda _relays: self._next_picture())
        else:
            self._next_picture()

    def retry(self) -> None:
        """Erneut: the same signed article to the relays again, or (after a
        failure) the whole step anew. A public work's report is made again
        quietly, never by hand (RG-M4-b 3)."""
        if self.stage is Stage.NOT_STORED and self._job is not None:
            self._say(Stage.WORKING, _(BUSY), "")
            if not self._job.send_again():
                self._say(Stage.NOT_STORED, _(NOT_PUBLISHED), self.words)
        elif self.stage in (Stage.FAILED, Stage.NOT_CHECKED, Stage.READY):
            self.start()

    def dismiss(self) -> None:
        """Abbrechen when the address could not be read: back at the start
        (what was signed and is not public is dropped already)."""
        if self.stage is Stage.NOT_CHECKED:
            self._say(Stage.READY, "", "")

    def stop(self) -> None:
        """The account was let go: nothing more is asked; what this run put
        up and nothing took yet comes down, and the job goes."""
        if self._stopped:
            return
        self._stopped = True
        if self.stage is Stage.WORKING and self.signed_article is None:
            self._take_down()
        self._let_go()

    def _let_go(self) -> None:
        """The job goes, with the key kept here and the article (RG-M4-a 4)."""
        job, self._job = self._job, None
        if job is not None:
            job.cancel()
            job.deleteLater()

    # -- 1: before anything goes up ----------------------------------------------

    def _template(self, urls: Mapping[str, str]) -> dict:
        now = int(self._ports.clock())
        return publishing.published_template(
            self.revision, writer=self.writer, d=self.d, media_urls=urls,
            published_at=self.published_at or now, created_at=now)

    @property
    def d(self) -> str:
        """The link name it is published under: the work's own, which its
        submission was made with (the server checks the coordinate)."""
        return str(self.revision["work"]["d"])

    @property
    def address(self) -> str:
        return f"{formats.ARTICLE_KIND}:{self.writer}:{self.d}"

    @property
    def published_at(self) -> Optional[int]:
        """A first publication's date is its own."""
        return None

    def _too_long(self, event: dict) -> bool:
        """Said, and true, when the signer could not sign ``event``."""
        if not size_problem(self._profile, event=event, key_here=self._ports.key_here):
            return False
        self._say_too_long(event)
        return True

    def _say_too_long(self, event: dict) -> None:
        """Q1 in the web's words (N1; RG-M4-b 4): the text is the accepted
        version's, so never "shorten it"; the key here is the one action,
        else a question to the Redaktion and no Erneut."""
        if offers_here(self._profile, event, self._ports.key_here):
            self._say(Stage.OFFER_HERE, _(NOT_PUBLISHED), _(TOO_LONG_HERE))
        else:
            self._fail(_(TOO_LONG_ELSEWHERE), Stage.TOO_LONG)

    # -- 2: the pictures ------------------------------------------------------------

    def _next_picture(self) -> None:
        if self._stopped:
            return
        left = [e for e in self._entries if e["ref"] not in self._urls]
        if not left:
            self._sign()
            return
        entry = left[0]
        self._cipher(entry, lambda data: self._decrypted(entry, data))

    def _servers_of(self, entry: Mapping) -> List[str]:
        servers = list(entry.get("servers") or ()) + list(self._ports.store.target_servers())
        return list(dict.fromkeys(s.rstrip("/") for s in servers
                                  if isinstance(s, str) and s.startswith("https://")))

    def _cipher(self, entry: Mapping, on_data: Callable[[bytes], None]) -> None:
        cached = self._ports.store.cached_bytes(entry["cipher_sha256"])
        if cached is not None and formats.sha256_hex(cached) == entry["cipher_sha256"]:
            on_data(cached)
            return
        servers = self._servers_of(entry)

        def ask(index: int) -> None:
            if self._stopped:
                return
            if index >= len(servers):
                self._fail(_(PICTURE_NOT_FOUND))
                return
            url = f"{servers[index]}/{entry['cipher_sha256']}"

            def got(data: bytes) -> None:
                if formats.sha256_hex(data) == entry["cipher_sha256"]:
                    on_data(data)
                else:
                    ask(index + 1)

            self._ports.fetch(url, on_success=got, on_failure=lambda _why: ask(index + 1))

        ask(0)

    def _decrypted(self, entry: Mapping, data: bytes) -> None:
        if self._stopped:
            return
        try:
            plain = self._ports.media_keys.decrypt(self.sid, data, entry["cipher_sha256"])
        except opening.NotConfirmed:
            self._fail(_(PICTURE_NOT_FOUND))
            return
        if formats.sha256_hex(plain) != entry["plain_sha256"]:
            self._fail(_(PICTURE_NOT_FOUND))
            return
        store, sha = self._ports.store, entry["plain_sha256"]
        held = store.files().get(sha)
        if held is not None and getattr(held, "url", ""):
            # Held before this run (another work's picture, perhaps public):
            # named as it is, never this run's to take down.
            self._urls[entry["ref"]] = held.url
            self._next_picture()
            return
        name = f"{sha}.{publishing.EXTENSIONS[entry['mime']]}"
        self._waiting[name] = (lambda media: self._up(entry, media), lambda: self._fail(
            _(PICTURE_NOT_UP)))
        store.upload_bytes(plain, name=name, mime_type=entry["mime"])

    def _uploaded(self, name: str, media) -> None:
        waiting = self._waiting.pop(name, None)
        if waiting is not None and not self._stopped:
            waiting[0](media)

    def _not_uploaded(self, name: str, _reason: str) -> None:
        waiting = self._waiting.pop(name, None)
        if waiting is not None and not self._stopped:
            waiting[1]()

    def _up(self, entry: Mapping, media) -> None:
        servers = tuple(u.get("server", "") for u in getattr(media, "urls", ()) if u.get("server"))
        if media.hash == entry["plain_sha256"]:
            self._created.append((media.hash, servers))
        if media.hash != entry["plain_sha256"] or not str(media.url).startswith("https://"):
            self._fail(_(PICTURE_NOT_UP))
            return
        self._urls[entry["ref"]] = media.url
        self._next_picture()

    def _take_down(self) -> None:
        created, self._created = self._created, []
        for sha, servers in created:
            self._ports.store.delete_file(sha, servers=servers)

    # -- 3: signed, read again, out ----------------------------------------------------

    def _sign(self) -> None:
        try:
            unsigned = self._template(self._urls)
        except publishing.NotPublishable as error:
            _log.info("not publishable: %s", error)
            self._fail(_(NOT_PUBLISHABLE))
            return
        if not self._here and self._too_long(unsigned):
            self._take_down()
            return
        self._say(Stage.WORKING, _(BUSY), _(BUSY_SIGNING))
        self._guard(None, unsigned, lambda relays: self._sign_now(unsigned, relays))

    def _sign_now(self, unsigned: dict, relays) -> None:
        job = self._job = self._ports.job(self._profile, unsigned, self._here, list(relays))
        job.setParent(self)                     # it goes with the run (RG-M4-a 4)
        job.signed.connect(lambda _id: self._signed(job))
        job.completed.connect(lambda results: self._sent(job, results))
        job.failed.connect(lambda _reason: self._not_signed(job))
        job.start()

    def _not_signed(self, job) -> None:
        if job is not self._job or self._stopped:
            return
        self._take_down()
        if not self._here and size_problem(self._profile, event=job.unsigned_event,
                                           key_here=self._ports.key_here):
            # The refused job goes first: signing with the key here is a
            # new job, which the guard only follows as the run's (RG-M4-c 1).
            self._let_go()
            self._say_too_long(job.unsigned_event)
        else:
            self._fail(job.failure_words)

    def _signed(self, job) -> None:
        if job is not self._job or self._stopped:
            return
        self.signed_article = job.signed_event           # kept with its revision (12.4)
        self._guard(job, None, lambda _relays: job.release())

    def _guard(self, job, unsigned: Optional[dict], then: Callable[[tuple], None]) -> None:
        """The address read before anything goes out (12.4): the submission
        (still the one accepted here), this server and, for ``unsigned``,
        every relay; ``then(relays)`` with those it may go to."""
        def live_now() -> bool:
            return not self._stopped and job is self._job and self.stage is Stage.WORKING

        def decide(taken: bool, read: bool, held: Optional[str], relays) -> None:
            if not live_now():
                return
            found = publishing.release(writer=self.writer, address=self.address,
                                       address_taken=taken, server_read=read, held=held,
                                       relays=relays)
            if found.someone_elses:
                self._fail(_(SOMEONE_ELSES), Stage.SOMEONE_ELSES)
            elif not read or (unsigned is not None and not found.send_to):
                self._fail(_(NOT_CHECKED), Stage.NOT_CHECKED)
            else:
                then(found.send_to)

        def on_relays(taken: bool, read: bool, held: Optional[str]) -> None:
            if unsigned is None or not live_now():
                decide(taken, read, held, {})
            else:
                self._ports.relays_hold(unsigned, lambda relays: decide(taken, read, held,
                                                                        relays))

        def got(detail) -> None:
            if not live_now():
                return
            view = detail.submission
            self._ports.serve(view)
            if view.revision_hash != self.revision_hash or view.state != self.expected_state \
                    or view.revision != self._accepted.revision:
                self._fail(_(NOT_THE_SAME), Stage.CHANGED)
                return
            self._ports.held_at(self.address,
                                lambda held: on_relays(detail.address_taken, True, held),
                                lambda _error: on_relays(detail.address_taken, False, None))

        self._ports.reread(got, lambda _error: live_now() and on_relays(False, False, None))

    def _sent(self, job, results) -> None:
        if job is not self._job or self._stopped:
            return
        took = [url for url, ok, _message in results if ok]
        if not took:
            self._say(Stage.NOT_STORED, _(NOT_PUBLISHED), not_taken_words(results))
            return
        self.naddr = bech32.encode_naddr(self.d, self.writer, 30023, took[:LINK_RELAYS])
        self._report()

    # -- 4: reported -----------------------------------------------------------------------

    def _report(self) -> None:
        self._say(Stage.WORKING, _(BUSY), "")
        event = self.signed_article

        def reported(view) -> None:
            if self._stopped:
                return
            self._ports.serve(view)
            self._say(Stage.DONE, _(DONE), "")
            if self._ports.cleanup is not None:
                self._ports.cleanup(self.sid, self._cleanup_asks)

        def not_reported(error) -> None:
            # Public all the same (a relay took it): said calmly, and the
            # report is made again quietly; another work listed at the link
            # name is said in one sentence, with a question (RG-M4-b 3).
            if self._stopped:
                return
            taken = getattr(error, "code", None) == Code.CONFLICT \
                and (getattr(error, "detail", {}) or {}).get("reason") == "address_taken"
            state = rules.NOT_LISTED if taken else rules.PUBLIC
            if self._ports.unlisted is not None:
                self._ports.unlisted.hand_over(self.sid, event=event, naddr=self.naddr,
                                               revision=self._version.revision, taken=taken,
                                               on_listed=reported)
            self._say(Stage.NOT_LISTED if taken else Stage.PUBLIC, "", self._public_words(state))

        self._ports.api.published(self.sid, naddr=self.naddr, event=event,
                                  revision=self._version.revision,
                                  on_success=reported, on_failure=not_reported)

    def _cleanup_asks(self) -> None:
        """The signer app is about to be asked to delete the private
        copies: one quiet line says so in the done state (RG-M4-b 12)."""
        if not self._stopped and self.stage is Stage.DONE:
            self._say(Stage.DONE, self.title, _(CLEANUP_ASKS))

    # -- the words ---------------------------------------------------------------------------

    def _fail(self, words: str, stage: Stage = Stage.FAILED) -> None:
        """Nothing goes out: a signed article is dropped and the pictures
        come down."""
        if self.stage is stage and self.words == words:
            return
        self.signed_article = None
        self._let_go()
        self._take_down()
        self._say(stage, _(NOT_PUBLISHED), words)

    def _say(self, stage: Stage, title: str, words: str) -> None:
        self.stage, self.title, self.words = stage, title, words
        self.changed.emit()


TAKING = N_("Taking the correction …")
TAKEN = N_("Correction taken.")
NOT_TAKEN = N_("Correction not taken")


class CorrectionPublish(AcceptedPublish):
    """Korrektur übernehmen (M-4 M15; plan 8.6; 12.4): the correction is
    confirmed alone (``["correction"]``, its verified hash), then
    published again under the writer's key at the address of the article
    as it stands in public (``published``: the event of the newest
    verified ``published`` action), keeping its first date; its
    ``created_at`` comes after that event's, so relays keep the newer.
    It changes only what the sheet shows; the published event's zaps,
    credits and carried tags stay (``publishing.corrected_template``)."""

    def __init__(self, *, published: dict, taken: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        # ``taken``: the View holds the writer's confirmation of it, so it
        # is published without being confirmed again (RG-M4-b 8).
        self._published, self._taken = published, bool(taken)
        self.expected_state = "published"

    @property
    def taken(self) -> bool:
        return self._taken

    @property
    def published_event(self) -> dict:
        """The article as it stands in public: what the correction is
        compared with and keeps."""
        return self._published

    @property
    def d(self) -> str:
        return formats.tag_value(self._published, "d") or super().d

    @property
    def published_at(self) -> Optional[int]:
        return published_at_of(self._published)

    def _template(self, urls: Mapping[str, str]) -> dict:
        return publishing.corrected_template(self.revision, self._published, writer=self.writer,
                                             media_urls=urls,
                                             created_at=int(self._ports.clock()))

    def start(self, *, here: bool = False) -> None:
        if self.stage is Stage.CHANGED:
            return
        if self._taken or self.stage is Stage.WORKING:
            super().start(here=here)
            return
        self._say(Stage.WORKING, _(TAKING), _(BUSY_SIGNING))

        def confirmed(error) -> None:
            if self._stopped:
                return
            if error is not None:
                # A 409 (a newer correction): read again already, and the
                # old hash is never sent again (RG-M4-b 6).
                cause = cause_words(getattr(error, "code", ""))
                stage = Stage.CHANGED if getattr(error, "status", None) == 409 else Stage.FAILED
                self._say(stage, _(NOT_TAKEN), _(cause))
                return
            self._taken = True
            self.stage = Stage.READY
            super(CorrectionPublish, self).start(here=here)

        self._ports.confirm(("correction",), self.revision_hash, confirmed)

    def _say(self, stage: Stage, title: str, words: str) -> None:
        if stage is Stage.DONE:
            title = _(TAKEN)
        super()._say(stage, title, words)


__all__ = ["AcceptedPublish", "BUSY", "BUSY_DETAIL", "BUSY_SIGNING", "CLEANUP_ASKS",
           "CorrectionPublish", "DONE", "HeldPublish", "LINK_RELAYS", "NOT_CHECKED",
           "NOT_PUBLISHABLE",
           "NOT_PUBLISHED", "NOT_THE_SAME", "PICTURE_NOT_FOUND",
           "PICTURE_NOT_UP", "PublishPorts", "SOMEONE_ELSES", "SignedHere", "Stage",
           "TOO_LONG_ELSEWHERE", "TOO_LONG_HERE",
           "offers_here", "publish_job", "run_for"]
