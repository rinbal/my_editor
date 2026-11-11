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

from nostr import events
from nostr.standup import crypto as scrypto
from nostr.standup import formats, publishing, views
from nostr.ui.standup_publish import AcceptedPublish, PublishPorts, Stage
from tests.standup_following_fakes import FollowWorld
from tests.standup_vectors import VECTORS, pk, sk

PLAIN = b"\x89PNG picture bytes"
SERVER = "https://media.example"


def _picture(plain: bytes = PLAIN):
    key = scrypto.new_secret_key()
    encryptor = scrypto.MediaEncryptor(key)
    cipher = encryptor.update(plain) + encryptor.finish()
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


def test_a_correction_changes_only_what_its_sheet_shows():
    # RG-M4-a 1 (probe p2): the Redaktion's correction keeps the text but
    # adds a co-author, swaps the cover and gives the editor every zap.
    # Republished under the writer's key, it keeps the published zaps,
    # credits and carried tags; the cover and the title it changes are
    # said; its text is compared with the article as it stands in public.
    entry, _cipher = _picture()
    ref, editor, stranger = entry["ref"], pk("editor"), pk("recipient")
    picture = f"{SERVER}/{entry['plain_sha256']}.png"
    published = publishing.published_template(
        {"work": {"d": "ein-jahr", "title": "Ein Jahr", "summary": "S", "image": picture,
                  "tags": ["bitcoin"], "lang": "de", "content": f"Text ![]({picture})",
                  "authors": [{"pubkey": pk("contributor"), "relay": ""}],
                  "extra_tags": [["zap", pk("writer"), "", "70"],
                                 ["zap", pk("contributor"), "", "30"], ["r", "https://a.example"]]},
         "media": []},
        writer=pk("writer"), d="ein-jahr", media_urls={}, published_at=100, created_at=200)
    correction = {"work": {"d": "anything-else", "title": "Neuer Titel", "summary": "S",
                           "image": "https://evil.example/cover.jpg", "tags": ["bitcoin"],
                           "lang": "de", "content": f"Text ![]({ref})",
                           "authors": [{"pubkey": stranger, "relay": ""}],
                           "extra_tags": [["zap", editor, "", "100"], ["r", "https://b.example"]]},
                  "media": [entry], "credits": [{"pubkey": editor, "part": "x"}]}
    event = publishing.corrected_template(correction, published, writer=pk("writer"),
                                          media_urls={ref: picture}, created_at=150)
    tags = event["tags"]
    assert [t for t in tags if t[0] == "zap"] == [t for t in published["tags"] if t[0] == "zap"]
    assert sorted(t[1] for t in tags if t[0] == "p") == sorted([pk("writer"), pk("contributor")])
    assert ["r", "https://a.example"] in tags and ["r", "https://b.example"] not in tags
    assert ["d", "ein-jahr"] in tags and ["published_at", "100"] in tags
    assert event["created_at"] == 201 and ["title", "Neuer Titel"] in tags
    assert publishing.correction_changes(correction, published) == [
        ("title", "Neuer Titel"), ("cover", "changed")]
    assert publishing.correction_texts(correction, published) == (
        "Text ![](standup-media)", "Text ![](standup-media)")


