# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compatibility matrix for publishing a document that already has media.

Adding NIP-92 imeta must not change what a document publishes. Two
promises are on the line and both are pinned here per scenario:

- media this app did not create survives byte-identically, so the URL
  written into the event is character for character the one the document
  held, and
- no metadata is ever invented for a URL this app did not verify, so a
  third party's image gets no tag rather than a plausible one.

The matrix, one section per row:

  our own URLs        an asset this app uploaded is fully described
  another user's      a stranger's image passes through undescribed,
   server              including when its path carries a hash we hold
  plain https         a pasted https or legacy http image is content,
                       not something to describe
  unknown mime        an unsniffable type omits ``m`` and keeps the rest
  unavailable media   an unuploaded or failed asset yields no URL and no
                       tag, and the image stays in the document
  duplicates          one tag per URL however often the image appears
  malformed URLs      hostile and unusable names produce nothing
  legacy posts        a reopened .md re-associates our own URL, and only
                       ours

Runs against the real ``AssetManager`` over the blob-store and uploader
fakes. No network, no relay, no signer.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextImageFormat
from PySide6.QtWidgets import QApplication

import image_safety
from doc_walk import iter_image_names
from editor import HtmlEditor
from main_window import MainWindow
from nostr import CLIENT_NAME
from nostr.media.assets import AssetIndex, AssetState
from nostr.media.manager import AssetManager
from nostr.publisher import build_article, build_note, build_imeta_tags

from tests.media_fakes import (
    GIF_BYTES, PNG_BYTES, PNG_HEIGHT, PNG_SHA, PNG_WIDTH, FakeBlobStore,
    FakeUploader,
)


OURS = "https://cdn.example"
THEIRS = "https://someone-else.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _Window:
    """MainWindow's publish-side media methods over a minimal stub."""

    _publish_payload = MainWindow._publish_payload
    _publish_text = MainWindow._publish_text

    def __init__(self, cache_dir):
        cache_dir = Path(cache_dir)
        self._store = FakeBlobStore(cache_dir)
        self._media_image_loader = self._store
        self._uploader = FakeUploader()
        self._asset_manager = AssetManager(
            blob_store=self._store,
            uploader=self._uploader,
            profile_provider=lambda: None,
            decoder=image_safety.decode_image_bytes,
            index=AssetIndex(path=cache_dir.parent / "media_assets.json"),
        )

    def uploaded_asset(self, data=PNG_BYTES, *, alt="a photo", servers=()):
        adopted = self._asset_manager.adopt_bytes(data, alt=alt)
        assert adopted is not None
        asset = self._asset_manager.adopt_library_file(
            sha256=adopted.sha256,
            remote_url=f"{OURS}/{adopted.sha256}",
            mime=adopted.mime,
            size=len(data),
            alt=alt,
        )
        assert asset is not None and asset.is_uploaded
        asset.servers = list(servers)
        return asset

    def local_asset(self, data=GIF_BYTES, *, alt="a sketch"):
        asset = self._asset_manager.adopt_bytes(data, alt=alt)
        assert asset is not None and not asset.is_uploaded
        return asset


def _fmt(name, alt=""):
    fmt = QTextImageFormat()
    fmt.setName(name)
    if alt:
        fmt.setProperty(QTextImageFormat.ImageAltText, alt)
    return fmt


def _editor_with(*names, text=""):
    """An editor holding one real image fragment per (name, alt) pair."""
    ed = HtmlEditor()
    cursor = ed.textCursor()
    if text:
        cursor.insertText(text)
    for entry in names:
        name, alt = entry if isinstance(entry, tuple) else (entry, "image")
        cursor.insertImage(_fmt(name, alt))
    ed.setTextCursor(cursor)
    return ed


def _tags_for(win, ed, flavor="markdown"):
    """The imeta tags a real publish of this document would carry."""
    content, media = win._publish_payload(ed, flavor)
    return content, build_imeta_tags(media, content)


def _entry(tag, key):
    prefix = f"{key} "
    return next((e[len(prefix):] for e in tag if e.startswith(prefix)), None)


def _names(ed):
    return list(iter_image_names(ed.document()))


# --------------------------------------------------------------------------- #
# Our own URLs                                                                #
# --------------------------------------------------------------------------- #

