# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one road from a private file to a public one.

Everything a user keeps private is stored as ciphertext, so there is no
such thing as flipping a file to public. The bytes on the server cannot
be read by anyone the user has not handed a key to, and handing out the
key is not publishing, it is giving away the original. The only honest
way to give a reader something they can open is to fetch the ciphertext,
open it here, strip what the picture reveals, and upload a second,
independent blob that has its own hash and its own life.

That sequence is written once, in this module, because every step of it
is a way to leak something. Spread across three call sites it would be
three chances to skip the scrub, to record the wrong hash, or to write a
key somewhere it does not belong. So this is the only code in the app
that turns a :class:`PrivateBlob` into a :class:`PublicBlob`, and the
order below is the whole design:

  1. Ask the ledger, not the pointer, whether a live copy already exists.
  2. Fetch the ciphertext, walking the file's servers until one answers.
  3. Decrypt with the file's own key.
  4. Scrub. There is no path from here to an upload that skips this.
  5. Upload through the ordinary uploader, so the copy gets the same
     auth, verification and mirroring as anything else this app sends.
  6. Commit to the ledger, and only then call it a success.
  7. Point the private record at the copy, re-reading it live first.

Step 6 is where the invariant lives. A public blob that is not in the
ledger is one the user cannot see, and one they cannot see is one they
cannot revoke, so a ledger write that fails fails the whole operation
and stops the run before the next upload starts. Step 7 is the opposite:
the pointer is a convenience, the ledger is the authority, so a pointer
that could not be written costs nothing and is reported rather than
retried.

The private original is never modified by any of this. Nothing here
deletes it, re-keys it, moves it or re-uploads it; the single write it
permits is the pointer in step 7, merged into the record as it reads
live so a rename made on another device is not clobbered.

Consent belongs to :meth:`PublicCopyMaker.ensure_public_copies`, which
is the only entry point the UI should use: it asks before the first byte
moves, and a refusal there means nothing was fetched, nothing uploaded
and nothing recorded. :meth:`PublicCopyMaker.make_public_copy` is the
mechanism underneath it and asserts nothing about consent, so a caller
reaching for it directly is claiming to have obtained it already.

