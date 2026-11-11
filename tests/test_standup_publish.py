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

from i18n import _
from nostr import events
from nostr.standup import crypto as scrypto
from nostr.standup import formats, publishing, views
from nostr.ui.standup_publish import AcceptedPublish, PublishPorts, Stage
from nostr.ui.standup_unlisted import QUICK_TRIES
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

    def delete_on_servers(self, pairs, on_done, *, going=lambda: True, on_not_signed=None):
        self.batches.append(list(pairs))
        on_done(list(pairs))


class FakeJob(QObject):
    """HeldPublish's side the run uses: signs with the account's key, and
    sends only once released; ``took`` says whether a relay takes it."""

    signed = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, key, unsigned, here, took, only=None) -> None:
        super().__init__()
        self.key, self.unsigned_event, self.here, self.took = key, unsigned, here, took
        self.only = only
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


def _run(case, *, store=None, took=(True,), reread_hash=None, key_here=False, profile=None,
         relays_hold=None, changed_from=0, cleanup=None):
    store = store or FakeStore()
    jobs = []
    following, api = case.following, case.writer.api

    reads = []

    def reread(on_detail, on_failure):
        view = following.row(case.sid).view
        reads.append(view)
        if reread_hash is not None and len(reads) > changed_from:
            view = replace(view, revision_hash=reread_hash)
        on_detail(SimpleNamespace(submission=view, address_taken=False))

    def held_at(address, on_author, on_failure):
        api.article_author(address, on_author, on_failure)

    def job(profile, unsigned, here, only):
        jobs.append(FakeJob(case.writer.key, unsigned, here, list(took), list(only)))
        return jobs[-1]

    view = following.row(case.sid).view
    ports = PublishPorts(
        api=api, media_keys=case.writer.media_keys, store=store,
        fetch=lambda url, on_success, on_failure: on_success(case.cipher)
        if url.endswith(case.entry["cipher_sha256"]) else on_failure("404"),
        job=job, key_here=lambda: key_here, serve=following.serve, reread=reread,
        held_at=held_at, clock=case.world.clock, cleanup=cleanup, unlisted=following.unlisted,
        relays_hold=relays_hold or (lambda _unsigned, done: done({"wss://r.example": (True, [])})))
    run = AcceptedPublish(sid=case.sid, profile=profile or case.writer.profile,
                          version=following.reading.latest(case.sid),
                          accepted=following.reading.accepted(case.sid),
                          revision_hash=view.revision_hash, publication="Magazin", ports=ports)
    following.track(case.sid, run)
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
    # RG-M4-b 3: public all the same; one sentence, a question, no retry.
    title = accepted.following.row(accepted.sid).title
    assert run.stage is Stage.NOT_LISTED and run.words == (
        f"“{title}” is public, but Magazin already lists another work under this link name.")
    assert formats.tag_value(jobs[0].signed_event, "d") == "ein-jahr"
    from nostr.standup import following as rules
    from nostr.ui.standup_publish_sheet import PublishSheet
    assert accepted.following.row(accepted.sid).publishing == rules.NOT_LISTED
    assert not any(ms == 30000 for ms, _call in accepted.world.later.waiting)   # never again
    sheet = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False)
    assert sheet.publish.text() == "Ask a Question" and sheet.cancel_button.text() == "Close"
    sheet.deleteLater()


def _flaky_report(api, failures):
    """``api.published`` failing ``failures[0]`` times while it is above 0."""
    from nostr.standup.errors import Code, StandupError
    real, calls = api.published, []

    def published(sid, **kwargs):
        calls.append(sid)
        if failures[0] > 0:
            failures[0] -= 1
            kwargs["on_failure"](StandupError(code=Code.OFFLINE))
        else:
            real(sid, **kwargs)

    api.published = published
    return calls


def _waiting(world, ms):
    """Run the wait of ``ms`` the test world holds."""
    found = next(i for i, (wait, _call) in enumerate(world.later.waiting) if wait == ms)
    world.later.waiting.pop(found)[1]()
    world.settle()


