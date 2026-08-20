# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the one road from a private file to a public one.

Every failure here is quiet and expensive. Uploading bytes that were
never scrubbed publishes a home address. Recording a copy under the
wrong hash publishes something the user cannot revoke. Uploading a
second copy before the first is listed leaves a blob nobody can find.
And touching the private original in any of these paths risks the only
copy of the key that opens it.

So the assertions are mostly negative: what did NOT get uploaded, what
did NOT reach the ledger, and what the private record still looks like
afterwards. The private original is checked in every failure path, not
only the interesting ones, because the path that damages it will be the
one nobody thought to look at.

Nothing here touches a network, a server, a relay, a signer or a real
home directory. The fetcher, the uploader and the record store are all
fakes, and the ledger is a real one writing into a tmp directory.
"""

from __future__ import annotations

import copy as _copy
import hashlib
import json
import os
import struct
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from nostr.media.filecrypto import encrypt_file
from nostr.media.scrub import scrub_for_publication
from nostr.media.publish_copy import (
    CopyStage,
    PublicCopyMaker,
    _merge_public_copy,
)
from nostr.media.visibility import PrivateBlob, PublicBlob, PublicLedger

from tests.media_fakes import (
    GIF_BYTES,
    PNG_BYTES,
    SVG_BYTES,
    TEXT_BYTES,
    make_media,
    sha_of,
)


KEY = "1e" * 32
OTHER_KEY = "2f" * 32

SERVER_ONE = "https://one.example"
SERVER_TWO = "https://two.example"
CDN = "https://cdn.example"

NOW = 1_700_000_000


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


# --------------------------------------------------------------------- #
# Fixtures built from real ciphertext                                   #
# --------------------------------------------------------------------- #

def sealed(data: bytes = PNG_BYTES, key: str = KEY) -> bytes:
    """A real envelope, so decryption in these tests is real."""
    return encrypt_file(data, key_hex=key).envelope


def jpeg_with_gps():
    """A JPEG carrying a recognisable EXIF payload, as a camera writes it.

    Spliced in as an APP1 segment right after SOI, so the marker really
    is in the file rather than appended where a decoder would skip it.
    """
    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor("red"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "JPEG")
    buffer.close()
    base = bytes(buffer.data())
    secret = b"GPSLatitude=51.5074;GPSLongitude=-0.1278;Make=SecretCamera"
    payload = b"Exif\x00\x00" + secret
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return base[:2] + app1 + base[2:], secret


CIPHER = sealed()
CIPHER_SHA = sha_of(CIPHER)
GIF_CIPHER = sealed(GIF_BYTES)
GIF_CIPHER_SHA = sha_of(GIF_CIPHER)
TEXT_CIPHER = sealed(TEXT_BYTES)
TEXT_CIPHER_SHA = sha_of(TEXT_CIPHER)
SVG_CIPHER = sealed(SVG_BYTES)
SVG_CIPHER_SHA = sha_of(SVG_CIPHER)


def private(
    envelope: bytes = CIPHER,
    *,
    servers=(SERVER_ONE,),
    key: str = KEY,
    **kw,
) -> PrivateBlob:
    return PrivateBlob(
        sha256=sha_of(envelope),
        key_hex=key,
        servers=list(servers),
        size=len(envelope),
        mime=kw.pop("mime", "image/png"),
        name=kw.pop("name", "holiday.png"),
        uploaded_at=kw.pop("uploaded_at", NOW - 100),
        **kw,
    )


def record_for(blob: PrivateBlob, **kw) -> dict:
    """A private library record in the reference implementation's shape."""
    record = {
        "name": blob.name,
        "hash": blob.sha256,
        "size": blob.size,
        "type": blob.mime,
        "folder": "/photos",
        "uploadedAt": blob.uploaded_at,
        "server": blob.servers[0] if blob.servers else "",
        "servers": list(blob.servers),
        "encryptionKey": blob.key_hex,
    }
    record.update(kw)
    return record


# --------------------------------------------------------------------- #
# Fakes                                                                 #
# --------------------------------------------------------------------- #

class FakeFetcher:
    """Serves bytes per URL, or refuses per URL. Never a socket."""

    def __init__(self, blobs=None, *, park=False) -> None:
        self.blobs = dict(blobs or {})
        self.errors: dict = {}
        self.requests: list = []
        self.park = park
        self.pending: list = []

    def fetch(self, url, *, on_success, on_failure) -> None:
        self.requests.append(url)
        if self.park:
            self.pending.append((url, on_success, on_failure))
            return
        if url in self.errors:
            on_failure(self.errors[url])
            return
        data = self.blobs.get(url)
        if data is None:
            on_failure("that server does not have it")
            return
        on_success(data)

    def deliver(self, index: int = 0, data: bytes = CIPHER) -> None:
        _url, on_success, _on_failure = self.pending.pop(index)
        on_success(data)


