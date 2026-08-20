# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Protocol-conformance tests for imported drafts (plan Phase 11).

Proves that what the importer produces behaves as NIP-23 / NIP-37 /
NIP-44 demand, end to end and without a signer: the inner events come
out of the real pipeline (fake publish factory captures them), the wrap
is built by the real ``build_draft_wrap``, and encryption round-trips
through the app's own NIP-44 v2 implementation with a locally generated
key.

Pinned claims:
- the inner event is a well-formed kind-30023 NIP-23 article (Markdown
  content; deterministic ``d`` tag; metadata tags only when non-empty;
  ``source`` + ``client`` attribution),
- the outer event is a kind-31234 NIP-37 wrap with the right ``d`` /
  ``k`` / ``expiration`` tags and NIP-44 ciphertext as content,
- a full build -> serialize -> encrypt -> decrypt -> parse round trip
  reproduces the inner event field by field,
- privacy: no plaintext marker (title, body, source URL) appears
  anywhere in the serialized outer event,
- addressable replacement: re-importing the same item yields the same
  identifier (and the migration path yields the legacy identifier).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from nostr import CLIENT_NAME
from nostr.crypto import (
    conversation_key,
    decrypt,
    encrypt,
    generate_secret_key,
    get_public_key,
)
from nostr.drafts import (
    DRAFT_WRAP_KIND,
    build_draft_wrap,
    parse_inner_event,
    parse_wrap_event,
    serialize_inner_event,
)
from nostr.imports.constants import IDENTIFIER_PREFIX, SOURCE_TAG
from nostr.rss.dtag import derive_identifier

from tests.imports_fakes import make_factory, make_item
from tests.test_imports_pipeline import FEED_URL, make_job


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


TITLE = "A Very Distinctive Title"
BODY_MARKER = "unmistakable-body-marker-phrase"

ITEM = make_item(
    TITLE,
    guid="proto-guid-1",
    link="https://blog.example/post-1",
    summary="A distinctive summary line.",
    content_html=(
        f"<p>{BODY_MARKER} with plenty of surrounding prose so the body "
        "clears the thin threshold comfortably and no recovery runs.</p>"
    ),
    published_at=1_700_000_000,
    categories=("Bitcoin", "writing"),
    image="https://blog.example/hero.png",
)


def imported_inner_event(item=ITEM, **job_kwargs):
    """Run the item through the real pipeline; return the inner event."""
    factory, created = make_factory()
    job = make_job([item], factory=factory, **job_kwargs)
    job.start()
    assert created, "pipeline produced no publish job"
    return created[0].inner_event


def tag_values(event, name):
    return [t[1] for t in event["tags"] if t and t[0] == name]


class TestInnerEventNip23:
    def test_shape_and_metadata_tags(self):
        inner = imported_inner_event()
        assert inner["kind"] == 30023
        assert isinstance(inner["content"], str)
        assert BODY_MARKER in inner["content"]
        assert "<p>" not in inner["content"]  # Markdown, never HTML

        expected_d = derive_identifier(
            guid="proto-guid-1", prefix=IDENTIFIER_PREFIX)
        assert tag_values(inner, "d") == [expected_d]
        assert tag_values(inner, "title") == [TITLE]
        assert tag_values(inner, "summary") == ["A distinctive summary line."]
        assert tag_values(inner, "image") == ["https://blog.example/hero.png"]
        assert tag_values(inner, "published_at") == ["1700000000"]
        assert sorted(tag_values(inner, "t")) == ["bitcoin", "writing"]
        assert tag_values(inner, SOURCE_TAG) == [FEED_URL]
        assert tag_values(inner, "client") == [CLIENT_NAME]

    def test_empty_metadata_emits_no_tags(self):
        bare = make_item("", guid="proto-guid-2", link=None,
                         content_html="<p>body text of the bare item</p>")
        inner = imported_inner_event(bare)
        for name in ("title", "summary", "image", "published_at", "t"):
            assert tag_values(inner, name) == [], name

    def test_identifier_is_deterministic_across_runs(self):
        first = imported_inner_event()
        second = imported_inner_event()
        assert tag_values(first, "d") == tag_values(second, "d")

    def test_migration_reuses_legacy_identifier(self):
        legacy = derive_identifier(guid="proto-guid-1")
        inner = imported_inner_event(
            identifier_exists=lambda d: d == legacy)
        assert tag_values(inner, "d") == [legacy]