def test_a_public_work_whose_report_did_not_arrive_says_so_and_is_reported_again_quietly(
        accepted):
    # RG-M4-b 3, 9: on its way, the list, the pill and the detail say "Wird
    # veröffentlicht …"; once a relay took it, it is public whatever the
    # server answers, said calmly, and reported again after a pause.
    from nostr.standup import following as rules
    from nostr.ui.standup_publish_sheet import PublishSheet
    following, world = accepted.following, accepted.world
    calls = _flaky_report(accepted.writer.api, [1])
    run, _store, _jobs = _run(accepted)
    sheet = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False)
    seen = []
    following.changed.connect(lambda: seen.append(following.row(accepted.sid).publishing))
    run.start()
    world.settle()
    assert seen[0] == rules.PUBLISHING
    row = following.row(accepted.sid)
    sentence = f"“{row.title}” is public. Magazin learns of it shortly."
    assert run.stage is Stage.PUBLIC and row.publishing == rules.PUBLIC and not row.turn
    assert rules.pill_word(row, lambda _pk: "") == "Published"
    status = rules.status_of(row, publication="Magazin", name_of=lambda _pk: "")
    assert (status.sentence, status.action) == (sentence, None)
    assert sheet.status.text() == sentence and not sheet.privacy.isVisibleTo(sheet)
    assert sheet.publish.text() == "Done"
    world.clock.advance(30)
    _waiting(world, 30000)
    assert len(calls) == 2 and run.stage is Stage.DONE
    assert following.row(accepted.sid).publishing == ""
    assert following.unlisted.record.sids() == []
    sheet.deleteLater()


def test_after_a_restart_a_public_work_still_says_so_and_is_reported_once_the_view_opens(
        accepted):
    # RG-M4-b 3: the signed article is kept (never a key) and, with the key
    # on this computer, reported again when the Einreichungen view first
    # opens, never at launch.
    from nostr.standup import following as rules
    world = accepted.world
    accepted.writer.profile.is_local = True
    _flaky_report(accepted.writer.api, [1])
    run, _store, _jobs = _run(accepted)
    run.start()
    world.settle()
    record = accepted.following.unlisted.record
    assert record.sids() == [accepted.sid] and "event" in record.entry(accepted.sid)
    accepted.following.stop()                                # the app closes
    world.later.waiting.clear()                              # and its timers with it
    again = world.following(accepted.writer, unlisted=record)
    assert again.row(accepted.sid).publishing == rules.PUBLIC
    assert not any(ms == 30000 for ms, _call in world.later.waiting)   # not at launch
    again.view_opened()
    world.settle()
    world.clock.advance(30)
    _waiting(world, 30000)
    assert world.record(accepted.sid).state == "published" and record.sids() == []
    assert again.row(accepted.sid).situation is rules.Situation.PUBLISHED_UNPLACED


def _days_pass(world, days):
    """The waits the world holds run when due, the clock moving with them,
    for ``days``."""
    end, seen = world.clock() + days * 86400, []
    while True:
        for entry in world.later.waiting:
            if not any(entry is known for known, _due in seen):
                seen.append((entry, world.clock() + entry[0] / 1000))
        waiting = [(due, entry) for entry, due in seen if any(
            entry is held for held in world.later.waiting)]
        if not waiting or min(due for due, _entry in waiting) > end:
            break
        due, entry = min(waiting, key=lambda pair: pair[0])
        world.later.waiting.remove(entry)
        world.clock.advance(max(0.0, due - world.clock()))
        entry[1]()
        world.settle()
    world.clock.advance(max(0.0, end - world.clock()))


def test_through_a_signer_app_a_week_brings_three_quick_tries_and_one_ride_along_a_run(
        accepted):
    # The lead's fix to the M4 open list (the calm rule): through a signer
    # app the quiet report tries at most 3 times right after the publish;
    # then a try only rides along with a signer action of the person's, and
    # never more than one such request per run, restart or not; after 7 days
    # none at all. Each report is one signed request (NIP-98), so its calls
    # count the signer app's asks over the week.
    world, sid, following = accepted.world, accepted.sid, accepted.following
    calls = _flaky_report(accepted.writer.api, [10 ** 6])
    run, _store, _jobs = _run(accepted)
    run.start()
    world.settle()
    assert calls == [sid]                                   # the run's own report
    _days_pass(world, 1)
    assert len(calls) == 1 + QUICK_TRIES                    # then nothing on its own
    for text in ("Ist es angekommen?", "Und jetzt?"):       # the person signs here twice
        assert following.talk.send(sid, text) == ""
        world.settle()
    assert len(calls) == 2 + QUICK_TRIES                    # one ride-along, not two
    following.stop()                                        # a restart
    world.later.waiting.clear()
    again = world.following(accepted.writer, unlisted=following.unlisted.record)
    again.view_opened()
    again.open(sid)
    world.settle()
    _days_pass(world, 5)
    assert len(calls) == 2 + QUICK_TRIES                    # none at all on its own
    for text in ("Hallo?", "Noch da?"):
        assert again.talk.send(sid, text) == ""
        world.settle()
    assert len(calls) == 3 + QUICK_TRIES                    # this run's one
    _days_pass(world, 2)
    again.talk.send(sid, "Eine Woche später")
    world.settle()
    from nostr.standup.following import UNREPORTED                    # kept, by hand now
    assert len(calls) == 3 + QUICK_TRIES and again.row(sid).publishing == UNREPORTED