class FakeUploader(QObject):
    """MediaStore's observed surface: two signals and one method."""

    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)

    def __init__(self, *, servers=(CDN,), auto=True, log=None) -> None:
        super().__init__()
        self.calls: list = []
        self.servers = list(servers)
        self.auto = auto
        self.fail_with: str | None = None
        self.media_for = None      # callable(sha) -> media record
        self.log = log if log is not None else []

    def upload_bytes(self, body, *, name, mime_type="application/octet-stream") -> None:
        self.calls.append(
            SimpleNamespace(name=name, mime_type=mime_type, body=bytes(body))
        )
        self.log.append(("upload", sha_of(bytes(body))))
        if not self.auto:
            return
        if self.fail_with is not None:
            self.upload_failed.emit(name, self.fail_with)
            return
        self.finish()

    # -- drivers -----------------------------------------------------------

    def finish(self, name: str | None = None, media=None) -> None:
        call = self.calls[-1]
        sha = sha_of(call.body)
        if media is None:
            media = (
                self.media_for(sha) if self.media_for is not None
                else make_media(sha, servers=self.servers, mime=call.mime_type)
            )
        self.upload_finished.emit(name or call.name, media)

    def fail(self, reason: str = "the server said no", name: str | None = None) -> None:
        self.upload_failed.emit(name or self.calls[-1].name, reason)


class FakeRecords:
    """The private library, live. Reads and writes are deep copies."""

    def __init__(self, records=None) -> None:
        self.records = {r["hash"]: _copy.deepcopy(r) for r in (records or [])}
        self.reads: list = []
        self.writes: list = []
        self.write_ok = True
        self.unreadable: set = set()

    def read_record(self, sha256, *, on_done) -> None:
        self.reads.append(sha256)
        if sha256 in self.unreadable:
            on_done(None)
            return
        found = self.records.get(sha256)
        on_done(_copy.deepcopy(found) if found is not None else None)

    def write_record(self, sha256, record, *, on_done) -> None:
        self.writes.append((sha256, _copy.deepcopy(record)))
        if self.write_ok:
            self.records[sha256] = _copy.deepcopy(record)
        on_done(self.write_ok)


class LoggingLedger(PublicLedger):
    """A real ledger that notes when a commit happened, in order."""

    def __init__(self, path, log) -> None:
        super().__init__(path=path)
        self._log = log

    def record(self, blob) -> bool:
        ok = super().record(blob)
        self._log.append(("record", blob.sha256))
        return ok


# --------------------------------------------------------------------- #
# Wiring                                                                #
# --------------------------------------------------------------------- #

class Env(SimpleNamespace):
    """Everything one test needs, plus the assertions worth repeating."""

    def copy_one(self, blob):
        """Run one copy to completion and return the result."""
        answers: list = []
        self.maker.make_public_copy(blob, on_done=answers.append)
        assert len(answers) == 1, "the fakes settle synchronously"
        return answers[0]

    def ensure(self, blobs, *, consent=True):
        answers: list = []

        def asked(needed):
            self.consent_calls.append(list(needed))
            return consent

        self.maker.ensure_public_copies(
            blobs, consent=asked, on_done=answers.append,
        )
        return answers

    def on_disk(self):
        """The ledger as another process would read it back."""
        return PublicLedger(path=self.ledger_path)

    def ledger_text(self):
        try:
            return self.ledger_path.read_text(encoding="utf-8")
        except OSError:
            return ""


def build(
    tmp_path,
    *,
    blobs=None,
    records=None,
    ledger=None,
    servers=(CDN,),
    auto=True,
    with_records=True,
) -> Env:
    log: list = []
    path = tmp_path / "media_public.json"
    store = FakeRecords(records)
    fetcher = FakeFetcher(blobs if blobs is not None else {
        f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
    })
    uploader = FakeUploader(servers=servers, auto=auto, log=log)
    real_ledger = ledger if ledger is not None else LoggingLedger(path, log)
    maker = PublicCopyMaker(
        ledger=real_ledger,
        fetcher=fetcher,
        uploader=uploader,
        records=store if with_records else None,
        clock=lambda: NOW,
    )
    env = Env(
        maker=maker,
        ledger=real_ledger,
        ledger_path=path,
        fetcher=fetcher,
        uploader=uploader,
        records=store,
        log=log,
        stages=[],
        finished=[],
        runs=[],
        consent_calls=[],
    )
    maker.copy_progress.connect(lambda sha, stage: env.stages.append((sha, stage)))
    maker.copy_finished.connect(lambda sha, res: env.finished.append((sha, res)))
    maker.run_progress.connect(lambda done, total: env.runs.append((done, total)))
    return env


def assert_original_intact(env, blob, before_record):
    """The private original survived whatever just happened.

    Four ways it could not have: the blob record in memory changed, the
    record on the user's relays was rewritten, its key moved, or the
    source blob itself was pushed back to a server.
    """
    assert blob == before_record["blob"], "the private blob was modified"
    assert blob.key_hex == before_record["blob"].key_hex
    stored = env.records.records.get(blob.sha256)
    assert stored == before_record["record"], "the private record was rewritten"
    if stored is not None:
        assert stored.get("encryptionKey") == before_record["blob"].key_hex
        assert not stored.get("deleted")
    # Nothing ever re-uploads the ciphertext, under any name.
    assert all(call.body != CIPHER for call in env.uploader.calls)
    assert all(sha_of(call.body) != blob.sha256 for call in env.uploader.calls)


def snapshot(env, blob):
    return {
        "blob": _copy.deepcopy(blob),
        "record": _copy.deepcopy(env.records.records.get(blob.sha256)),
    }


# --------------------------------------------------------------------- #
# Step 1: reuse is decided by the ledger                                #
# --------------------------------------------------------------------- #

