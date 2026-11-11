# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publishing an accepted submission (M-4 M13, Q1): the article built as
the web app builds it, and the run that puts its pictures up, signs it,
sends it and reports it."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from nostr.standup import crypto as scrypto
from nostr.standup import formats, publishing, views
from nostr.ui.standup_publish import AcceptedPublish, PublishPorts, Stage
from tests.standup_following_fakes import FollowWorld
from tests.standup_vectors import VECTORS, pk

PLAIN = b"\x89PNG picture bytes"
SERVER = "https://media.example"


def _picture():
    key = scrypto.new_secret_key()
    encryptor = scrypto.MediaEncryptor(key)
    cipher = encryptor.update(PLAIN) + encryptor.finish()
    entry = scrypto.media_entry(key=key, encryptor=encryptor, name="cafe.png",
                                mime="image/png", servers=["https://blossom.example"])
    return entry, cipher


def test_the_article_credits_its_people_and_names_its_pictures_in_public():
    entry, _cipher = _picture()
    ref, coauthor = entry["ref"], pk("contributor")
    revision = {
        "work": {"d": "ein-jahr", "title": "Ein Jahr", "summary": "S", "image": ref,
                 "tags": ["bitcoin"], "lang": "de", "content": f"Text ![]({ref})",
                 "authors": [{"pubkey": coauthor, "relay": "wss://r.example"}],
                 "extra_tags": [["imeta", f"url {ref}", f"x {entry['cipher_sha256']}"],
                                ["zap", coauthor, "", "30"], ["expiration", "1"],
                                ["p", pk("recipient"), "", "author"]]},
        "media": [entry]}
    url = f"{SERVER}/{entry['plain_sha256']}.png"
    event = publishing.published_template(
        revision, writer=pk("writer"), d="ein-jahr", media_urls={ref: url},
        published_at=100, created_at=200)
    tags = event["tags"]
    assert event["content"] == f"Text ![]({url})" and ["image", url] in tags
    assert ["imeta", f"url {url}", f"x {entry['plain_sha256']}"] in tags
    authors = [t[1] for t in tags if t[0] == "p" and t[3:] == ["author"]]
    assert authors == [pk("writer"), coauthor]             # never one the work carried
    assert ["zap", coauthor, "", "30"] in tags and not any(t[0] == "expiration" for t in tags)
    assert ["l", "de", "ISO-639-1"] in tags and ["published_at", "100"] in tags
    assert "standup-media:" not in str(event)
    with pytest.raises(publishing.NotPublishable):
        publishing.published_template(revision, writer=pk("writer"), d="ein-jahr",
                                      media_urls={}, published_at=1, created_at=1)


class FakeStore(QObject):
    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)

    def __init__(self, *, refuse: bool = False) -> None:
        super().__init__()
        self.refuse, self.uploaded, self.deleted, self.batches = refuse, [], [], []

    def target_servers(self):
        return [SERVER]

    def cached_bytes(self, _sha):
        return None

    def files(self):
        return {}

    def upload_bytes(self, body, *, name, mime_type):
        self.uploaded.append((body, name, mime_type))
        sha = hashlib.sha256(body).hexdigest()
        if self.refuse:
            self.upload_failed.emit(name, "refused")
            return
        url = f"{SERVER}/{name}"
        self.upload_finished.emit(name, SimpleNamespace(
            hash=sha, url=url, urls=[{"server": SERVER, "url": url}]))

    def delete_file(self, sha, *, servers=()):
        self.deleted.append((sha, tuple(servers)))

    def delete_on_servers(self, pairs, on_done):
        self.batches.append(list(pairs))
        on_done(list(pairs))


class FakeJob(QObject):
    """HeldPublish's side the run uses: signs with the account's key, and
    sends only once released; ``took`` says whether a relay takes it."""

    signed = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, key, unsigned, here, took) -> None:
        super().__init__()
        self.key, self.unsigned_event, self.here, self.took = key, unsigned, here, took
        self.signed_event, self.sent, self.cancelled = None, [], False
        self.failure_words = ""

    def start(self):
        def done(event):
            self.signed_event = event
            self.signed.emit(event["id"])
        self.key.sign_event(self.unsigned_event, done, self.failed.emit)

    def release(self):
        self.send_again()

    def send_again(self):
        self.sent.append(self.signed_event)
        self.completed.emit([("wss://relay.example", self.took[0], "")])
        return True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def accepted():
    """An accepted submission with one picture, read by its writer."""
    world = FollowWorld()
    writer = world.account()
    entry, cipher = _picture()
    prepared, _entry = world.submit(writer, d="ein-jahr", content=f"Text ![]({entry['ref']})",
                                    media=(entry,))
    sid = prepared.body["submission_id"]
    world.decide(prepared, "accept")
    following = world.following(writer)
    following.open(sid)
    world.settle()
    return SimpleNamespace(world=world, writer=writer, sid=sid, following=following,
                           cipher=cipher, entry=entry)


