# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Criterion 19 of the plan: signing out, or switching accounts, stops
everything of the account that is left.

One test lets the account go while each thing is running (the watcher's
lookup, a message waiting for the signer app, a reaction on its way to
the server, an upload, a write of the submissions list, a compare on
the worker, a cover being fetched), then lets every late answer arrive.
Nothing writes the interface, the lists or the media library after it,
and the next account sees nothing of the first."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from nostr.avatar_store import AvatarStore
from nostr.profiles import Profile, ProfileStore
from nostr.standup import conversation, formats
from nostr.standup.drafting import ArticleDraft, Choice, LocalPicture
from nostr.standup.submitting import Step
from nostr.ui.standup_compare import Comparer
from nostr.ui.standup_submit_sheet import SubmitSheet
from nostr.ui.submission_cover import CoverFetch, png_bytes
from tests.membership_fakes import FakeTimer
from tests.standup_following_fakes import FollowWorld
from tests.standup_vectors import ADDRESS
from tests.test_encrypted_lists import Relay
from tests.test_standup_submissions_ui import EDITOR, ME, OTHER, Window


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


class HoldingRelay(Relay):
    """The account's relays; a write of the submissions list waits while
    ``holding``."""

    holding = False

    def __init__(self) -> None:
        super().__init__()
        self.held = []

    def __call__(self, relays, signed, *, on_done, every_relay=False):
        d_tag = next(t[1] for t in signed["tags"] if t[0] == "d")
        if self.holding and d_tag.startswith(formats.SUBMISSIONS_LIST_D):
            self.held.append(lambda: Relay.__call__(self, relays, signed, on_done=on_done))
            return
        super().__call__(relays, signed, on_done=on_done, every_relay=every_relay)


class HeldFetcher:
    def __init__(self) -> None:
        self.asked = []

    def fetch(self, url, *, on_success, on_failure) -> None:
        self.asked.append((url, on_success))


def list_writes(relay) -> list:
    return [d for d in relay.published if d.startswith(formats.SUBMISSIONS_LIST_D)]