def test_a_file_that_is_already_public_is_reused_and_nothing_is_uploaded(tmp_path):
    env = build(tmp_path)
    blob = private()
    existing = PublicBlob(
        sha256="c" * 64, url=f"{CDN}/{'c' * 64}", source_hash=blob.sha256,
    )
    env.ledger.record(existing)

    result = env.copy_one(blob)

    assert result.ok and result.reused
    assert result.public is existing
    assert env.fetcher.requests == []
    assert env.uploader.calls == []


def test_reuse_is_read_from_the_ledger_not_from_the_records_pointer(tmp_path):
    # The pointer says published; the ledger says that blob is gone,
    # which is what a half-finished revoke leaves behind. Believing the
    # pointer would badge a dead link as live and refuse to republish.
    env = build(tmp_path)
    blob = private(public_copy_hash="d" * 64)

    result = env.copy_one(blob)

    assert result.ok and not result.reused
    assert env.uploader.calls, "a real copy was made"


def test_a_pointer_at_another_files_copy_is_not_reuse(tmp_path):
    env = build(tmp_path)
    stranger = PublicBlob(
        sha256="e" * 64, url=f"{CDN}/{'e' * 64}", source_hash="f" * 64,
    )
    env.ledger.record(stranger)
    blob = private(public_copy_hash=stranger.sha256)

    result = env.copy_one(blob)

    assert result.public is not None and result.public.sha256 != stranger.sha256
    assert result.public.source_hash == blob.sha256


def test_a_pointer_the_ledger_agrees_with_is_reused_without_a_scan(tmp_path):
    env = build(tmp_path)
    blob = private()
    live = PublicBlob(
        sha256="c" * 64, url=f"{CDN}/{'c' * 64}", source_hash=blob.sha256,
    )
    env.ledger.record(live)
    pointed = private(public_copy_hash=live.sha256)

    assert env.maker.existing_copy(pointed) is live


# --------------------------------------------------------------------- #
# The whole sequence, in order                                          #
# --------------------------------------------------------------------- #

def test_a_copy_is_fetched_decrypted_scrubbed_uploaded_and_recorded(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])

    result = env.copy_one(blob)

    assert result.ok
    assert env.fetcher.requests == [f"{SERVER_ONE}/{blob.sha256}"]
    assert len(env.uploader.calls) == 1
    public = result.public
    assert env.on_disk().get(public.sha256) is not None


def test_the_bytes_uploaded_are_the_scrubbed_plaintext(tmp_path):
    env = build(tmp_path)
    blob = private()

    env.copy_one(blob)

    sent = env.uploader.calls[0].body
    assert sent != CIPHER, "the ciphertext was never the thing to publish"
    assert sent == scrub_for_publication(PNG_BYTES).data
    assert sent.startswith(b"\x89PNG")


def test_what_reaches_the_server_carries_no_camera_or_location_data(tmp_path):
    # The assertion that matters most in this file: the metadata is
    # genuinely gone from the bytes that left, not merely scrubbed
    # somewhere on the way.
    photo, secret = jpeg_with_gps()
    envelope = sealed(photo)
    blob = private(envelope, mime="image/jpeg")
    env = build(tmp_path, blobs={f"{SERVER_ONE}/{sha_of(envelope)}": envelope})

    result = env.copy_one(blob)

    assert result.ok and result.public.scrubbed is True
    sent = env.uploader.calls[0].body
    assert secret not in sent
    assert b"Exif" not in sent
    assert sent.startswith(b"\xff\xd8")


def test_the_ledger_entry_names_the_copys_own_hash_and_its_source(tmp_path):
    env = build(tmp_path)
    blob = private()

    result = env.copy_one(blob)

    uploaded = env.uploader.calls[0].body
    assert result.public.sha256 == hashlib.sha256(uploaded).hexdigest()
    assert result.public.source_hash == blob.sha256
    assert result.public.sha256 != blob.sha256
    assert result.public.size == len(uploaded)
    assert result.public.scrubbed is True
    assert result.public.uploaded_at == NOW


def test_the_copy_is_addressed_at_the_server_that_confirmed_it(tmp_path):
    env = build(tmp_path, servers=(CDN, SERVER_TWO))
    blob = private()

    result = env.copy_one(blob)

    assert result.public.servers == [CDN, SERVER_TWO]
    assert result.public.url == f"{CDN}/{result.public.sha256}"


def test_an_animated_gif_is_published_untouched_and_says_so(tmp_path):
    env = build(tmp_path, blobs={f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER})
    blob = private(GIF_CIPHER, mime="image/gif")

    result = env.copy_one(blob)

    assert env.uploader.calls[0].body == GIF_BYTES
    assert result.public.scrubbed is False, "nothing was stripped, so do not claim it"
    assert result.public.mime == "image/gif"


def test_the_stages_of_one_copy_are_observable_in_order(tmp_path):
    env = build(tmp_path)
    blob = private()

    env.copy_one(blob)

    assert [stage for _sha, stage in env.stages] == [
        CopyStage.CHECKING,
        CopyStage.FETCHING,
        CopyStage.DECRYPTING,
        CopyStage.PREPARING,
        CopyStage.UPLOADING,
        CopyStage.RECORDING,
        CopyStage.DONE,
    ]
    assert {sha for sha, _stage in env.stages} == {blob.sha256}