def _run(case, *, store=None, took=(True,), reread_hash=None, key_here=False, profile=None):
    store = store or FakeStore()
    jobs = []
    following, api = case.following, case.writer.api

    def reread(on_view, on_failure):
        view = following.row(case.sid).view
        if reread_hash is not None:
            from dataclasses import replace
            view = replace(view, revision_hash=reread_hash)
        on_view(view)

    def job(profile, unsigned, here):
        jobs.append(FakeJob(case.writer.key, unsigned, here, list(took)))
        return jobs[-1]

    view = following.row(case.sid).view
    ports = PublishPorts(
        api=api, media_keys=case.writer.media_keys, store=store,
        fetch=lambda url, on_success, on_failure: on_success(case.cipher)
        if url.endswith(case.entry["cipher_sha256"]) else on_failure("404"),
        job=job, key_here=lambda: key_here, serve=following.serve, reread=reread,
        clock=case.world.clock)
    run = AcceptedPublish(sid=case.sid, profile=profile or case.writer.profile,
                          version=following.reading.latest(case.sid),
                          accepted=following.reading.accepted(case.sid),
                          revision_hash=view.revision_hash, publication="Magazin", ports=ports)
    return run, store, jobs


def test_publishing_puts_the_picture_up_in_plain_signs_sends_and_reports(accepted):
    run, store, jobs = _run(accepted)
    assert accepted.following.reading.accepted(accepted.sid).revision == 1
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.DONE, (run.title, run.words)
    assert [(body, mime) for body, _name, mime in store.uploaded] == [(PLAIN, "image/png")]
    event = jobs[0].signed_event
    assert f"{SERVER}/{accepted.entry['plain_sha256']}.png" in event["content"]
    assert accepted.world.record(accepted.sid).state == "published"
    assert run.naddr.startswith("naddr1") and store.deleted == []


def test_no_relay_took_it_so_try_again_sends_the_same_signed_article(accepted):
    run, store, jobs = _run(accepted, took=(False,))
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.NOT_STORED
    assert accepted.world.record(accepted.sid).state == "accepted"
    jobs[0].took[0] = True
    run.retry()
    accepted.world.settle()
    assert len(jobs) == 1 and jobs[0].sent == [jobs[0].signed_event] * 2
    assert run.stage is Stage.DONE


def test_another_work_at_the_link_name_is_said_in_one_sentence_and_the_name_kept(accepted):
    # Contract 12.4 One work per address; F5A publishedD: a writer's own work
    # keeps its d (another d would not be this work's), so it is only said.
    from nostr.standup.errors import Code, StandupError
    run, _store, jobs = _run(accepted)

    def taken(sid, *, naddr, event, on_success, on_failure):
        on_failure(StandupError(code=Code.CONFLICT, status=409,
                                detail={"reason": "address_taken"}, own=True))

    run._ports = replace(run._ports, api=SimpleNamespace(published=taken))
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.NOT_REPORTED and "another article under this link name" in run.words
    assert formats.tag_value(jobs[0].signed_event, "d") == "ein-jahr"


def test_a_submission_that_changed_meanwhile_is_not_published_and_its_picture_comes_down(
        accepted):
    run, store, jobs = _run(accepted, reread_hash="ab" * 32)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.FAILED and jobs[0].sent == [] and jobs[0].cancelled
    assert store.deleted == [(accepted.entry["plain_sha256"], (SERVER,))]
    assert run.signed_article is None


def test_a_picture_the_servers_refuse_publishes_nothing(accepted):
    run, store, jobs = _run(accepted, store=FakeStore(refuse=True))
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.FAILED and jobs == []


def test_too_long_for_the_signer_app_with_the_key_here_offers_it_once(accepted, monkeypatch):
    # Q1: explicit and for this article only; never without a key here.
    from nostr import signer_limits
    from nostr.ui import standup_publish_job
    for module in (signer_limits, standup_publish_job):     # the signer app's limit
        monkeypatch.setattr(module, "signable", lambda profile, event: False)
    remote = SimpleNamespace(user_pubkey=pk("writer"), signer="remote", bunker_relays=[])
    run, store, jobs = _run(accepted, key_here=True, profile=remote)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.OFFER_HERE and store.uploaded == [] and jobs == []
    run.start(here=True)
    accepted.world.settle()
    assert jobs[0].here and run.stage is Stage.DONE
    run, store, jobs = _run(accepted, key_here=False, profile=remote)
    run.start()
    assert run.stage is Stage.FAILED and store.uploaded == []


