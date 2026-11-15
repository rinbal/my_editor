# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Your own drafts never expire; imported ones you leave alone do (D-3).

Every draft used to expire 90 days after its last save, so a draft left
alone for three months vanished without a word. Now only an import
dates its drafts' end (NIP-40); a save from the editor, also of an
imported draft the person changed, makes a draft that stays.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from main_window import MainWindow
from nostr.drafts import DEFAULT_EXPIRATION_SECONDS, build_inner_event
from nostr.publisher import DraftPublishJob
from tests.imports_fakes import (
    PROFILE,
    FakeFetcher,
    FakeLongFormFetcher,
    RecordingPacer,
    inline_run_blocking,
    make_factory,
    make_item,
)
from tests.outbox_fakes import FakeRelayDirectory, settle


def wrap_of(job: DraftPublishJob) -> dict:
    """The unsigned wrap a draft job asks the signer to sign."""
    asked = []
    client = MagicMock()
    client.sign_event.side_effect = lambda unsigned, on_success, on_failure: asked.append(
        unsigned)
    job._on_encrypted("ciphertext", client, ["wss://r.example"])
    return asked[0]


def job(**kw) -> DraftPublishJob:
    inner = build_inner_event(kind=30023, content="Body", pubkey_hex=PROFILE.user_pubkey)
    return DraftPublishJob(relay_pool=MagicMock(), relay_directory=MagicMock(),
                           session_pool=MagicMock(), profile=PROFILE, inner_event=inner,
                           identifier="my-draft", **kw)


def test_a_draft_saved_from_the_editor_never_expires():
    tags = wrap_of(job())["tags"]
    assert not any(tag[0] == "expiration" for tag in tags)


def test_an_import_dates_the_end_of_its_drafts():
    wrap = wrap_of(job(expiration_seconds=DEFAULT_EXPIRATION_SECONDS))
    (expiration,) = [tag[1] for tag in wrap["tags"] if tag[0] == "expiration"]
    assert int(expiration) == wrap["created_at"] + 90 * 24 * 3600


def test_the_import_pipeline_asks_for_the_ninety_days():
    from nostr.imports.pipeline import ImportItemsJob
    factory, created = make_factory()
    importer = ImportItemsJob(
        items=[make_item("Post", guid="g")], feed_url="https://blog.example/feed",
        profile=PROFILE, relay_pool=None, relay_directory=FakeRelayDirectory(),
        session_pool=None, is_imported=lambda _d: False, fetcher=FakeFetcher(),
        long_form_fetcher=FakeLongFormFetcher(None),
        publish_job_factory=factory, run_blocking=inline_run_blocking, pacer=RecordingPacer())
    importer.start()
    settle()
    assert created[0].kwargs["expiration_seconds"] == DEFAULT_EXPIRATION_SECONDS


def test_the_editor_save_passes_no_expiration(monkeypatch):
    """Saving from the editor, also an imported draft the person changed,
    makes a draft that stays: the save path asks for no end date (review
    L16: this used to read MainWindow's source)."""
    import types
    from PySide6.QtCore import QObject, Signal
    import main_window
    made = []

    class RecordingJob(QObject):
        status_changed = Signal(str)
        signed = Signal(dict)
        stashed = Signal(str, str, int)
        completed = Signal(list)
        failed = Signal(str)

        def __init__(self, **kwargs):
            super().__init__()
            made.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(main_window, "DraftPublishJob", RecordingJob)
    host = types.SimpleNamespace(
        _is_stash_in_flight_for=lambda _ed: False, _relay_pool=None,
        _relay_directory=FakeRelayDirectory(), _session_pool=None,
        _entitled_relays=lambda: (), status=types.SimpleNamespace(showMessage=print),
        _attach_active_stash=lambda _ed, _job: None)
    host._draft_job = types.MethodType(MainWindow._draft_job, host)
    choice = types.SimpleNamespace(identifier="rss-0123456789abcdef")
    inner = {"kind": 30023, "content": "An edited import", "tags": [["d", "x"]],
             "created_at": 1_800_000_000, "pubkey": PROFILE.user_pubkey}
    MainWindow._fire_draft_publish_job(host, object(), PROFILE, choice, inner)
    assert len(made) == 1
    assert made[0].get("expiration_seconds") is None


def test_a_draft_that_still_has_an_end_date_says_so_in_the_list():
    """Engine review M6: drafts saved before drafts stopped expiring keep
    their old end date until they are saved again; the list says when,
    and how to keep them."""
    from nostr.draft_store import DraftRecord, DraftState
    from nostr.ui.drafts_panel import _accessible_row_text, _display_meta, expiry_note
    end = 1_800_000_000
    record = DraftRecord(identifier="note-1", inner_kind=1, state=DraftState.READY,
                         title="A note", snippet="first words", expiration=end)
    note = expiry_note(end)
    assert note.startswith("Removed on ") and note.endswith("unless you save it again")
    assert _display_meta(record) == note
    assert note in _accessible_row_text(record, now=end - 30 * 86400)
    kept = DraftRecord(identifier="note-2", inner_kind=1, state=DraftState.READY,
                       title="Kept", snippet="first words")
    assert _display_meta(kept) == "first words"
    assert expiry_note(None) == ""


def test_an_imported_draft_keeps_its_preview_and_says_its_end_once_elsewhere():
    """CP2 review N6: every imported draft's row showed the expiry instead
    of its preview, cut at the panel's width; Q-11 says it once, where
    drafts are created. The spoken row still says it (the row's tooltip
    shows only a title the row cuts short)."""
    from nostr.draft_store import DraftRecord, DraftState
    from nostr.ui.drafts_panel import (_accessible_row_text, _display_meta, expiry_note,
                                       is_imported)
    end = 1_800_000_000
    imported = DraftRecord(identifier="rss-0123456789abcdef", inner_kind=30023,
                           state=DraftState.READY, title="An article",
                           snippet="first words", expiration=end,
                           inner_tags=[["source", "https://blog.example/feed"]])
    assert is_imported(imported)
    assert _display_meta(imported) == "blog.example · first words"
    assert expiry_note(end) in _accessible_row_text(imported, now=end - 30 * 86400)
    by_name = DraftRecord(identifier="rss-fedcba9876543210", inner_kind=30023,
                          state=DraftState.READY, title="A file's post", snippet="words",
                          expiration=end)
    assert is_imported(by_name) and _display_meta(by_name) == "words"


def test_a_date_is_not_padded():
    """CP2 review: "04 January 2027" read oddly. The day is written the
    language's own way (i18n.format_day): in English, January 4, 2027."""
    import datetime
    from nostr.ui.drafts_common import format_absolute_date
    moment = int(datetime.datetime(2027, 1, 4, 12, 0).timestamp())
    assert format_absolute_date(moment) == "January 4, 2027"