def test_the_finished_signal_carries_the_result(tmp_path):
    env = build(tmp_path)
    blob = private()

    result = env.copy_one(blob)

    assert env.finished == [(blob.sha256, result)]


# --------------------------------------------------------------------- #
# No key may escape                                                     #
# --------------------------------------------------------------------- #

def test_the_key_never_reaches_the_ledger_on_disk(tmp_path):
    env = build(tmp_path)
    blob = private()

    env.copy_one(blob)

    text = env.ledger_text()
    assert text, "the ledger really was written"
    assert KEY not in text
    entry = json.loads(text)["public"][0]
    assert not any("key" in field.lower() for field in entry)


def test_the_key_never_reaches_the_upload_the_job_name_or_a_signal(tmp_path):
    env = build(tmp_path)
    blob = private()

    env.copy_one(blob)

    assert KEY not in env.uploader.calls[0].name
    assert KEY.encode() not in env.uploader.calls[0].body
    assert all(KEY not in str(stage) for stage in env.stages)


def test_a_public_blob_has_nowhere_to_hold_a_key(tmp_path):
    env = build(tmp_path)

    result = env.copy_one(private())

    assert not hasattr(result.public, "key_hex")
    assert not any("key" in field for field in vars(result.public))


# --------------------------------------------------------------------- #
# Step 2: failover across the servers that might hold it                #
# --------------------------------------------------------------------- #

def test_the_next_server_is_asked_when_the_first_refuses(tmp_path):
    env = build(tmp_path, blobs={f"{SERVER_TWO}/{CIPHER_SHA}": CIPHER})
    env.fetcher.errors[f"{SERVER_ONE}/{CIPHER_SHA}"] = "503"
    blob = private(servers=(SERVER_ONE, SERVER_TWO))

    result = env.copy_one(blob)

    assert result.ok
    assert env.fetcher.requests == [
        f"{SERVER_ONE}/{CIPHER_SHA}", f"{SERVER_TWO}/{CIPHER_SHA}",
    ]


def test_every_server_is_tried_before_giving_up(tmp_path):
    env = build(tmp_path, blobs={})
    blob = private(servers=(SERVER_ONE, SERVER_TWO, CDN))

    result = env.copy_one(blob)

    assert not result.ok
    assert len(env.fetcher.requests) == 3
    assert env.uploader.calls == []


def test_a_server_returning_the_wrong_bytes_is_failed_over_not_decrypted(tmp_path):
    # Content addressing is the only reason it is safe to ask a server
    # the user did not personally choose.
    env = build(tmp_path, blobs={
        f"{SERVER_ONE}/{CIPHER_SHA}": sealed(GIF_BYTES),
        f"{SERVER_TWO}/{CIPHER_SHA}": CIPHER,
    })
    blob = private(servers=(SERVER_ONE, SERVER_TWO))

    result = env.copy_one(blob)

    assert result.ok
    assert env.uploader.calls[0].body.startswith(b"\x89PNG")


def test_a_server_the_media_policy_refuses_is_never_asked(tmp_path):
    env = build(tmp_path, blobs={f"{SERVER_TWO}/{CIPHER_SHA}": CIPHER})
    blob = private(servers=("http://plain.example", SERVER_TWO))

    result = env.copy_one(blob)

    assert result.ok
    assert env.fetcher.requests == [f"{SERVER_TWO}/{CIPHER_SHA}"]


def test_the_same_server_written_twice_is_asked_once(tmp_path):
    env = build(tmp_path, blobs={})
    blob = private(servers=(SERVER_ONE, SERVER_ONE + "/", f"{SERVER_ONE}/x"))

    env.copy_one(blob)

    assert env.fetcher.requests == [f"{SERVER_ONE}/{CIPHER_SHA}"]


def test_a_file_that_names_no_usable_server_fails_without_a_request(tmp_path):
    env = build(tmp_path)
    blob = private(servers=())

    result = env.copy_one(blob)

    assert not result.ok
    assert env.fetcher.requests == []
    assert env.uploader.calls == []
    assert "where it is stored" in result.reason


# --------------------------------------------------------------------- #
# Step 3: decryption                                                    #
# --------------------------------------------------------------------- #

def test_a_key_that_does_not_open_the_file_stops_it_and_uploads_nothing(tmp_path):
    blob = private(key=OTHER_KEY)
    env = build(tmp_path, records=[record_for(blob)])
    before = snapshot(env, blob)

    result = env.copy_one(blob)

    assert not result.ok
    assert env.uploader.calls == []
    assert len(env.on_disk()) == 0
    assert_original_intact(env, blob, before)


def test_the_decrypt_failure_message_carries_no_key_and_no_ciphertext(tmp_path):
    blob = private(key=OTHER_KEY)
    env = build(tmp_path)

    result = env.copy_one(blob)

    assert OTHER_KEY not in result.reason
    assert KEY not in result.reason
    assert CIPHER.hex()[:32] not in result.reason
    assert "could not be opened" in result.reason


def test_bytes_that_are_not_an_envelope_at_all_stop_the_file(tmp_path):
    junk = b"not an envelope, just some bytes"
    env = build(tmp_path, blobs={f"{SERVER_ONE}/{sha_of(junk)}": junk})
    blob = PrivateBlob(
        sha256=sha_of(junk), key_hex=KEY, servers=[SERVER_ONE], mime="image/png",
    )

    result = env.copy_one(blob)

    assert not result.ok
    assert env.uploader.calls == []