Every boundary is injected: the ledger, the byte fetcher, the uploader,
the private-record store and the clock. The tests drive the whole state
machine with no network, no server, no signer and no real home
directory.
"""

from __future__ import annotations

import hashlib
import time as _time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Callable,
    Deque,
    Dict,
    Final,
    List,
    Optional,
    Protocol,
    Sequence,
)

from PySide6.QtCore import QObject, Signal

import url_safety

from ..blossom.hashes import blob_url, url_agrees_with_hash
from .filecrypto import FileCryptoError, decrypt_file
from .scrub import ScrubError, scrub_for_publication
from .visibility import PrivateBlob, PublicBlob, PublicLedger, needs_public_copy


# Upload job names are how the uploader tells its callbacks apart, so
# they carry a sequence number as well as the source hash. Two attempts
# at the same file must never share a name, or a late answer from the
# first would be read as the answer to the second.
_JOB_PREFIX: Final[str] = "public-copy"


class CopyStage:
    """The stages one copy passes through, for a progress dialog.

    Plain strings rather than an enum: they cross a Qt signal, and a
    dialog that wants to show them should not have to import this
    module to name one.
    """

    CHECKING: Final[str] = "checking"
    FETCHING: Final[str] = "fetching"
    DECRYPTING: Final[str] = "decrypting"
    PREPARING: Final[str] = "preparing"
    UPLOADING: Final[str] = "uploading"
    RECORDING: Final[str] = "recording"
    REUSED: Final[str] = "reused"
    DONE: Final[str] = "done"
    FAILED: Final[str] = "failed"
    CANCELLED: Final[str] = "cancelled"


# Failure text is for a person to read. A transport or a server can be
# as verbose as it likes; what reaches the UI is a sentence.
_MAX_DETAIL_CHARS: Final[int] = 120


# --------------------------------------------------------------------------- #
# The seams                                                                   #
# --------------------------------------------------------------------------- #

class CiphertextFetcher(Protocol):
    """Fetch the bytes at one URL.

    Deliberately one URL per call and nothing more. Choosing which
    server to ask, and what to do when it will not answer, is the
    interesting part and belongs in this module where it can be read
    next to everything else that decides what becomes public.

    The bytes are verified against the content hash here, so an
    implementation is not trusted to do it and cannot be the reason a
    substituted blob is decrypted.
    """

    def fetch(
        self,
        url: str,
        *,
        on_success: Callable[[bytes], None],
        on_failure: Callable[[str], None],
    ) -> None: ...


class Uploader(Protocol):
    """The upload orchestrator (``MediaStore`` in the app).

    Also emits ``upload_finished(name, media)`` and
    ``upload_failed(name, reason)``, where ``media`` carries ``hash``,
    ``url`` and ``urls``. The public copy goes through this rather than
    a private path of its own so it gets the same auth, the same hash
    verification and the same mirroring as any other upload.
    """

    def upload_bytes(self, body: bytes, *, name: str, mime_type: str) -> None: ...


class PrivateRecordStore(Protocol):
    """Read and write one private file record, live.

    ``read_record`` answers with the decrypted record as it is on the
    user's relays right now, or None when it could not be read.
    ``write_record`` re-encrypts and republishes it.

    The record it hands back holds the file's key. Nothing in this
    module logs it, persists it, or puts it in a result, a signal or a
    failure message; the dict is merged and handed straight back.
    """

    def read_record(
        self, sha256: str, *, on_done: Callable[[Optional[dict]], None],
    ) -> None: ...

    def write_record(
        self, sha256: str, record: dict, *, on_done: Callable[[bool], None],
    ) -> None: ...


# --------------------------------------------------------------------------- #
# Results                                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CopyResult:
    """What one copy attempt produced, or why it produced nothing.

    ``reason`` is written for a person and never carries a key, a
    ciphertext or a fragment of either.
    """

    source_hash: str
    public: Optional[PublicBlob] = None
    reason: str = ""
    # The copy already existed and was reused, so nothing was uploaded.
    reused: bool = False
    # Stopped by the user rather than by a fault. Nothing was uploaded
    # for this file, which is why it is not counted as a failure.
    cancelled: bool = False
    # Whether the private record now points at the copy. False is not a
    # failure: the ledger is what decides a file is published.
    pointer_recorded: bool = False

    @property
    def ok(self) -> bool:
        return self.public is not None


@dataclass(frozen=True)
class PublishSet:
    """The answer to "may this be published, and with what".

    ``blobs`` is the rewritten list in the caller's own order, or None
    when publishing must not go ahead. None with ``cancelled`` set is
    the user's answer; None without it is a failure, and ``failures``
    says which file and why.

    ``minted`` lists the copies this run created. Every one of them is
    in the ledger, which is what makes a run that stopped half way an
    inconvenience rather than a set of public blobs the user cannot
    find.
    """

    blobs: Optional[List[PublicBlob]] = None
    cancelled: bool = False
    minted: List[PublicBlob] = field(default_factory=list)
    failures: List[CopyResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.blobs is not None


# --------------------------------------------------------------------------- #
# Internal state                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class _Copy:
    """One in-flight copy. Never holds the plaintext longer than a step."""

    blob: PrivateBlob
    on_done: Callable[[CopyResult], None]
    job: str
    urls: List[str]
    # Set the moment the bytes are handed to the uploader. From then on
    # the copy is carried to its ledger commit whatever else happens,
    # because abandoning it is how a blob ends up public and unlisted.
    uploading: bool = False
    sha: str = ""
    size: int = 0
    mime: str = ""
    scrubbed: bool = False


@dataclass
class _Run:
    """One ensure_public_copies pass over a document's media."""

    inputs: List[PrivateBlob]
    queue: Deque[PrivateBlob]
    on_done: Callable[[PublishSet], None]
    total: int
    done: int = 0
    # What this run settled on for each source, minted or reused. Kept
    # because the ledger cannot always answer the reverse question: two
    # private files that scrub to identical bytes are one public blob
    # with one ``source_hash``, and the second copy overwrites the first.
    by_source: Dict[str, PublicBlob] = field(default_factory=dict)
    minted: List[PublicBlob] = field(default_factory=list)
    failures: List[CopyResult] = field(default_factory=list)
    cancelled: bool = False
    stopped: bool = False
    finished: bool = False