def test_a_picture_the_correction_adds_gets_its_tag_and_one_it_removes_loses_it():
    # RG-M4-a 1, refined: the media tags follow the corrected text, and
    # the compare names the change with the web's word for it.
    kept, added = _picture()[0], _picture(PLAIN + b"another")[0]
    kept_url, gone_url = (f"{SERVER}/{kept['plain_sha256']}.png", f"{SERVER}/{'ee' * 32}.png")
    published = publishing.published_template(
        {"work": {"d": "ein-jahr", "title": "Ein Jahr", "summary": "", "image": "", "tags": [],
                  "lang": "", "content": f"![]({kept_url}) ![]({gone_url})", "authors": [],
                  "extra_tags": [["imeta", f"url {kept_url}", "m image/png"],
                                 ["imeta", f"url {gone_url}", "m image/png"]]}, "media": []},
        writer=pk("writer"), d="ein-jahr", media_urls={}, published_at=100, created_at=200)
    added_url = f"{SERVER}/{added['plain_sha256']}.png"
    correction = {"work": {"d": "ein-jahr", "title": "Ein Jahr", "summary": "", "image": "",
                           "tags": [], "lang": "", "authors": [],
                           "content": f"![]({kept['ref']}) ![]({added['ref']})",
                           "extra_tags": [["imeta", f"url {added['ref']}",
                                           f"x {added['cipher_sha256']}"]]},
                  "media": [kept, added]}
    event = publishing.corrected_template(
        correction, published, writer=pk("writer"), created_at=300,
        media_urls={kept["ref"]: kept_url, added["ref"]: added_url})
    imeta = [t for t in event["tags"] if t[0] == "imeta"]
    assert imeta == [["imeta", f"url {added_url}", f"x {added['plain_sha256']}"],
                     ["imeta", f"url {kept_url}", "m image/png"]]
    assert publishing.correction_changes(correction, published) == [("pictures", "")]
    from nostr.ui.standup_correction_sheet import change_words
    assert change_words([("title", "T"), ("pictures", "")]) == "Also changed: title, pictures."


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

    def reread(on_detail, on_failure):
        view = following.row(case.sid).view
        if reread_hash is not None:
            view = replace(view, revision_hash=reread_hash)
        on_detail(SimpleNamespace(submission=view, address_taken=False))

    def held_at(address, on_author, on_failure):
        api.article_author(address, on_author, on_failure)

    def job(profile, unsigned, here, only):
        jobs.append(FakeJob(case.writer.key, unsigned, here, list(took)))
        return jobs[-1]

    view = following.row(case.sid).view
    ports = PublishPorts(
        api=api, media_keys=case.writer.media_keys, store=store,
        fetch=lambda url, on_success, on_failure: on_success(case.cipher)
        if url.endswith(case.entry["cipher_sha256"]) else on_failure("404"),
        job=job, key_here=lambda: key_here, serve=following.serve, reread=reread,
        held_at=held_at, relays_hold=lambda unsigned, done: done(["wss://relay.example"], False),
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

    def taken(sid, *, naddr, event, revision, on_success, on_failure):
        on_failure(StandupError(code=Code.CONFLICT, status=409,
                                detail={"reason": "address_taken"}, own=True))

    run._ports = replace(run._ports, api=SimpleNamespace(published=taken))
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.NOT_REPORTED and "another article under this link name" in run.words
    assert formats.tag_value(jobs[0].signed_event, "d") == "ein-jahr"


def test_the_writer_release_vectors():
    # Contract 12.4, Before the writer's work goes out (vector writer_release).
    vector = VECTORS["writer_release"]

    def may(case):
        held = case["held"]
        credited = None if held is None else publishing.held_credit(held, vector["address"])
        return publishing.writer_may_release(credited, address_taken=case["address_taken"],
                                             writer=vector["writer"])

    assert all(may(case) for case in vector["may"].values())
    assert not any(may(case) for case in vector["must_fail"].values())


def test_nothing_goes_out_over_another_persons_article_at_the_address(accepted):
    # RG-M4-a 2: read right before signing; another writer's article stands
    # at the writer's address, so nothing is signed or sent, the picture
    # stays private, and the sheet offers a question instead of Erneut.
    other = events.sign_event({"kind": 30023, "created_at": 1, "content": "Fremd",
                               "tags": [["d", "ein-jahr"], ["p", pk("contributor"), "", "author"]]},
                              sk("writer"))
    accepted.world.server.articles[f"30023:{pk('writer')}:ein-jahr"] = other
    run, store, jobs = _run(accepted)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.SOMEONE_ELSES and jobs == []
    assert "So that it stays, nothing is published." in run.words
    assert store.deleted == [(accepted.entry["plain_sha256"], (SERVER,))]
    assert accepted.world.record(accepted.sid).state == "accepted"


def test_the_address_is_read_on_every_relay_and_only_clear_ones_take_it():
    # RG-B5-d: the server's index may miss a guest's work the writer's own
    # publication released elsewhere. Right before signing the address is
    # read on every relay it goes to; one that did not answer is left out
    # quietly, and another person's work on one sends nothing anywhere.
    from nostr.ui.standup_publish_job import read_relays
    from tests.outbox_fakes import HandPool, settle
    A, B, C = "wss://a.example", "wss://b.example", "wss://c.example"
    writer = pk("writer")
    unsigned = {"kind": 30023, "pubkey": writer, "tags": [["d", "ein-jahr"]], "content": "x"}
    own = events.sign_event({"kind": 30023, "created_at": 5, "content": "Alt",
                             "tags": [["d", "ein-jahr"]]}, sk("writer"))
    guest = events.sign_event({"kind": 30023, "created_at": 6, "content": "Gast",
                               "tags": [["d", "ein-jahr"], ["p", pk("contributor"), "", "author"]]},
                              sk("writer"))
    directory = SimpleNamespace(publish_plan=lambda author, done, **_kw: done(
        SimpleNamespace(targets=[A, B, C])))

    owner = QObject()                   # the queries' parent, as the writer's is

    def read(answers):
        pool, said = HandPool(), []
        read_relays(relay_pool=pool, relay_directory=directory,
                    profile=SimpleNamespace(user_pubkey=writer), unsigned_event=unsigned,
                    on_done=lambda clear, other: said.append((clear, other)), parent=owner)
        for sub in pool.subs:
            answers(sub)
        settle()
        return said

    def quiet_c(sub, held):
        relay = sub.urls[0]
        if relay == C:
            sub.fail(C)
        else:
            sub.answer(relay, *([held] if relay == B else []))

    assert read(lambda sub: quiet_c(sub, own)) == [([A, B], False)]
    assert read(lambda sub: quiet_c(sub, guest)) == [([A], True)]


def test_a_submission_that_changed_meanwhile_is_not_published_and_its_picture_comes_down(
        accepted):
    run, store, jobs = _run(accepted, reread_hash="ab" * 32)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.FAILED and jobs == []      # read before signing: nothing signed
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


def test_the_published_revision_comes_from_signed_words_never_the_servers():
    # RG-M4-a 11, contract 12.4 Which revision is published: the server's
    # item says revision 3; only the verified accept (1) and the correction
    # the writer confirmed before the report (2) count.
    import json
    from nostr import nip98
    from nostr.standup import following as rules
    sid = "ab" * 32

    def item(seq, name, path, body, revision):
        text = json.dumps(dict(body, submission_id=sid), separators=(",", ":"))
        unsigned = nip98.build_unsigned_auth_event(
            f"https://standup.example/api/submissions/{sid}/{path}", "POST", text.encode(),
            now=1_762_675_200)
        return views.TimelineItem(seq=seq, at=0, type="event", wrap=None, event=views.TimelineEvent(
            name=name, actor=pk("writer"), revision=revision, to=None,
            signed=views.SignedRecord(event=events.sign_event(unsigned, sk("writer")), body=text)))

    confirmed = item(2, "confirmed", "confirmation",
                     {"revision_hash": "cd" * 32, "confirms": ["correction"]}, 3)
    report = item(3, "published", "published", {"revision": 3}, 3)
    later = dict(writer=pk("writer"), accepted=1, checks=rules.SignedChecks())
    assert publishing.published_revision([report], sid, report, corrections={}, **later) == 1
    assert publishing.published_revision([confirmed, report], sid, report,
                                         corrections={"cd" * 32: 2}, **later) == 2


def test_after_publication_the_encrypted_files_go_but_one_another_submission_names_stays():
    # M-4 M14 blob deletion: quietly, from the verified revisions' signed
    # blob lists and the writer's servers; only a file's last user removes it.
    from nostr.bech32 import encode_naddr
    from nostr.standup.cleanup import SETTINGS_KEY, Cleanups
    from nostr.ui.standup_cleanup import BlobCleanup
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
                         event=article, revision=1, on_success=reported.append,
                         on_failure=reported.append)
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