# --------------------------------------------------------------------- #
# Step 4: the scrub refusal has no way around it                        #
# --------------------------------------------------------------------- #

def test_something_that_is_not_an_image_is_refused_and_never_uploaded(tmp_path):
    blob = private(TEXT_CIPHER, mime="text/plain")
    env = build(
        tmp_path,
        blobs={f"{SERVER_ONE}/{TEXT_CIPHER_SHA}": TEXT_CIPHER},
        records=[record_for(blob)],
    )
    before = snapshot(env, blob)

    result = env.copy_one(blob)

    assert not result.ok
    assert env.uploader.calls == []
    assert len(env.on_disk()) == 0
    assert_original_intact(env, blob, before)


def test_an_svg_is_refused_however_it_describes_itself(tmp_path):
    blob = private(SVG_CIPHER, mime="image/png")
    env = build(tmp_path, blobs={f"{SERVER_ONE}/{SVG_CIPHER_SHA}": SVG_CIPHER})

    result = env.copy_one(blob)

    assert not result.ok
    assert env.uploader.calls == []


def test_the_refusal_says_what_was_wrong_with_the_file(tmp_path):
    blob = private(TEXT_CIPHER, mime="text/plain")
    env = build(tmp_path, blobs={f"{SERVER_ONE}/{TEXT_CIPHER_SHA}": TEXT_CIPHER})

    result = env.copy_one(blob)

    assert "not published" in result.reason
    assert "image" in result.reason
    assert TEXT_BYTES.decode() not in result.reason


# --------------------------------------------------------------------- #
# Step 5: the upload                                                    #
# --------------------------------------------------------------------- #

def test_an_upload_failure_stops_the_file_and_records_nothing(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])
    env.uploader.fail_with = "the server is full"
    before = snapshot(env, blob)

    result = env.copy_one(blob)

    assert not result.ok
    assert "the server is full" in result.reason
    assert len(env.on_disk()) == 0
    assert_original_intact(env, blob, before)


def test_an_answer_for_another_job_is_ignored(tmp_path):
    env = build(tmp_path, auto=False)
    blob = private()
    answers: list = []
    env.maker.make_public_copy(blob, on_done=answers.append)

    env.uploader.upload_finished.emit("some other upload", make_media("a" * 64))
    env.uploader.upload_failed.emit("some other upload", "unrelated")

    assert answers == []
    assert len(env.on_disk()) == 0
    env.uploader.finish()
    assert answers[0].ok


def test_a_server_confirming_a_different_file_is_not_recorded(tmp_path):
    # The hash is how a copy is revoked. Recording a number that is not
    # ours would record something the user cannot reach.
    env = build(tmp_path, auto=False)
    blob = private()
    answers: list = []
    env.maker.make_public_copy(blob, on_done=answers.append)

    env.uploader.finish(media=make_media("b" * 64))

    assert not answers[0].ok
    assert len(env.on_disk()) == 0


def test_an_upload_with_no_usable_address_is_not_recorded(tmp_path):
    env = build(tmp_path, auto=False)
    blob = private()
    answers: list = []
    env.maker.make_public_copy(blob, on_done=answers.append)
    sha = sha_of(env.uploader.calls[-1].body)

    env.uploader.finish(media=SimpleNamespace(hash=sha, url="", urls=[]))

    assert not answers[0].ok
    assert len(env.on_disk()) == 0


def test_a_mismatched_primary_url_falls_back_to_the_canonical_address(tmp_path):
    env = build(tmp_path, auto=False)
    blob = private()
    answers: list = []
    env.maker.make_public_copy(blob, on_done=answers.append)
    sha = sha_of(env.uploader.calls[-1].body)

    env.uploader.finish(media=SimpleNamespace(
        hash=sha,
        url=f"{CDN}/{'0' * 64}.png",
        urls=[{"server": CDN, "url": f"{CDN}/{'0' * 64}.png"}],
    ))

    assert answers[0].ok
    assert answers[0].public.url == f"{CDN}/{sha}"


# --------------------------------------------------------------------- #
# Step 6: an unlisted public blob is the failure that matters           #
# --------------------------------------------------------------------- #

def test_a_ledger_that_cannot_be_written_fails_the_whole_operation(tmp_path):
    obstruction = tmp_path / "in-the-way"
    obstruction.write_text("not a directory", encoding="utf-8")
    blob = private()
    env = build(
        tmp_path,
        ledger=PublicLedger(path=obstruction / "media_public.json"),
        records=[record_for(blob)],
    )
    before = snapshot(env, blob)

    result = env.copy_one(blob)

    assert not result.ok
    assert "could not be saved" in result.reason
    assert env.records.writes == [], "no pointer at a copy that is not listed"
    assert_original_intact(env, blob, before)


def test_a_ledger_owned_by_a_newer_build_fails_the_operation(tmp_path):
    path = tmp_path / "media_public.json"
    path.write_text(json.dumps({"version": 99, "public": []}), encoding="utf-8")
    env = build(tmp_path, ledger=PublicLedger(path=path))

    result = env.copy_one(private())

    assert not result.ok
    assert json.loads(path.read_text(encoding="utf-8"))["public"] == []