def test_our_own_uploaded_image_is_fully_described(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset()
    content, tags = _tags_for(win, _editor_with((asset.key, "a photo")))

    assert asset.remote_url in content
    assert len(tags) == 1
    assert _entry(tags[0], "url") == asset.remote_url
    assert _entry(tags[0], "x") == PNG_SHA
    assert _entry(tags[0], "m") == "image/png"
    assert _entry(tags[0], "dim") == f"{PNG_WIDTH}x{PNG_HEIGHT}"
    assert _entry(tags[0], "size") == str(len(PNG_BYTES))
    assert _entry(tags[0], "alt") == "a photo"


def test_a_confirmed_mirror_becomes_a_fallback(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(servers=[OURS, "https://mirror.example"])
    _content, tags = _tags_for(win, _editor_with(asset.key))

    assert f"fallback https://mirror.example/{PNG_SHA}" in tags[0]
    # The origin the primary URL already sits on is not repeated.
    assert f"fallback {OURS}/{PNG_SHA}" not in tags[0]


def test_the_alt_on_the_fragment_beats_the_stored_one(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(alt="stored alt")
    _content, tags = _tags_for(win, _editor_with((asset.key, "fragment alt")))
    assert _entry(tags[0], "alt") == "fragment alt"


# --------------------------------------------------------------------------- #
# Another user's server                                                       #
# --------------------------------------------------------------------------- #

def test_a_strangers_image_is_published_untouched_and_undescribed(tmp_path):
    win = _Window(tmp_path / "cache")
    foreign = f"{THEIRS}/photos/holiday.jpg"
    content, tags = _tags_for(win, _editor_with((foreign, "holiday")))

    assert content == f"![holiday]({foreign})"
    assert tags == []


def test_the_same_picture_on_a_strangers_server_is_still_not_ours(tmp_path):
    # Content addressing means a stranger hosting the identical file
    # matches our index by hash. Describing their copy, or worse treating
    # it as ours, is the silent rewrite the save path had removed.
    win = _Window(tmp_path / "cache")
    win.uploaded_asset()
    theirs = f"{THEIRS}/{PNG_SHA}.png"
    content, tags = _tags_for(win, _editor_with((theirs, "same picture")))

    assert theirs in content
    assert tags == []


def test_a_note_carries_no_tag_for_a_foreign_image(tmp_path):
    win = _Window(tmp_path / "cache")
    foreign = f"{THEIRS}/photo.jpg"
    content, media = win._publish_payload(_editor_with(foreign), "note")
    event = build_note(content, "7" * 64, media=media)

    assert foreign in event["content"]
    assert event["tags"] == [["client", CLIENT_NAME]]


# --------------------------------------------------------------------------- #
# Plain https                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "https://plain.example/logo.png",
    "http://legacy.example/banner.gif",
    "data:image/png;base64,AAAA",
])
def test_a_portable_source_survives_character_for_character(tmp_path, name):
    win = _Window(tmp_path / "cache")
    content, tags = _tags_for(win, _editor_with((name, "img")))

    assert content == f"![img]({name})"
    assert tags == []


# --------------------------------------------------------------------------- #
# Unknown mime                                                                #
# --------------------------------------------------------------------------- #

def test_an_unsniffable_type_omits_the_mime_and_keeps_the_hash(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset()
    # The asset layer's placeholder for "the bytes did not sniff".
    asset.mime = "application/octet-stream"
    _content, tags = _tags_for(win, _editor_with(asset.key))

    assert _entry(tags[0], "m") is None
    assert _entry(tags[0], "x") == PNG_SHA
    assert _entry(tags[0], "size") == str(len(PNG_BYTES))


def test_an_undecodable_image_omits_the_dimensions(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset()
    asset.width = 0
    asset.height = 0
    _content, tags = _tags_for(win, _editor_with(asset.key))

    assert _entry(tags[0], "dim") is None
    assert _entry(tags[0], "x") == PNG_SHA


# --------------------------------------------------------------------------- #
# Unavailable media                                                           #
# --------------------------------------------------------------------------- #

def test_an_asset_that_never_uploaded_yields_no_url_and_no_tag(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.local_asset()
    ed = _editor_with((asset.key, "a sketch"), text="before")
    content, tags = _tags_for(win, ed)

    assert content == "before"
    assert tags == []
    # The image is still in the document; only the event omits it.
    assert asset.key in _names(ed)


def test_a_failed_upload_yields_no_tag_and_keeps_the_image(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.local_asset()
    asset.upload_state = AssetState.FAILED
    ed = _editor_with(asset.key)
    _content, tags = _tags_for(win, ed)

    assert tags == []
    assert asset.key in _names(ed)


def test_an_uploaded_asset_missing_from_the_index_is_not_invented(tmp_path):
    win = _Window(tmp_path / "cache")
    unknown_key = "myeditor-asset:" + "d" * 64
    content, tags = _tags_for(win, _editor_with(unknown_key, text="text"))

    assert content == "text"
    assert tags == []


# --------------------------------------------------------------------------- #
# Duplicates                                                                  #
# --------------------------------------------------------------------------- #

def test_one_image_used_three_times_gets_one_tag(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset()
    ed = HtmlEditor()
    cursor = ed.textCursor()
    for index in range(3):
        cursor.insertText(f"paragraph {index} ")
        cursor.insertImage(_fmt(asset.key, "a photo"))
    ed.setTextCursor(cursor)
    content, tags = _tags_for(win, ed)

    assert content.count(asset.remote_url) == 3
    assert len(tags) == 1


def test_two_different_images_get_one_tag_each(tmp_path):
    win = _Window(tmp_path / "cache")
    first = win.uploaded_asset(PNG_BYTES, alt="png")
    second = win.uploaded_asset(GIF_BYTES, alt="gif")
    _content, tags = _tags_for(win, _editor_with(first.key, second.key))

    assert [_entry(t, "url") for t in tags] == [
        first.remote_url, second.remote_url]


# --------------------------------------------------------------------------- #
# Malformed URLs                                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "javascript:alert(1)",
    "file:///etc/passwd",
    "pics/dog.png",
    "/absolute/dog.png",
    "myeditor-asset:../../etc/passwd",
    "myeditor-asset:not-a-hash",
    "",
])
def test_an_unusable_image_name_publishes_nothing_and_describes_nothing(
        tmp_path, name):
    win = _Window(tmp_path / "cache")
    content, tags = _tags_for(win, _editor_with((name, "x"), text="prose"))

    assert content == "prose"
    assert tags == []


def test_a_hostile_name_cannot_reach_the_index(tmp_path):
    # parse_asset_key is a full match, so a traversal name is never a
    # lookup key, and find_by_url needs a real hash plus a known origin.
    win = _Window(tmp_path / "cache")
    win.uploaded_asset()
    hostile = f"https://evil.example/{PNG_SHA}/../../{PNG_SHA}"
    _content, tags = _tags_for(win, _editor_with(hostile))
    assert tags == []


# --------------------------------------------------------------------------- #
# Legacy posts                                                                #
# --------------------------------------------------------------------------- #

def test_a_reopened_md_still_describes_our_own_image(tmp_path):
    # After a .md save and reopen the fragment carries the plain URL, not
    # the asset key, so without re-association a republish would silently
    # stop carrying imeta for an image we uploaded ourselves.
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset()
    content, tags = _tags_for(win, _editor_with((asset.remote_url, "a photo")))

    assert content == f"![a photo]({asset.remote_url})"
    assert _entry(tags[0], "url") == asset.remote_url
    assert _entry(tags[0], "x") == PNG_SHA


def test_a_reopened_md_accepts_another_path_on_a_confirmed_server(tmp_path):
    win = _Window(tmp_path / "cache")
    asset = win.uploaded_asset(servers=[OURS])
    same_origin = f"{OURS}/media/{PNG_SHA}.png"
    _content, tags = _tags_for(win, _editor_with(same_origin))

    assert _entry(tags[0], "url") == same_origin
    assert _entry(tags[0], "x") == PNG_SHA


def test_a_reopened_md_leaves_an_unknown_origin_alone(tmp_path):
    win = _Window(tmp_path / "cache")
    win.uploaded_asset(servers=[OURS])
    elsewhere = f"https://unknown.example/{PNG_SHA}.png"
    content, tags = _tags_for(win, _editor_with(elsewhere))

    assert elsewhere in content
    assert tags == []


def test_a_legacy_post_with_no_media_at_all_is_unchanged(tmp_path):
    win = _Window(tmp_path / "cache")
    ed = _editor_with(text="an article from before any of this existed")
    content, media = win._publish_payload(ed, "markdown")
    event = build_article(content, "7" * 64, "legacy", media=media)

    assert content == "an article from before any of this existed"
    assert media == []
    assert not any(t[0] == "imeta" for t in event["tags"])