class TestWrapNip37:
    def _wrap(self, inner):
        sk = generate_secret_key()
        pubkey = get_public_key(sk).hex()
        conv = conversation_key(sk, get_public_key(sk))
        ciphertext = encrypt(serialize_inner_event(inner), conv)
        identifier = tag_values(inner, "d")[0]
        wrap = build_draft_wrap(
            identifier=identifier,
            inner_kind=inner["kind"],
            encrypted_content=ciphertext,
            pubkey_hex=pubkey,
            client_name=CLIENT_NAME,
        )
        return wrap, conv, identifier

    def test_wrap_shape(self):
        inner = imported_inner_event()
        wrap, _conv, identifier = self._wrap(inner)
        assert wrap["kind"] == DRAFT_WRAP_KIND == 31234
        assert tag_values(wrap, "d") == [identifier]
        assert tag_values(wrap, "k") == [str(inner["kind"])]
        assert tag_values(wrap, "client") == [CLIENT_NAME]
        (expiration,) = tag_values(wrap, "expiration")
        assert int(expiration) > wrap["created_at"]
        # parse_wrap_event accepts what we built.
        meta = parse_wrap_event({**wrap, "id": "e" * 64})
        assert meta is not None
        assert meta.identifier == identifier
        assert meta.inner_kind == inner["kind"]

    def test_round_trip_reproduces_inner_event_exactly(self):
        inner = imported_inner_event()
        wrap, conv, _identifier = self._wrap(inner)
        plaintext = decrypt(wrap["content"], conv)
        recovered = parse_inner_event(plaintext)
        assert recovered["kind"] == inner["kind"]
        assert recovered["content"] == inner["content"]
        assert recovered["tags"] == inner["tags"]
        assert recovered["created_at"] == inner["created_at"]
        assert recovered["pubkey"] == inner["pubkey"]

    def test_privacy_nothing_sensitive_in_plaintext(self):
        inner = imported_inner_event()
        wrap, _conv, _identifier = self._wrap(inner)
        serialized = json.dumps(wrap)
        # The body, title, and source feed URL live only inside the
        # NIP-44 ciphertext; the outer event must never leak them.
        assert BODY_MARKER not in serialized
        assert TITLE not in serialized
        assert FEED_URL not in serialized
        assert "blog.example" not in serialized

    def test_wrong_key_cannot_decrypt(self):
        inner = imported_inner_event()
        wrap, _conv, _identifier = self._wrap(inner)
        other_sk = generate_secret_key()
        other_conv = conversation_key(other_sk, get_public_key(other_sk))
        with pytest.raises(Exception):
            decrypt(wrap["content"], other_conv)


class TestEncryptionFloor:
    def test_subscription_sync_is_ciphertext_only(self):
        # The feed list (kind 30078) also self-encrypts; its URLs must
        # never appear in the published event outside the ciphertext.
        import pathlib
        import tempfile

        from tests.imports_fakes import PROFILE
        from tests.test_imports_subscriptions import (
            FakeScheduler, make_store,
        )
        scheduler = FakeScheduler()
        store, publisher, _ = make_store(
            pathlib.Path(tempfile.mkdtemp()), scheduler=scheduler)
        store.bind_profile(PROFILE)
        store.add_feed("https://secret-reading-list.example/feed")
        scheduler.fire_last()
        _relays, signed = publisher.calls[0]
        outer = json.dumps(
            {k: v for k, v in signed.items() if k != "content"})
        assert "secret-reading-list" not in outer
        assert "secret-reading-list" not in signed["content"] or \
            signed["content"].startswith("ENC[")  # fake cipher wraps it