def test_a_failed_ledger_write_stops_the_run_before_the_next_upload(tmp_path):
    obstruction = tmp_path / "in-the-way"
    obstruction.write_text("not a directory", encoding="utf-8")
    first = private()
    second = private(GIF_CIPHER, mime="image/gif")
    env = build(
        tmp_path,
        ledger=PublicLedger(path=obstruction / "media_public.json"),
        blobs={
            f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
            f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER,
        },
    )

    answers = env.ensure([first, second])

    assert answers[0].blobs is None
    assert len(env.uploader.calls) == 1, "the second copy never started"
    assert len(env.fetcher.requests) == 1


def test_every_copy_is_committed_before_the_next_upload_starts(tmp_path):
    first = private()
    second = private(GIF_CIPHER, mime="image/gif")
    env = build(tmp_path, blobs={
        f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
        f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER,
    })

    env.ensure([first, second])

    kinds = [entry[0] for entry in env.log]
    assert kinds == ["upload", "record", "upload", "record"]


# --------------------------------------------------------------------- #
# Step 7: the pointer, merged into the record as it reads live          #
# --------------------------------------------------------------------- #

def test_the_pointer_is_merged_into_the_record_as_it_reads_now(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])
    # Renamed on another device while this ran. Writing back the record
    # this session started with would undo that.
    env.records.records[blob.sha256]["name"] = "renamed elsewhere.png"
    env.records.records[blob.sha256]["folder"] = "/moved"

    result = env.copy_one(blob)

    written = env.records.writes[0][1]
    assert written["name"] == "renamed elsewhere.png"
    assert written["folder"] == "/moved"
    assert written["publicCopy"] == {
        "hash": result.public.sha256,
        "servers": [CDN],
        "uploadedAt": NOW,
    }
    assert result.pointer_recorded is True


def test_the_pointer_write_leaves_the_key_and_the_hash_alone(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])

    env.copy_one(blob)

    written = env.records.writes[0][1]
    assert written["encryptionKey"] == KEY
    assert written["hash"] == blob.sha256
    assert written["size"] == blob.size
    assert not written.get("deleted")


def test_a_record_that_cannot_be_re_read_leaves_the_copy_published(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])
    env.records.unreadable.add(blob.sha256)
    before = snapshot(env, blob)

    result = env.copy_one(blob)

    assert result.ok, "the ledger decides, not the pointer"
    assert result.pointer_recorded is False
    assert env.records.writes == [], "a record that could not be read is not invented"
    assert_original_intact(env, blob, before)


def test_a_file_deleted_while_the_copy_ran_is_not_resurrected(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob, deleted=True)])

    result = env.copy_one(blob)

    assert result.ok
    assert result.pointer_recorded is False
    assert env.records.writes == []


def test_a_record_filed_under_another_file_is_never_written(tmp_path):
    blob = private()
    stray = record_for(blob)
    stray["hash"] = "9" * 64
    env = build(tmp_path)
    env.records.records[blob.sha256] = stray

    result = env.copy_one(blob)

    assert result.ok
    assert env.records.writes == []


def test_a_pointer_that_will_not_save_is_not_a_failed_publish(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])
    env.records.write_ok = False

    result = env.copy_one(blob)

    assert result.ok
    assert result.pointer_recorded is False
    assert env.on_disk().get(result.public.sha256) is not None


def test_a_maker_with_no_record_store_still_publishes(tmp_path):
    env = build(tmp_path, with_records=False)

    result = env.copy_one(private())

    assert result.ok
    assert result.pointer_recorded is False
    assert env.records.reads == []


def test_merging_a_pointer_never_invents_a_record():
    public = PublicBlob(sha256="a" * 64, url=f"{CDN}/{'a' * 64}")
    assert _merge_public_copy(None, public, "b" * 64) is None
    assert _merge_public_copy("not a record", public, "b" * 64) is None
    assert _merge_public_copy({"hash": "c" * 64}, public, "b" * 64) is None


# --------------------------------------------------------------------- #
# ensure_public_copies: consent, order, cancellation                    #
# --------------------------------------------------------------------- #

def test_files_that_are_already_public_need_no_warning_and_no_upload(tmp_path):
    env = build(tmp_path)
    blob = private()
    live = PublicBlob(
        sha256="c" * 64, url=f"{CDN}/{'c' * 64}", source_hash=blob.sha256,
    )
    env.ledger.record(live)

    answers = env.ensure([blob])

    assert env.consent_calls == [], "nothing new is published, so nothing to ask"
    assert answers[0].blobs == [live]
    assert env.uploader.calls == []


def test_declining_the_warning_uploads_nothing_and_records_nothing(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)])
    before = snapshot(env, blob)

    answers = env.ensure([blob], consent=False)

    assert answers[0].blobs is None
    assert answers[0].cancelled is True
    assert answers[0].minted == []
    assert env.fetcher.requests == []
    assert env.uploader.calls == []
    assert not env.ledger_path.exists()
    assert_original_intact(env, blob, before)