def test_after_a_stage_change_the_focus_is_on_the_status_line_never_on_view(accepted):
    # RG-M4-c 8: Space on Veröffentlichen, and once published the focus is
    # on what the sheet says, so a second Space opens nothing.
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from nostr.ui.standup_publish_sheet import PublishSheet
    run, _store, _jobs = _run(accepted)
    sheet = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False)
    sheet.show()
    sheet.activateWindow()
    QApplication.processEvents()
    sheet.publish.setFocus()
    QTest.keyClick(sheet.publish, Qt.Key.Key_Space)
    accepted.world.settle()
    assert run.stage is Stage.DONE and sheet.view.isVisibleTo(sheet)
    assert sheet.focusWidget() is sheet.status
    sheet.deleteLater()


def test_the_confirm_and_publish_sheets_read_whole_at_their_minimum_size_in_german(accepted):
    # RG-M4-c 9: at its own minimum size, in German, no button clips and no
    # wrapping line is cut (Profil bearbeiten …, Erneut versuchen, Q1).
    import i18n
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    from nostr.ui.standup_confirm_sheet import ConfirmSheet
    from nostr.ui.standup_publish_sheet import PublishSheet

    def whole(sheet):
        sheet.show()
        sheet.resize(sheet.minimumSize())
        for _ in range(4):
            QApplication.processEvents()
        for button in sheet.findChildren(QPushButton):
            if button.isVisibleTo(sheet) and button.text():
                assert button.width() >= button.sizeHint().width(), button.text()
        for label in sheet.findChildren(QLabel):
            if label.isVisibleTo(sheet) and label.wordWrap() and label.text():
                assert label.height() >= label.heightForWidth(label.width()), label.text()
        sheet.close()

    i18n.install("de")
    try:
        for address in ("", None):                   # Profil bearbeiten …, Erneut versuchen
            sheet = ConfirmSheet(
                role="writer", publication="Magazin", number=2,
                shares=((pk("writer"), 70, "writer"), (pk("contributor"), 30, "contributor")),
                me=pk("writer"), name_of=lambda _pk: "Jana",
                zap_address=lambda _pk, answer, said=address: answer(said),
                confirm=lambda confirms, done: None, is_dark=False)
            whole(sheet)
        from nostr.ui.standup_publish_words import NOT_CHECKED, TOO_LONG_HERE
        for stage, words in ((Stage.READY, ""), (Stage.NOT_CHECKED, NOT_CHECKED),
                             (Stage.OFFER_HERE, TOO_LONG_HERE)):
            run, _store, _jobs = _run(accepted)
            run._say(stage, _("Not published") if words else "", _(words) if words else "")
            whole(PublishSheet(run=run, publication="Magazin", number=1, is_dark=False))
    finally:
        i18n.install("en")


def test_a_public_work_with_no_quiet_try_left_offers_send_again_and_stays_public(accepted):
    # RG-M4-c 3 (ux/p_giveup.py): once no quiet try is left the row says it
    # is public and offers Erneut senden; the record stays, after 7 days and
    # a restart too, so a public text never reads as private again.
    from nostr.standup import following as rules
    from nostr.ui.standup_publish_sheet import PublishSheet
    world, sid, following = accepted.world, accepted.sid, accepted.following
    failures = [10 ** 6]
    calls = _flaky_report(accepted.writer.api, failures)
    run, _store, _jobs = _run(accepted)
    run.start()
    world.settle()
    assert following.row(sid).publishing == rules.PUBLIC
    _days_pass(world, 8)
    row = following.row(sid)
    status = rules.status_of(row, publication="Magazin", name_of=lambda _pk: "")
    assert (row.publishing, run.going, status.action) == (
        rules.UNREPORTED, rules.UNREPORTED, rules.Action.REPORT)
    assert (status.sentence, status.more) == (f"“{row.title}” is public.",
                                              "Magazin hasn’t learned of it yet.")
    following.stop()                                        # a restart
    world.later.waiting.clear()
    again = world.following(accepted.writer, unlisted=following.unlisted.record)
    again.view_opened()
    again.open(sid)
    world.settle()
    assert again.row(sid).publishing == rules.UNREPORTED and not again.row(sid).turn
    later, _store, _jobs = _run(SimpleNamespace(**dict(vars(accepted), following=again)))
    sheet = PublishSheet(run=later, publication="Magazin", number=1, is_dark=False)
    assert later.stage is Stage.PUBLIC and not sheet.privacy.isVisibleTo(sheet)
    sheet.deleteLater()
    failures[0] = 0
    asked = len(calls)
    answers = []
    again.unlisted.send_again(sid, answers.append)          # Erneut senden, by hand
    world.settle()
    assert answers == [None] and len(calls) == asked + 1
    assert world.record(sid).state == "published" and again.unlisted.record.sids() == []
    assert again.row(sid).publishing == ""