def test_a_correction_taken_is_confirmed_alone_then_replaces_the_article_keeping_its_date(
        accepted):
    # M-4 M15: bound to the correction's verified revision, at the address
    # and with the first date of the article as it stands in public.
    from nostr.ui.standup_publish import CorrectionPublish
    first, _store, jobs = _run(accepted)
    first.start()
    accepted.world.settle()
    published = jobs[0].signed_event
    accepted.world.clock.advance(60)
    content = f"Korrigiert ![]({accepted.entry['ref']})"
    accepted.world.correct(accepted.writer, accepted.sid, content, media=[accepted.entry])
    following = accepted.following
    following.refresh()
    accepted.world.settle()
    following.open(accepted.sid)
    accepted.world.settle()
    row = following.row(accepted.sid)
    assert row.situation.name == "CORRECTION"
    version = following.reading.latest(accepted.sid)
    assert version is not None and version.opened.verified.correction    # walked back
    d = next(tag[1] for tag in published["tags"] if tag[0] == "d")
    # Contract 10: the address rests on the reported event itself.
    assert following.reading.published(accepted.sid).address == f"30023:{pk('writer')}:{d}"
    ports = first._ports
    confirmed = []

    def confirm(confirms, revision_hash, done):
        confirmed.append((tuple(confirms), revision_hash))
        following.confirm(accepted.sid, confirms=confirms, revision_hash=revision_hash,
                          split_rumor_id=None, on_done=done)

    from nostr.standup.publishing import Accepted
    run = CorrectionPublish(
        published=following.reading.published_event(accepted.sid), sid=accepted.sid,
        profile=accepted.writer.profile, version=version,
        accepted=Accepted(revision=2, split_rumor_id=None, actor="", seq=0),
        revision_hash=row.view.revision_hash, publication="Magazin",
        ports=replace(ports, confirm=confirm))
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.DONE, (run.title, run.words)
    assert confirmed == [(("correction",), row.view.revision_hash)]
    event = jobs[-1].signed_event
    assert ["published_at", publishing_tag(published)] in event["tags"]
    assert event["created_at"] > published["created_at"] and "Korrigiert" in event["content"]
    assert following.row(accepted.sid).situation.name != "CORRECTION"


def publishing_tag(event):
    return next(tag[1] for tag in event["tags"] if tag[0] == "published_at")


def test_a_published_works_address_rests_on_its_own_event_as_the_vector_says():
    # Contract 10, The address of a published work (vector published_address):
    # whoever reported it, the work's own event proves its address.
    vector = VECTORS["published_address"]
    sid, publication = vector["submission_id"], vector["publication"]

    def signed(report):
        return views.SignedRecord(event=report["event"], body=report["body"])

    def address(report):
        found = publishing.work_address(signed(report), sid, writer=vector["writer"],
                                        coordinate_hash=vector["coordinate_hash"],
                                        publication=publication)
        return found.address if found is not None else None

    for name, case in vector["counts"].items():
        assert address(case["report"]) == case["address"], name
    for name, report in vector["must_fail"].items():
        assert address(report) is None, name
    placed = vector["placed"]

    def section(report):
        return publishing.placed_section(signed(report), sid, placed["address"], publication)

    assert section(placed["report"]) == placed["section"]
    for name, report in vector["placed_must_fail"].items():
        assert section(report) is None, name


def test_after_publication_the_encrypted_files_go_but_one_another_submission_names_stays():
    # M-4 M14 blob deletion: quietly, from the verified revisions' signed
    # blob lists and the writer's servers; only a file's last user removes it.
    from nostr import events
    from nostr.bech32 import encode_naddr
    from nostr.standup.cleanup import SETTINGS_KEY, Cleanups
    from nostr.ui.standup_cleanup import BlobCleanup
    from tests.standup_vectors import sk
    world = FollowWorld()
    writer = world.account()
    shared, own = _picture()[0], _picture()[0]
    world.server.blob_servers.everywhere.update({shared["cipher_sha256"], own["cipher_sha256"]})
    def blob(entry):
        return {"sha256": entry["cipher_sha256"], "size": entry["cipher_size"],
                "servers": ["https://blossom.example"]}

    prepared, _entry = world.submit(writer, d="ein-jahr", media=(shared, own),
                                    blobs=(blob(shared), blob(own)),
                                    content=f"![]({shared['ref']}) ![]({own['ref']})")
    sid = prepared.body["submission_id"]
    world.decide(prepared, "accept")
    article = events.sign_event({"kind": 30023, "created_at": world.clock(), "content": "Text.",
                                 "tags": [["d", "ein-jahr"]]}, sk("writer"))
    reported = []
    writer.api.published(sid, naddr=encode_naddr("ein-jahr", pk("writer"), 30023),
                         event=article, on_success=reported.append, on_failure=reported.append)
    other, _ = world.submit(writer, d="zwei", content=f"![]({shared['ref']})", media=(shared,),
                            blobs=(blob(shared),))
    following = world.following(writer)
    following.open(other.body["submission_id"])         # its revision names the shared file
    world.settle()
    store, kept = FakeStore(), {}
    BlobCleanup(following, writer, SimpleNamespace(user_pubkey=pk("writer"), is_local=True),
                store=store, cleanups=Cleanups(pk("writer"), load=lambda: kept,
                                               save=kept.__setitem__),
                clock=world.clock, parent=following)
    following.open(sid)
    world.settle()
    assert store.batches == [[(own["cipher_sha256"], "https://blossom.example"),
                              (own["cipher_sha256"], SERVER)]]
    assert kept[SETTINGS_KEY][pk("writer")][sid]["over"]
    following.open(sid)
    world.settle()
    assert len(store.batches) == 1                       # recorded: never asked again