def test_the_warning_names_the_files_that_would_be_copied_once_each(tmp_path):
    env = build(tmp_path)
    blob = private()
    public_already = private(GIF_CIPHER, mime="image/gif")
    env.ledger.record(PublicBlob(
        sha256="c" * 64,
        url=f"{CDN}/{'c' * 64}",
        source_hash=public_already.sha256,
    ))

    env.ensure([blob, public_already, blob], consent=False)

    assert len(env.consent_calls) == 1
    assert [b.sha256 for b in env.consent_calls[0]] == [blob.sha256]


def test_the_rewritten_list_is_public_blobs_in_the_callers_order(tmp_path):
    first = private()
    second = private(GIF_CIPHER, mime="image/gif")
    env = build(tmp_path, blobs={
        f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
        f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER,
    })

    answers = env.ensure([first, second])

    published = answers[0].blobs
    assert [b.source_hash for b in published] == [first.sha256, second.sha256]
    assert all(isinstance(b, PublicBlob) for b in published)
    assert len(answers[0].minted) == 2


def test_the_same_picture_twice_is_one_copy_used_twice(tmp_path):
    blob = private()
    env = build(tmp_path)

    answers = env.ensure([blob, blob])

    assert len(env.uploader.calls) == 1
    assert answers[0].blobs[0] is answers[0].blobs[1]


def test_one_picture_stored_twice_under_two_keys_still_publishes(tmp_path):
    # Two private files, two hashes, two keys, one identical photo. The
    # ledger is keyed by the public hash, so the second copy lands on
    # top of the first and the trail back to a source is last writer
    # wins. Neither picture may drop out of the publish because of it.
    other = sealed(PNG_BYTES, OTHER_KEY)
    first = private()
    second = private(other, key=OTHER_KEY)
    assert first.sha256 != second.sha256
    env = build(tmp_path, blobs={
        f"{SERVER_ONE}/{first.sha256}": CIPHER,
        f"{SERVER_ONE}/{second.sha256}": other,
    })

    answers = env.ensure([first, second])

    published = answers[0].blobs
    assert published is not None
    assert published[0].sha256 == published[1].sha256, "the same picture"
    assert env.on_disk().get(published[0].sha256) is not None


def test_per_item_progress_is_observable(tmp_path):
    first = private()
    second = private(GIF_CIPHER, mime="image/gif")
    env = build(tmp_path, blobs={
        f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
        f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER,
    })

    env.ensure([first, second])

    assert env.runs == [(0, 2), (1, 2), (2, 2)]
    assert {sha for sha, _stage in env.stages} == {first.sha256, second.sha256}


def test_cancelling_before_the_first_upload_leaves_nothing_behind(tmp_path):
    blob = private()
    env = build(tmp_path, records=[record_for(blob)], auto=False)
    env.fetcher.park = True
    before = snapshot(env, blob)
    answers: list = []
    env.maker.ensure_public_copies(
        [blob], consent=lambda needed: True, on_done=answers.append,
    )

    env.maker.cancel()

    assert answers[0].blobs is None
    assert answers[0].cancelled is True
    assert answers[0].minted == []
    assert env.uploader.calls == []
    assert not env.ledger_path.exists()
    assert_original_intact(env, blob, before)


def test_a_cancelled_file_stops_showing_progress(tmp_path):
    env = build(tmp_path)
    env.fetcher.park = True
    blob = private()
    env.maker.ensure_public_copies(
        [blob], consent=lambda needed: True, on_done=lambda outcome: None,
    )

    env.maker.cancel()

    assert env.stages[-1] == (blob.sha256, CopyStage.CANCELLED)


def test_a_cancelled_copy_answers_its_caller_exactly_once(tmp_path):
    env = build(tmp_path)
    env.fetcher.park = True
    blob = private()
    answers: list = []
    env.maker.make_public_copy(blob, on_done=answers.append)

    env.maker.cancel()
    env.fetcher.deliver()

    assert len(answers) == 1
    assert answers[0].cancelled is True and not answers[0].ok
    assert env.uploader.calls == []


def test_a_cancelled_file_is_not_reported_as_a_failure(tmp_path):
    env = build(tmp_path)
    env.fetcher.park = True
    answers: list = []
    env.maker.ensure_public_copies(
        [private()], consent=lambda needed: True, on_done=answers.append,
    )

    env.maker.cancel()

    assert answers[0].cancelled is True
    assert answers[0].failures == []


def test_a_late_answer_after_a_cancel_publishes_nothing(tmp_path):
    env = build(tmp_path)
    env.fetcher.park = True
    blob = private()
    env.maker.ensure_public_copies(
        [blob], consent=lambda needed: True, on_done=lambda outcome: None,
    )
    env.maker.cancel()

    env.fetcher.deliver()

    assert env.uploader.calls == []
    assert not env.ledger_path.exists()


def test_cancelling_mid_run_carries_the_upload_in_flight_into_the_ledger(tmp_path):
    # The bytes are already on a server. Dropping the callback now is
    # what leaves a public blob nobody can list and nobody can revoke.
    first = private()
    second = private(GIF_CIPHER, mime="image/gif")
    env = build(tmp_path, auto=False, blobs={
        f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
        f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER,
    })
    answers: list = []
    env.maker.ensure_public_copies(
        [first, second], consent=lambda needed: True, on_done=answers.append,
    )
    assert len(env.uploader.calls) == 1

    env.maker.cancel()
    assert answers == [], "the run is not over while bytes are in the air"
    env.uploader.finish()

    outcome = answers[0]
    assert outcome.blobs is None and outcome.cancelled is True
    assert len(outcome.minted) == 1
    assert env.on_disk().get(outcome.minted[0].sha256) is not None
    assert len(env.uploader.calls) == 1, "the second copy never started"