def test_the_key_kept_here_is_wiped_after_its_signature_and_goes_with_the_run(accepted):
    # RG-M4-a 4 (probe p3): the Q1 key is wiped once the signer answered
    # or the job was cancelled, and the run's jobs are its own, so they go
    # when the account is let go.
    from PySide6.QtCore import QCoreApplication, QEvent
    from nostr.ui.standup_publish_job import SignedHere
    directory = SimpleNamespace(publish_plan=lambda *_a, **_kw: None)
    unsigned = {"kind": 30023, "pubkey": pk("writer"), "created_at": 1, "tags": [["d", "x"]],
                "content": "t"}

    def here():
        return SignedHere(secret=sk("writer"), relay_pool=None, relay_directory=directory,
                          session_pool=None, unsigned_event=unsigned,
                          profile=SimpleNamespace(user_pubkey=pk("writer"), bunker_relays=[]))

    signed, cancelled = here(), here()
    signed.start()
    cancelled.start()
    cancelled.cancel()
    accepted.world.settle()
    assert signed.signed_event is not None and not signed.holds_key
    assert not cancelled.holds_key
    run, _store, jobs = _run(accepted, took=())          # signed, no relay took it
    run.start()
    accepted.world.settle()
    gone = []
    jobs[0].destroyed.connect(lambda *_: gone.append(True))
    assert jobs[0].parent() is run
    run.stop()                                           # signed out
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert gone == [True]


def test_a_contributor_confirms_their_part_and_the_license_in_the_webs_words():
    # RG-M4-b 7: the part as the revision credits it, the license it names.
    from nostr.ui.standup_confirm_sheet import rights_words
    revision = {"credits": [{"pubkey": pk("contributor"), "part": "Fotos"}],
                "rights": {"confirmed": True, "license": "CC BY 4.0"}}
    assert rights_words(**publishing.rights_of(revision, pk("contributor"))) == (
        "My contribution (Fotos) is mine. I agree to the license CC BY 4.0.")
    assert rights_words(**publishing.rights_of({}, pk("contributor"))) == (
        "My contribution is mine.")


def test_a_share_keeps_its_percent_sign_on_the_line_of_its_number_in_german():
    # RG-M4-b 10: "70 %" never breaks before "%" (U+202F).
    import i18n
    from nostr.standup.thread import share_text
    i18n.install("de")
    try:
        text = share_text("Jana", 70)
    finally:
        i18n.install("en")
    assert text == "Jana 70\u202f%" and share_text("Jana", 70) == "Jana 70%"


def test_the_writer_release_vectors():
    # Contract 12.4 at 569b3f8, Before the writer's work goes out (vector
    # writer_release, releaseRelays): this server, then every relay.
    vector = VECTORS["writer_release"]

    def send_to(case):
        held = case["held"]
        return publishing.release(
            writer=vector["writer"], address=vector["address"],
            address_taken=case["address_taken"], server_read=case["server_read"],
            held=None if held is None else publishing.held_credit(held, vector["address"]),
            relays={url: (read["complete"], read["events"])
                    for url, read in case["relays"].items()}).send_to

    for name, case in vector["may"].items():
        assert list(send_to(case)) == case["send_to"] and case["send_to"], name
    for name, case in vector["must_fail"].items():
        assert send_to(case) == (), name