def test_signing_out_and_switching_stops_everything_that_was_running(monkeypatch, tmp_path):
    world = FollowWorld()
    world.relay = HoldingRelay()
    uploads, compares, fetcher = [], [], HeldFetcher()

    def upload(pictures, on_progress, on_done, on_failure):
        uploads.append(SimpleNamespace(done=on_done, stopped=False))
        return lambda u=uploads[-1]: setattr(u, "stopped", True)

    window = Window(world, monkeypatch, uploads=upload,
                    comparer=Comparer(run=lambda fn, on_done, on_error: compares.append(
                        (fn, on_done))),
                    cover_fetch=lambda parent: CoverFetch(parent, fetcher=fetcher,
                                                          timer=FakeTimer()))
    seed = world.account()
    talk, entry = world.submit(seed, d="gespraech", title="Ein Jahr")
    world.message(talk, "Passt das so?")
    _unconfirmed, waiting = world.submit(seed, d="unbestaetigt", title="Zwei", stored=False)
    _never, unsent = world.submit(seed, d="nie", title="Drei", stored=False, send=False)
    tab = window.tab("gespraech")
    window.follow()
    account, following = window.accounts[ME], window.following
    window.view.show_detail(entry.sid)
    world.settle()
    detail = following.detail(entry.sid)
    target = next(m.bubble for m in detail.thread if hasattr(m, "bubble"))
    world.message(talk, "Noch eine Frage.")           # news the next lookup would bring
    tab.notice.hide()

    # -- each thing is started, and none has ended ---------------------------------
    # An upload: a new version with a picture, held at its media server.
    draft = ArticleDraft(d="gespraech", title="Ein Jahr", summary="",
                         content="Text.\n\n![Bild](bild-1)", pictures=(LocalPicture(
                             key="bild-1", name="bild.jpg", mime="image/jpeg", data=b"\xff" * 64),))
    run = window.writer.revise(
        window.profile, draft, Choice(publication=ADDRESS, name="Magazin", signer="writer",
                                      rights=True), round_=1, revision=2, change_summary="neu")
    run.start()
    world.settle()
    assert run.progress.step is Step.UPLOADING and len(uploads) == 1
    # A message waiting for the signer app, and a reaction on its way to the server.
    account.signer.held = []
    following.talk.send(entry.sid, "Ja, so passt es.")
    account.signer.release()
    account.signer.held = []
    following.talk.react(entry.sid, target.id, EDITOR, conversation.REACTIONS[0], None)
    # A write of the submissions list (Verwerfen), held at the relays.
    world.relay.holding = True
    discarded = []
    following.discard(unsent.sid, discarded.append)
    for _ in range(20):
        QApplication.processEvents()
    assert len(world.relay.held) == 1
    # The watcher's lookup: it would find the unconfirmed entry's receipt,
    # mark it stored on the list, and bring news for the article's tab.
    following.refresh()
    # A compare on the worker, and a cover being fetched for an open sheet.
    heard = []
    window.host.comparer.compare("alt", "neu", heard.append)
    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = Profile(user_pubkey=ME, bunker_pubkey="b" * 64, display_name="Jana",
                      bunker_relays=[], local_secret_hex="0" * 64)
    store.upsert(profile)
    from nostr.ui.standup_publications import PublicationLogos
    sheet = SubmitSheet(writer=window.writer, draft=draft, cover=QPixmap(), byline="Jana",
                        private_names=(), profile=profile, store=store, avatars=AvatarStore(),
                        logos=PublicationLogos(lambda *_a: None), is_dark=False)
    sheet.open()
    window.host.cover_for_sheet(sheet, "https://bilder.example/cover.jpg", "Ein Jahr")
    covers = []
    sheet.set_cover = lambda *args: covers.append(args)
    assert len(fetcher.asked) == 1 and len(compares) == 1

    changes = []
    following.changed.connect(lambda: changes.append("rows"))
    following.detail_changed.connect(changes.append)
    requests, writes = len(world.server.requests), list_writes(world.relay)

    # -- sign out -------------------------------------------------------------------
    window.writer.stop()
    assert window.view.items() == [] and not window.view.showing_detail()
    assert window.pill.isHidden() and not sheet.isVisible()
    assert uploads[0].stopped                           # cancelled in the media library

    # -- and every late answer arrives ------------------------------------------------
    world.relay.holding = False
    for release in world.relay.held:
        release()
    uploads[0].done({})                                 # the media server answers
    account.signer.release()                            # the signer app answers
    fn, on_done = compares[0]
    on_done(fn())                                       # the worker finishes
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    fetcher.asked[0][1](png_bytes(image), "image/png")  # the cover arrives
    world.settle()
    world.later.run()
    world.settle()

    # Nothing wrote the interface ...
    assert changes == [] and heard == [] and covers == [] and discarded == []
    assert window.view.items() == [] and window.pill.isHidden() and tab.notice.isHidden()
    assert world.news == [] or all(n.sid != entry.sid for n in world.news)
    # ... nor the lists: no write of the submissions list began after it
    # (the one held at the relays had left before; the read state's last
    # write is the sign-out's own, contract 13) ...
    assert list_writes(world.relay)[len(writes) + len(world.relay.held):] == []
    assert account.submissions.entries() == [] and not account.read_state.loaded
    # ... nor the server: no message, no reaction, no version, no session.
    late = [(verb, path) for verb, path, _b, _h in world.server.requests[requests:]]
    assert [r for r in late if r[0] == "POST"] == []
    assert world.record(entry.sid).revision == 1
    assert not world.record(waiting.sid) is None        # untouched on the server

    # -- another account: it sees nothing of the first ----------------------------------
    window.profile = SimpleNamespace(user_pubkey=OTHER, bunker_relays=[])
    window.follow()
    assert window.following is not following and window.view.items() == []
    window.view.opened()                                # its list is read once it shows
    world.settle()
    assert window.view.empty_title.text() == "No Submissions Yet"
    following.refresh()                                 # a late caller of the first
    following.open(entry.sid)
    world.settle()
    assert changes == [] and window.view.items() == [] and window.pill.isHidden()


def test_a_list_read_or_write_not_yet_at_the_signer_never_asks_it_once_the_account_is_let_go():
    # Lesson 31: the relays of a list are found on a later turn; an
    # account let go meanwhile is not read for, so its signer app is not
    # asked to open a page for an account that was left.
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="nie", stored=False, send=False)
    following = world.following(account)
    asked, written = len(account.key.asked), list(world.relay.published)
    done = []
    following.discard(entry.sid, done.append)           # a write starts ...
    account.read_whole(done.append)                     # ... and a read
    following.stop()
    account.let_go()                                    # the account is let go at once
    world.settle()
    assert account.key.asked[asked:] == [] and world.relay.published == written
    assert done == []