def test_a_partial_failure_leaves_the_earlier_copies_listed(tmp_path):
    first = private()
    second = private(TEXT_CIPHER, mime="text/plain")
    third = private(GIF_CIPHER, mime="image/gif")
    env = build(tmp_path, blobs={
        f"{SERVER_ONE}/{CIPHER_SHA}": CIPHER,
        f"{SERVER_ONE}/{TEXT_CIPHER_SHA}": TEXT_CIPHER,
        f"{SERVER_ONE}/{GIF_CIPHER_SHA}": GIF_CIPHER,
    })

    answers = env.ensure([first, second, third])

    outcome = answers[0]
    assert outcome.blobs is None
    assert outcome.cancelled is False, "a failure is not a cancellation"
    assert len(outcome.minted) == 1
    assert [f.source_hash for f in outcome.failures] == [second.sha256]
    on_disk = env.on_disk()
    assert on_disk.copy_of(first.sha256) is not None, "not orphaned"
    assert on_disk.copy_of(third.sha256) is None, "never started"
    assert len(env.uploader.calls) == 1


def test_a_second_run_while_one_is_active_is_refused(tmp_path):
    env = build(tmp_path)
    env.fetcher.park = True
    env.maker.ensure_public_copies(
        [private()], consent=lambda needed: True, on_done=lambda outcome: None,
    )

    answers: list = []
    env.maker.ensure_public_copies(
        [private(GIF_CIPHER)], consent=lambda needed: True, on_done=answers.append,
    )

    assert answers[0].blobs is None
    assert answers[0].cancelled is False
    assert len(env.fetcher.requests) == 1


def test_a_second_copy_while_one_is_active_is_refused(tmp_path):
    env = build(tmp_path)
    env.fetcher.park = True
    env.maker.make_public_copy(private(), on_done=lambda result: None)

    answers: list = []
    env.maker.make_public_copy(private(GIF_CIPHER), on_done=answers.append)

    assert not answers[0].ok
    assert len(env.fetcher.requests) == 1


def test_a_revoked_copy_is_made_again_rather_than_reported_as_published(tmp_path):
    # The pointer on the record outlives the copy it names. Trusting it
    # would claim the file is published and then refuse to republish it,
    # which is the worst of both.
    env = build(tmp_path)
    blob = private()
    answers = env.ensure([blob])
    minted = answers[0].blobs[0]

    env.ledger.forget(minted.sha256)
    again = env.ensure([private(public_copy_hash=minted.sha256)])

    assert again[0].blobs is not None
    assert len(env.uploader.calls) == 2
    assert len(env.consent_calls) == 2, "a second copy is a second decision"


# --------------------------------------------------------------------- #
# The private original, across every path                               #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "reuse", "success", "no-servers", "fetch-failed", "wrong-bytes",
    "bad-key", "not-an-image", "upload-failed", "ledger-failed",
])
def test_the_private_original_is_untouched_whatever_happens(tmp_path, path):
    envelope = TEXT_CIPHER if path == "not-an-image" else CIPHER
    blob = private(
        envelope,
        key=OTHER_KEY if path == "bad-key" else KEY,
        servers=() if path == "no-servers" else (SERVER_ONE,),
    )
    served = {}
    if path == "wrong-bytes":
        served[f"{SERVER_ONE}/{blob.sha256}"] = sealed(GIF_BYTES)
    elif path != "fetch-failed":
        served[f"{SERVER_ONE}/{blob.sha256}"] = envelope

    ledger = None
    if path == "ledger-failed":
        obstruction = tmp_path / "in-the-way"
        obstruction.write_text("not a directory", encoding="utf-8")
        ledger = PublicLedger(path=obstruction / "media_public.json")

    env = build(tmp_path, blobs=served, records=[record_for(blob)], ledger=ledger)
    if path == "upload-failed":
        env.uploader.fail_with = "the server is full"
    if path == "reuse":
        env.ledger.record(PublicBlob(
            sha256="c" * 64, url=f"{CDN}/{'c' * 64}", source_hash=blob.sha256,
        ))
    before = snapshot(env, blob)

    result = env.copy_one(blob)

    assert result is not None
    if path == "success":
        # The one permitted write: the pointer, and nothing else.
        assert len(env.records.writes) == 1
        written = env.records.writes[0][1]
        assert set(written) - set(before["record"]) == {"publicCopy"}
        for field, value in before["record"].items():
            assert written[field] == value
    else:
        assert_original_intact(env, blob, before)


@pytest.mark.parametrize("path", ["success", "bad-key", "not-an-image"])
def test_no_path_ever_uploads_the_ciphertext(tmp_path, path):
    envelope = TEXT_CIPHER if path == "not-an-image" else CIPHER
    blob = private(envelope, key=OTHER_KEY if path == "bad-key" else KEY)
    env = build(tmp_path, blobs={f"{SERVER_ONE}/{blob.sha256}": envelope})

    env.copy_one(blob)

    for call in env.uploader.calls:
        assert call.body != envelope
        assert not call.body.startswith(bytes([2]))