def test_the_writer_is_the_first_author_though_the_text_mentions_a_contributor_first():
    # RG-M4-c 2 (probe p8): a mention ahead of the credits never takes the
    # first author tag (vector the_writer_credited_only_after_another), and
    # a correction of a work published in that order puts the writer first.
    from nostr.bech32 import encode_npub
    writer, contributor = pk("writer"), pk("contributor")
    revision = {"work": {"d": "ein-jahr", "title": "Ein Jahr", "summary": "", "image": "",
                         "tags": [], "lang": "de", "authors": [], "extra_tags": [],
                         "content": f"Fotos: nostr:{encode_npub(contributor)}\n\nText."},
                "media": [], "credits": [{"pubkey": contributor, "part": "Fotos"}]}

    def authors(event):
        return [tag[1] for tag in event["tags"] if tag[0] == "p" and tag[3:4] == ["author"]]

    event = publishing.published_template(revision, writer=writer, d="ein-jahr",
                                          media_urls={}, published_at=100, created_at=100)
    assert authors(event) == [writer, contributor]
    assert [t[1] for t in event["tags"] if t[0] == "p"] == [writer, contributor]
    shape = VECTORS["writer_release"]["must_fail"]["the_writer_credited_only_after_another"]
    assert shape and publishing.credited_writer(event) == writer
    old = dict(event, tags=[t for t in event["tags"] if t[0] != "p"]
               + [["p", contributor, "", "author"], ["p", writer, "", "author"]])
    corrected = publishing.corrected_template(revision, events.sign_event(old, sk("writer")),
                                              writer=writer, media_urls={}, created_at=200)
    assert authors(corrected) == [writer, contributor]


def test_nothing_goes_out_over_another_persons_article_at_the_address(accepted):
    # RG-M4-a 2, 12.4: read before any picture goes up in plain; another
    # writer's article stands at the writer's address, so nothing is
    # signed, sent or put up, and the sheet offers a question, not Erneut.
    other = events.sign_event({"kind": 30023, "created_at": 1, "content": "Fremd",
                               "tags": [["d", "ein-jahr"], ["p", pk("contributor"), "", "author"]]},
                              sk("writer"))
    accepted.world.server.articles[f"30023:{pk('writer')}:ein-jahr"] = other
    run, store, jobs = _run(accepted)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.SOMEONE_ELSES and jobs == []
    assert "So that it stays, nothing is published." in run.words
    assert store.uploaded == [] and store.deleted == []
    assert accepted.world.record(accepted.sid).state == "accepted"
    # RG-M4-c 7: with no sheet open the detail says the refusal and offers
    # Rückfrage stellen, never a Veröffentlichen that only says it again.
    from nostr.standup import following as rules
    row = accepted.following.row(accepted.sid)
    status = rules.status_of(row, publication="Magazin", name_of=lambda _pk: "")
    assert (row.publishing, status.sentence, status.action) == (
        rules.SOMEONE_ELSES, run.words, rules.Action.ASK)
    assert rules.pill_word(row, lambda _pk: "") == "Your turn"
    assert _(rules.ACTION_LABELS[rules.Action.ASK]) == "Ask a Question"


def test_the_address_is_read_on_every_relay_and_only_clear_ones_take_it(accepted):
    # RG-B5-d, contract 12.4: the server's index may miss a guest's work
    # the writer's own publication released elsewhere. Before a picture
    # goes up and right before signing, the address is read on every
    # relay it goes to: one that did not answer is left out quietly;
    # another person's work, even on a relay that did not finish, sends
    # nothing anywhere, and no answer at all says so with Erneut.
    from nostr.ui.standup_publish_job import read_relays
    from tests.outbox_fakes import HandPool
    A, B, C = "wss://a.example", "wss://b.example", "wss://c.example"
    guest = events.sign_event({"kind": 30023, "created_at": 6, "content": "Gast",
                               "tags": [["d", "ein-jahr"], ["p", pk("contributor"), "", "author"]]},
                              sk("writer"))
    directory = SimpleNamespace(publish_plan=lambda author, done, **_kw: done(
        SimpleNamespace(targets=[A, B, C])))
    pool, owner = HandPool(), QObject()

    def relays_hold(unsigned, done):
        read_relays(relay_pool=pool, relay_directory=directory,
                    profile=SimpleNamespace(user_pubkey=pk("writer")), unsigned_event=unsigned,
                    on_done=done, parent=owner)

    def publish(answer):
        run, store, jobs = _run(accepted, relays_hold=relays_hold)
        pool.subs.clear()
        run.start()
        accepted.world.settle()
        answered = 0
        while answered < len(pool.subs):         # before the picture, then before signing
            answer(pool.subs[answered])
            answered += 1
            accepted.world.settle()
        return run, store, jobs

    run, store, jobs = publish(lambda sub: (sub.answer(A), sub.send(B, guest), sub.fail(B),
                                            sub.fail(C)))
    assert run.stage is Stage.SOMEONE_ELSES and jobs == [] and store.uploaded == []
    run, store, jobs = publish(lambda sub: (sub.fail(A), sub.fail(B), sub.fail(C)))
    assert run.stage is Stage.NOT_CHECKED and jobs == [] and store.uploaded == []
    assert run.title == "Not published" and "couldn’t be checked just now" in run.words
    from nostr.ui.standup_publish_sheet import PublishSheet
    sheet = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False)
    assert (sheet.publish.text(), sheet.cancel_button.text()) == ("Try Again", "Cancel")
    sheet.cancel_button.click()                 # Abbrechen: back at Veröffentlichen
    assert run.stage is Stage.READY and sheet.publish.text() == "Publish"
    sheet.deleteLater()
    run, store, jobs = publish(lambda sub: (sub.answer(A), sub.answer(B), sub.fail(C)))
    assert run.stage is Stage.DONE and jobs[-1].only == [A, B] and len(pool.subs) == 2
    assert pool.subs[0].filters == [{"kinds": [30023], "authors": [pk("writer")],
                                     "#d": ["ein-jahr"], "limit": 25}]