# --------------------------------------------------------------------------- #
# The maker                                                                   #
# --------------------------------------------------------------------------- #

class PublicCopyMaker(QObject):
    """Mints public copies of private files, one at a time.

    Signals:
      copy_progress(str, str)   source hash, one of ``CopyStage``
      copy_finished(str, object)  source hash, the :class:`CopyResult`
      run_progress(int, int)    copies settled, copies planned

    One at a time is not a simplification. Each upload is a signer
    prompt and a ledger write, and the ledger must hold every copy
    before the next upload starts, so a fan-out would be both a stack of
    prompts and a window in which a crash strands a public blob.
    """

    copy_progress = Signal(str, str)
    copy_finished = Signal(str, object)
    run_progress = Signal(int, int)

    def __init__(
        self,
        *,
        ledger: PublicLedger,
        fetcher: CiphertextFetcher,
        uploader: Uploader,
        records: Optional[PrivateRecordStore] = None,
        clock: Optional[Callable[[], int]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._ledger = ledger
        self._fetcher = fetcher
        self._uploader = uploader
        # Optional: without it a copy is still made and still recorded,
        # it just leaves no hint on the private record. The ledger is
        # the authority, so nothing downstream depends on the pointer.
        self._records = records
        self._clock = clock or (lambda: int(_time.time()))

        self._active: Optional[_Copy] = None
        self._run: Optional[_Run] = None
        self._pumping = False
        self._sequence = 0

        uploader.upload_finished.connect(self._on_upload_finished)
        uploader.upload_failed.connect(self._on_upload_failed)

    # -- state -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._active is not None or self._run is not None

    def existing_copy(self, blob: PrivateBlob) -> Optional[PublicBlob]:
        """The live public copy of ``blob``, or None.

        The pointer on the record is a hint and is believed only when
        the ledger agrees, because the two can disagree: a revoke that
        half succeeded leaves a pointer naming a blob that is gone.
        """
        pointed = (
            self._ledger.get(blob.public_copy_hash)
            if blob.public_copy_hash
            else None
        )
        if pointed is not None and pointed.source_hash == blob.sha256:
            return pointed
        return self._ledger.copy_of(blob.sha256)

    # -- one file ----------------------------------------------------------

    def make_public_copy(
        self, blob: PrivateBlob, *, on_done: Callable[[CopyResult], None],
    ) -> None:
        """Mint the public copy of one private file.

        Says nothing about consent: this is the mechanism, and
        :meth:`ensure_public_copies` is the gate. A caller here is
        claiming the user has already agreed to publish this file.
        """
        if self._active is not None:
            # Two copies at once would race for the ledger and the
            # signer. Refusing is honest; queueing behind an operation
            # the caller cannot see is not.
            self._refuse(blob, on_done, "Another copy is already being made.")
            return

        existing = self.existing_copy(blob)
        if existing is not None:
            # Step 1. Nothing is fetched and nothing is uploaded: the
            # bytes are already public and already revocable.
            self.copy_progress.emit(blob.sha256, CopyStage.REUSED)
            result = CopyResult(
                source_hash=blob.sha256, public=existing, reused=True,
            )
            self.copy_finished.emit(blob.sha256, result)
            on_done(result)
            return

        urls = _candidate_urls(blob)
        if not urls:
            self._refuse(
                blob,
                on_done,
                "This file does not say where it is stored, so no public "
                "copy could be made.",
            )
            return

        self._sequence += 1
        copy = _Copy(
            blob=blob,
            on_done=on_done,
            job=f"{_JOB_PREFIX}:{self._sequence}:{blob.sha256}",
            urls=urls,
        )
        self._active = copy
        self.copy_progress.emit(blob.sha256, CopyStage.CHECKING)
        self._fetch_next(copy, "")

    def cancel(self) -> None:
        """Stop the current run at the next safe boundary.

        A copy that has not reached the uploader is dropped where it
        stands: nothing left the process, so there is nothing to list.
        A copy already uploading is carried through to its ledger commit
        instead, because the alternative is a blob on a server that
        appears in no list the user can revoke from.
        """
        run = self._run
        if run is not None:
            run.cancelled = True
            run.stopped = True
            run.queue.clear()

        active = self._active
        if active is not None and not active.uploading:
            # Answered rather than abandoned: every copy that starts
            # settles exactly once, so a dialog is never left showing a
            # file stuck at whichever stage it had reached. The late
            # answers this copy is still owed find a stranger in
            # ``_active`` and bail.
            self.copy_progress.emit(active.blob.sha256, CopyStage.CANCELLED)
            self._settle(active, CopyResult(
                source_hash=active.blob.sha256,
                cancelled=True,
                reason="This copy was cancelled before anything was uploaded.",
            ))
            return
        if run is not None:
            self._pump()

    # -- a document's worth ------------------------------------------------

    def ensure_public_copies(
        self,
        blobs: Sequence[PrivateBlob],
        *,
        consent: Callable[[List[PrivateBlob]], bool],
        on_done: Callable[[PublishSet], None],
    ) -> None:
        """Make sure every one of ``blobs`` has a public copy to publish.

        Answers with the rewritten list once they all do, or with None
        when publishing must not go ahead. ``consent`` is asked exactly
        once, before anything is fetched, and only when copies are
        actually needed: a set of files that are already public mints
        nothing, so there is nothing to warn about.

        A refusal at that prompt means nothing was uploaded and nothing
        was recorded. A cancel later means the same for every file that
        had not started uploading; the ones already minted stay in the
        ledger, listed and revocable, and are reported in ``minted``.

        The first failure stops the run. The publish cannot go ahead
        without that file, so continuing would put more of the user's
        pictures on the public internet for an operation that is not
        going to happen.
        """
        if self.busy:
            on_done(PublishSet(
                blobs=None,
                failures=[CopyResult(
                    source_hash="",
                    reason="Public copies are already being made.",
                )],
            ))
            return

        inputs = list(blobs)
        needed: List[PrivateBlob] = []
        seen: set = set()
        for blob in needs_public_copy(inputs, self._ledger):
            # The same picture used twice is one copy. The ledger check
            # inside ``make_public_copy`` would catch the second anyway,
            # but not before it had been counted and shown as work.
            if blob.sha256 in seen:
                continue
            seen.add(blob.sha256)
            needed.append(blob)

        if not needed:
            on_done(self._publish_set(inputs, settled={}, minted=[]))
            return

        if not consent(list(needed)):
            # Nothing has been fetched, uploaded or written at this
            # point, and that is exactly what the user was promised.
            on_done(PublishSet(blobs=None, cancelled=True))
            return

        self._run = _Run(
            inputs=inputs,
            queue=deque(needed),
            on_done=on_done,
            total=len(needed),
        )
        self.run_progress.emit(0, len(needed))
        self._pump()

    # -- internals: the run loop -------------------------------------------

    def _pump(self) -> None:
        """Start copies until one is in flight or the run is over.

        A loop with a re-entrancy guard rather than a chain of
        callbacks: a fetcher and an uploader that answer synchronously
        would otherwise recurse once per file, and the depth would grow
        with the size of the document.
        """
        if self._pumping:
            return
        self._pumping = True
        finished: Optional[_Run] = None
        try:
            while True:
                run = self._run
                if run is None or self._active is not None:
                    break
                if run.stopped or not run.queue:
                    run.stopped = True
                    break
                self.make_public_copy(
                    run.queue.popleft(), on_done=self._on_item_done,
                )
            run = self._run
            if (
                run is not None
                and self._active is None
                and run.stopped
                and not run.finished
            ):
                # Claimed inside the guard so a re-entrant pump cannot
                # settle the same run twice.
                run.finished = True
                finished = run
        finally:
            self._pumping = False
        if finished is not None:
            self._finish_run(finished)

    def _on_item_done(self, result: CopyResult) -> None:
        run = self._run
        if run is None:
            return
        run.done += 1
        if result.cancelled:
            run.cancelled = True
            run.stopped = True
            run.queue.clear()
        elif result.ok and result.public is not None:
            run.by_source[result.source_hash] = result.public
            if not result.reused:
                run.minted.append(result.public)
        else:
            # The first failure stops the run: this publish cannot go
            # ahead without the file, so minting more copies would put
            # more of the user's pictures on the internet for nothing.
            run.failures.append(result)
            run.stopped = True
            run.queue.clear()
        self.run_progress.emit(run.done, run.total)

    def _finish_run(self, run: _Run) -> None:
        self._run = None
        if run.cancelled or run.failures:
            outcome = PublishSet(
                blobs=None,
                cancelled=run.cancelled,
                minted=list(run.minted),
                failures=list(run.failures),
            )
        else:
            outcome = self._publish_set(
                run.inputs, settled=run.by_source, minted=run.minted,
            )
        run.on_done(outcome)

    def _publish_set(
        self,
        inputs: Sequence[PrivateBlob],
        *,
        settled: dict,
        minted: Sequence[PublicBlob],
    ) -> PublishSet:
        """The rewritten list, checked against the ledger.

        Every entry is confirmed to be listed before it is handed to a
        publisher, because the ledger is what makes a blob revocable and
        a copy that did not land in it must not be published as if it
        had. What the run settled on is consulted first only because the
        ledger cannot always answer the reverse question.
        """
        rewritten: List[PublicBlob] = []
        for blob in inputs:
            copy = settled.get(blob.sha256) or self.existing_copy(blob)
            if copy is None or self._ledger.get(copy.sha256) is None:
                return PublishSet(
                    blobs=None,
                    minted=list(minted),
                    failures=[CopyResult(
                        source_hash=blob.sha256,
                        reason="This file's public copy is not listed, so it "
                               "was not published.",
                    )],
                )
            rewritten.append(copy)
        return PublishSet(blobs=rewritten, minted=list(minted))

    # -- internals: one copy, step by step ---------------------------------

    def _fetch_next(self, copy: _Copy, earlier: str) -> None:
        """Step 2. Ask the next server that might hold the ciphertext.

        One server at a time, and every one of them before giving up: a
        file the user mirrored is a file that survives one host being
        down, and that is the entire point of having mirrored it.
        """
        if self._active is not copy:
            return
        if not copy.urls:
            detail = _short(earlier)
            self._fail(
                copy,
                "This file could not be downloaded from any of your servers"
                + (f" ({detail})." if detail else "."),
            )
            return
        url = copy.urls.pop(0)
        self.copy_progress.emit(copy.blob.sha256, CopyStage.FETCHING)
        self._fetcher.fetch(
            url,
            on_success=lambda data, c=copy: self._on_fetched(c, data),
            on_failure=lambda reason, c=copy: self._fetch_next(c, str(reason or "")),
        )

    def _on_fetched(self, copy: _Copy, data: bytes) -> None:
        if self._active is not copy:
            return
        if not data or hashlib.sha256(data).hexdigest() != copy.blob.sha256:
            # Content addressing is the only reason it is safe to ask a
            # server the user did not choose. Bytes that are not the
            # ones asked for are that server failing, not this file.
            self._fetch_next(copy, "a server returned the wrong file")
            return

        self.copy_progress.emit(copy.blob.sha256, CopyStage.DECRYPTING)
        try:
            plaintext = decrypt_file(data, copy.blob.key_hex)
        except FileCryptoError:
            # The exception text is not repeated. Nothing in it carries
            # a key today, and this is the one place where a change to
            # that would be silent and unrecoverable.
            self._fail(
                copy,
                "This file could not be opened with the key in your library, "
                "so no public copy was made.",
            )
            return

        self.copy_progress.emit(copy.blob.sha256, CopyStage.PREPARING)
        try:
            scrubbed = scrub_for_publication(plaintext, copy.blob.mime)
        except ScrubError as exc:
            # Step 4 refusing is the feature. There is no branch from
            # here that uploads the bytes anyway.
            self._fail(copy, f"This file was not published: {exc}.")
            return
        finally:
            # The plaintext of a private file has no business outliving
            # the step that needed it, and the upload below is the part
            # that can take a while.
            del plaintext

        copy.sha = hashlib.sha256(scrubbed.data).hexdigest()
        copy.size = len(scrubbed.data)
        copy.mime = scrubbed.mime
        copy.scrubbed = scrubbed.scrubbed

        self.copy_progress.emit(copy.blob.sha256, CopyStage.UPLOADING)
        copy.uploading = True
        # The uploader may fail inside this call and re-enter the slots
        # below, so nothing may be touched after it returns.
        self._uploader.upload_bytes(
            scrubbed.data, name=copy.job, mime_type=scrubbed.mime,
        )

    def _on_upload_finished(self, name: str, media: object) -> None:
        copy = self._active
        if copy is None or name != copy.job:
            return
        public = _public_blob_from(media, copy, self._clock())
        if public is None:
            # Uploaded, but with no address this app is willing to
            # record. Recording a guess would put a blob in the ledger
            # that a revoke cannot reach, which is worse than saying so.
            self._fail(
                copy,
                "The server did not give this copy an address this app can "
                "record, so it was not published.",
            )
            return

        self.copy_progress.emit(copy.blob.sha256, CopyStage.RECORDING)
        if not self._ledger.record(public):
            # Step 6. The blob is on a server and the list of what is
            # public could not be written, so the user has something
            # they cannot see and therefore cannot revoke. That is a
            # failure of the whole operation, and the run stops here
            # rather than adding a second one.
            self._fail(
                copy,
                "Your record of what is public could not be saved, so this "
                "copy was not published. It may already be on your server.",
            )
            return

        self._record_pointer(copy, public)

    def _on_upload_failed(self, name: str, reason: str) -> None:
        copy = self._active
        if copy is None or name != copy.job:
            return
        detail = _short(reason)
        self._fail(
            copy,
            "This file's public copy could not be uploaded"
            + (f": {detail}" if detail else "."),
        )

    # -- step 7: the pointer, which is only ever a hint ---------------------

    def _record_pointer(self, copy: _Copy, public: PublicBlob) -> None:
        """Note the copy on the private record, without clobbering it.

        The record is re-read live first. It may have been renamed, or
        moved to another folder, on another device since this run began,
        and writing back the copy this session started with would undo
        that. So the live record is what is written, with one field
        added to it.

        Nothing here is fatal. The ledger already holds the copy, which
        is what decides whether the file is published; the pointer only
        saves a lookup.
        """
        if self._records is None:
            self._succeed(copy, public, pointer=False)
            return

        def _on_read(record: Optional[dict], c=copy, p=public) -> None:
            if self._active is not c:
                return
            merged = _merge_public_copy(record, p, c.blob.sha256)
            if merged is None:
                self._succeed(c, p, pointer=False)
                return
            self._records.write_record(
                c.blob.sha256,
                merged,
                on_done=lambda ok, cc=c, pp=p: (
                    self._succeed(cc, pp, pointer=bool(ok))
                    if self._active is cc
                    else None
                ),
            )

        self._records.read_record(copy.blob.sha256, on_done=_on_read)

    # -- settling ----------------------------------------------------------

    def _succeed(self, copy: _Copy, public: PublicBlob, *, pointer: bool) -> None:
        self.copy_progress.emit(copy.blob.sha256, CopyStage.DONE)
        self._settle(copy, CopyResult(
            source_hash=copy.blob.sha256,
            public=public,
            pointer_recorded=pointer,
        ))

    def _fail(self, copy: _Copy, reason: str) -> None:
        self.copy_progress.emit(copy.blob.sha256, CopyStage.FAILED)
        self._settle(copy, CopyResult(source_hash=copy.blob.sha256, reason=reason))

    def _refuse(
        self,
        blob: PrivateBlob,
        on_done: Callable[[CopyResult], None],
        reason: str,
    ) -> None:
        """Fail a copy that never became active."""
        self.copy_progress.emit(blob.sha256, CopyStage.FAILED)
        result = CopyResult(source_hash=blob.sha256, reason=reason)
        self.copy_finished.emit(blob.sha256, result)
        on_done(result)

    def _settle(self, copy: _Copy, result: CopyResult) -> None:
        if self._active is copy:
            self._active = None
        self.copy_finished.emit(copy.blob.sha256, result)
        copy.on_done(result)
        self._pump()


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _short(text: object) -> str:
    """One line of somebody else's failure message, at most."""
    trimmed = " ".join(str(text or "").split())
    if len(trimmed) <= _MAX_DETAIL_CHARS:
        return trimmed
    return trimmed[:_MAX_DETAIL_CHARS].rstrip() + "..."


def _candidate_urls(blob: PrivateBlob) -> List[str]:
    """Where the ciphertext might be, best first.

    Built from BUD-01's canonical ``<origin>/<sha256>`` rather than
    from a stored address, because that form is what every Blossom
    server serves and what makes a mirror worth having. The media
    policy is applied here so a record naming a plain-http host cannot
    talk this into a downgrade.
    """
    urls: List[str] = []
    for server in blob.servers:
        origin = url_safety.origin_of(server)
        if not origin:
            continue
        url = blob_url(origin, blob.sha256)
        if url in urls or not url_safety.is_safe_media_url(url):
            continue
        urls.append(url)
    return urls


def _public_blob_from(
    media: object, copy: _Copy, now: int,
) -> Optional[PublicBlob]:
    """The ledger entry for a finished upload, or None when unusable.

    The hash is the one computed here from the bytes that were sent,
    never the server's claim about them: the hash is how a copy is
    revoked, so recording someone else's number would record something
    the user cannot reach. A server that confirms a different hash is
    answering about a different file, and that is a failure.
    """
    claimed = str(getattr(media, "hash", "") or "").lower()
    if claimed and claimed != copy.sha:
        return None

    servers: List[str] = []

    def offer(value: str) -> None:
        origin = url_safety.origin_of(value)
        if not origin or origin in servers:
            return
        if url_safety.is_safe_media_url(origin):
            servers.append(origin)

    entries = getattr(media, "urls", None)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                offer(str(entry.get("server") or entry.get("url") or ""))

    primary = str(getattr(media, "url", "") or "")
    offer(primary)
    if not servers:
        return None

    url = ""
    if (
        primary
        and url_safety.is_safe_media_url(primary)
        and url_agrees_with_hash(primary, copy.sha)
    ):
        url = primary
    if not url:
        # BUD-01 serves every blob from the root of the domain, so this
        # address is safe to construct for a server that confirmed it.
        url = blob_url(servers[0], copy.sha)

    try:
        return PublicBlob(
            sha256=copy.sha,
            url=url,
            servers=servers,
            size=copy.size,
            mime=copy.mime,
            uploaded_at=now,
            source_hash=copy.blob.sha256,
            scrubbed=copy.scrubbed,
        )
    except ValueError:
        return None


def _merge_public_copy(
    record: Optional[dict], public: PublicBlob, source_hash: str,
) -> Optional[dict]:
    """The live record with the pointer added, or None to leave it alone.

    Everything except ``publicCopy`` is carried across exactly as it
    was read, which is what makes a rename on another device survive
    this write. A record that could not be read, that is filed under
    another file, or that the user has since deleted is not written at
    all: inventing one would replace the only copy of that file's key
    with a record this app made up.
    """
    if not isinstance(record, dict):
        return None
    if str(record.get("hash") or "").strip().lower() != source_hash:
        return None
    if record.get("deleted"):
        # Deleted while this ran. Publishing a copy of it is done and
        # recorded; resurrecting the private record is not ours to do.
        return None
    merged = dict(record)
    merged["publicCopy"] = {
        "hash": public.sha256,
        "servers": list(public.servers),
        "uploadedAt": public.uploaded_at,
    }
    return merged