def test_a_submission_that_changed_meanwhile_is_not_published_and_its_picture_comes_down(
        accepted):
    # Read again right before signing, after the picture went up: it
    # changed meanwhile, so nothing is signed and the picture comes down.
    from nostr.ui.standup_publish_sheet import PublishSheet
    run, store, jobs = _run(accepted, reread_hash="ab" * 32, changed_from=1)
    sheet, closed = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False), []
    sheet.rejected.connect(lambda: closed.append(True))
    sheet.show()
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.CHANGED and jobs == [] and closed == [True]
    run.retry()                                 # RG-M4-a 9: no signer asked again
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.CHANGED and jobs == []
    assert store.deleted == [(accepted.entry["plain_sha256"], (SERVER,))]
    assert run.signed_article is None


def test_a_picture_the_servers_refuse_publishes_nothing(accepted):
    run, store, jobs = _run(accepted, store=FakeStore(refuse=True))
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.FAILED and jobs == []


def test_too_long_for_the_signer_app_with_the_key_here_offers_it_once(accepted, monkeypatch):
    # Q1: explicit and for this article only; never without a key here.
    # RG-M4-b 4: the web's words, never "shorten it"; without a key, a
    # question to the Redaktion and no Erneut.
    from nostr import signer_limits
    from nostr.ui import standup_publish_job
    for module in (signer_limits, standup_publish_job):     # the signer app's limit
        monkeypatch.setattr(module, "signable", lambda profile, event: False)
    remote = SimpleNamespace(user_pubkey=pk("writer"), signer="remote", bunker_relays=[])
    run, store, jobs = _run(accepted, key_here=False, profile=remote)
    run.start()
    assert run.stage is Stage.TOO_LONG and store.uploaded == []
    assert "Sign in with another signer app, or write to the editorial team." in run.words
    from nostr.standup import following as rules
    row = accepted.following.row(accepted.sid)                     # RG-M4-c 7: it stays
    assert row.publishing == rules.TOO_LONG and rules.status_of(
        row, publication="Magazin", name_of=lambda _pk: "").action is rules.Action.ASK
    from nostr.ui.standup_publish_sheet import PublishSheet
    sheet = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False)
    assert sheet.publish.text() == "Ask a Question"
    sheet.deleteLater()
    run, store, jobs = _run(accepted, key_here=True, profile=remote)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.OFFER_HERE and store.uploaded == [] and jobs == []
    assert run.words == ("Your signer app doesn’t sign texts this long. It works with the key "
                         "on this computer.")
    run.start(here=True)
    accepted.world.settle()
    assert jobs[0].here and run.stage is Stage.DONE


def test_q1_learned_only_from_the_signer_apps_refusal_still_publishes_with_the_key_here(
        accepted, monkeypatch):
    # RG-M4-c 1 (probe p10): the relays' caps come once the signer app is
    # connected, so the refusal teaches the limit; the key here then signs.
    from nostr import signer_limits
    from nostr.ui import standup_publish_job
    limit = {"known": False}
    for module in (signer_limits, standup_publish_job):
        monkeypatch.setattr(module, "signable", lambda profile, event: not limit["known"])
    remote = SimpleNamespace(user_pubkey=pk("writer"), signer="remote", bunker_relays=[])
    run, _store, jobs = _run(accepted, key_here=True, profile=remote)
    real_start = FakeJob.start

    def refused(job):
        if job.here:
            real_start(job)
        else:
            limit["known"] = True
            job.failure_words = "refused"
            job.failed.emit("too long")

    monkeypatch.setattr(FakeJob, "start", refused)
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.OFFER_HERE
    run.start(here=True)
    accepted.world.settle()
    assert jobs[-1].here and run.stage is Stage.DONE
    assert accepted.world.record(accepted.sid).state == "published"


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


def test_a_correction_taken_before_is_said_so_and_published_without_asking_again(accepted):
    # RG-M4-b 8: the View holds the writer's correction confirmation (a
    # publish that failed, a restart): the web's words, and Publish.
    from nostr.standup import following as rules
    from nostr.ui.standup_publish import CorrectionPublish
    first, _store, jobs = _run(accepted)
    first.start()
    accepted.world.settle()
    accepted.world.clock.advance(60)
    content = f"Korrigiert ![]({accepted.entry['ref']})"
    accepted.world.correct(accepted.writer, accepted.sid, content, media=[accepted.entry])
    following = accepted.following
    following.refresh()
    accepted.world.settle()
    following.open(accepted.sid)
    accepted.world.settle()
    view = following.row(accepted.sid).view
    following.confirm(accepted.sid, confirms=("correction",), revision_hash=view.revision_hash,
                      split_rumor_id=None, on_done=lambda _error: None)
    accepted.world.settle()
    row = following.row(accepted.sid)
    assert row.situation is rules.Situation.CORRECTION_TAKEN
    status = rules.status_of(row, publication="Magazin", name_of=lambda _pk: "")
    assert (status.sentence, status.action) == ("Correction taken, not published yet.",
                                                rules.Action.PUBLISH)
    asked = []
    from nostr.standup.publishing import Accepted
    run = CorrectionPublish(
        published=following.reading.published_event(accepted.sid), taken=True,
        sid=accepted.sid, profile=accepted.writer.profile,
        version=following.reading.latest(accepted.sid),
        accepted=Accepted(revision=2, split_rumor_id=None, actor="", seq=0),
        revision_hash=row.view.revision_hash, publication="Magazin",
        ports=replace(first._ports, confirm=lambda *args: asked.append(args)))
    from nostr.ui.standup_compare import Comparer
    from nostr.ui.standup_correction_sheet import CorrectionSheet
    sheet = CorrectionSheet(run=run, publication="Magazin", old="Text.", new=content, note="",
                            comparer=Comparer(run=lambda fn, done, _error: done(fn())),
                            changes=(), is_dark=False)
    assert sheet.take.text() == "Publish" and not sheet.replaces.isVisibleTo(sheet)
    sheet.deleteLater()
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.DONE and asked == [] and "Korrigiert" in jobs[-1].signed_event[
        "content"]


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


def test_right_before_the_signer_app_is_asked_again_the_done_sheet_says_why(accepted):
    # RG-M4-b 12: through a signer app, the deletion asks once more right
    # after "Veröffentlicht."; one quiet line says so just before.
    from nostr.standup.cleanup import Cleanups
    from nostr.ui.standup_cleanup import BlobCleanup
    world, writer, following, sid, _own = _published_with_pictures()
    store, kept, said = FakeStore(), {}, []
    cleaner = BlobCleanup(following, writer, SimpleNamespace(user_pubkey=pk("writer"),
                                                             is_local=False),
                          store=store, cleanups=Cleanups(pk("writer"), load=lambda: kept,
                                                         save=kept.__setitem__),
                          clock=world.clock, parent=following)
    cleaner.after_publishing(sid, lambda: said.append(len(store.batches)) or True)
    world.settle()
    assert said == [0] and len(store.batches) == 1          # said, then asked
    from nostr.ui.standup_publish_sheet import PublishSheet
    run, _store, _jobs = _run(accepted, cleanup=lambda _sid, asking: asking())
    sheet = PublishSheet(run=run, publication="Magazin", number=1, is_dark=False)
    sheet.show()
    run.start()
    accepted.world.settle()
    assert run.stage is Stage.DONE and sheet.status.text() == (
        "Published. Your signer app asks once more in a moment: with it, MyEditor deletes the "
        "private copies of your article.")
    sheet.deleteLater()


def test_each_submission_has_its_own_delete_token_of_at_most_32_files():
    # RG-M4-a 10: a token names at most 32 files (every server of a file
    # with it); one per submission, and one that fails fails only its own.
    from nostr.blossom.store import delete_batches
    from nostr.standup.cleanup import Cleanups
    from nostr.ui.standup_cleanup import BlobCleanup
    files = [f"{n:064x}" for n in range(33)]
    pairs = [(sha, server) for sha in files for server in (SERVER, "https://b.example")]
    batches = delete_batches(pairs)
    assert [len({sha for sha, _s in batch}) for batch in batches] == [32, 1]
    assert batches[1] == [(files[32], SERVER), (files[32], "https://b.example")]

    class Refusing(FakeStore):
        def delete_on_servers(self, pairs, on_done, **_kw):
            self.batches.append(list(pairs))
            on_done([] if len(self.batches) == 1 else list(pairs))   # the first token fails

    store, kept = Refusing(), {}
    from nostr.ui.standup_unlisted import RideAlong
    owner = QObject()
    owner.detail_changed = SimpleNamespace(connect=lambda _slot: None)
    owner.ride_along = RideAlong()
    cleaner = BlobCleanup(owner, SimpleNamespace(writer=pk("writer"), stopped=False),
                          SimpleNamespace(is_local=True), store=store,
                          cleanups=Cleanups(pk("writer"), load=lambda: kept,
                                            save=kept.__setitem__), clock=lambda: 1000)
    cleaner._delete({"a": (None, [(files[0], SERVER)]), "b": (None, [(files[1], SERVER)])})
    records = Cleanups(pk("writer"), load=lambda: kept, save=kept.__setitem__)
    assert store.batches == [[(files[0], SERVER)], [(files[1], SERVER)]]
    assert not records.entry("a")["over"] and records.entry("b")["over"]
    cleaner._delete({"c": (None, pairs)}, one_request=True)     # a ride-along: one token
    assert store.batches[2:] == [batches[0]] and not records.entry("c")["over"]


def _published_with_pictures():
    """A published submission of the writer's with two pictures, one of
    which another submission names too."""
    from nostr.bech32 import encode_naddr
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
    return world, writer, following, sid, own


def test_through_a_signer_app_the_cleanup_asks_only_when_said_or_as_the_one_ride(accepted):
    # RG-M4-c 5 (probe p11): a report that arrives on a later quick try
    # starts no cleanup; the cleanup asks only while its line is on screen,
    # else as the run's one ride; a refusal ends it for the run; and no ride
    # is spent on a work the View already lists.
    from nostr.standup.cleanup import Cleanups
    from nostr.ui.standup_cleanup import BlobCleanup
    asked = []
    _flaky_report(accepted.writer.api, [3])
    run, _store, _jobs = _run(accepted, cleanup=lambda sid, _say: asked.append(sid))
    run.start()
    accepted.world.settle()
    _days_pass(accepted.world, 1)
    assert run.stage is Stage.DONE and asked == []           # listed later: no cleanup

    def cleaner(store):
        world, writer, following, sid, _own = _published_with_pictures()
        kept = {}
        made = BlobCleanup(following, writer, SimpleNamespace(user_pubkey=pk("writer"),
                                                              is_local=False),
                           store=store, cleanups=Cleanups(pk("writer"), load=lambda: kept,
                                                          save=kept.__setitem__),
                           clock=world.clock, parent=following)
        made.after_publishing(sid, lambda: False)           # the sheet was closed
        world.settle()
        return made, following, world

    store = FakeStore()
    _cleaner, following, there = cleaner(store)
    assert store.batches == []                              # nothing asked unsaid
    for _ in range(2):
        following.ride_along.person_signed()
        there.settle()
    assert len(store.batches) == 1                          # the run's one ride

    class Refusing(FakeStore):
        def delete_on_servers(self, pairs, on_done, *, going=lambda: True, on_not_signed=None):
            self.batches.append(list(pairs))
            on_not_signed()
            on_done([])

    store = Refusing()
    refused, following, there = cleaner(store)
    following.ride_along.person_signed()
    there.settle()
    assert len(store.batches) == 1 and refused._ride() is False

    from nostr.standup import unlisted
    world, sid = accepted.world, accepted.sid
    record = accepted.following.unlisted.record          # kept, though the View lists it
    record.keep(sid, unlisted.first(event=run.signed_article, naddr=run.naddr, revision=1,
                                    now=world.clock()))
    calls = _flaky_report(accepted.writer.api, [10 ** 6])
    accepted.following.stop()                            # a restart
    world.later.waiting.clear()
    again = world.following(accepted.writer, unlisted=record)
    again.view_opened()
    again.open(sid)
    world.settle()
    again.ride_along.person_signed()
    world.settle()
    assert calls == [] and record.sids() == []


def test_after_publication_the_encrypted_files_go_but_one_another_submission_names_stays():
    # M-4 M14 blob deletion: quietly, from the verified revisions' signed
    # blob lists and the writer's servers; only a file's last user removes it.
    from nostr.standup.cleanup import SETTINGS_KEY, Cleanups
    from nostr.ui.standup_cleanup import BlobCleanup
    world, writer, following, sid, own = _published_with_pictures()
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
