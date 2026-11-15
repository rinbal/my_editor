# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an address or a file brings in (nostr/imports/intake.py).

A feed, a Nostr author and a collection are followed; a single post is
imported once and never kept as a source (EINUNDZWANZIG STANDUP's
sourceKind, the same words in both apps). An export file holds posts or
a list of sources.
"""

from __future__ import annotations

import pytest

from nostr.rss.normalize import item_to_article

from nostr.bech32 import encode_naddr
from nostr.imports.intake import (
    AUTHOR,
    COLLECTION,
    FEED,
    MAX_FILE_BYTES,
    NOT_AN_EXPORT,
    SINGLE,
    is_list_address,
    kind_of,
    kind_word,
    looks_like_markup,
    opml_titles,
    read_export,
)
from nostr.imports.snapshots import identifier_of
from nostr.imports.sources.opml import parse_opml
from tests.test_imports_files import OPML, WXR, medium_zip

NPUB = "npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237lmu63q0uf63m"
NADDR = encode_naddr(identifier="my-article", author_pubkey_hex="ab" * 32, kind=30023)


@pytest.mark.parametrize("address, kind", [
    ("https://blog.example/feed", FEED),
    ("blog.example", FEED),
    ("https://name.substack.com/feed", FEED),
    (NPUB, AUTHOR),
    ("nostr:" + NPUB, AUTHOR),
    ("alice@example.com", AUTHOR),
    ("https://example.com/sitemap.xml", COLLECTION),
    ("https://github.com/owner/repo/tree/main/posts", COLLECTION),
    ("https://raw.githubusercontent.com/owner/repo/main/post.md", SINGLE),
    ("https://bsky.app/profile/alice.example/post/3kxyz", SINGLE),
])
def test_what_an_address_is(address, kind):
    assert kind_of(address) == kind


def test_one_article_is_a_single_post():
    assert kind_of(NADDR) == SINGLE


def test_nothing_claims_an_empty_address():
    assert kind_of("") == ""
    assert kind_of("   ") == ""


def test_a_site_that_only_has_a_sitemap_is_a_collection():
    assert kind_of("https://blog.example", "sitemap") == COLLECTION
    assert kind_of("https://blog.example", "atom") == FEED


def test_kinds_have_words():
    assert kind_word(FEED) == "Feed"
    assert kind_word(AUTHOR) == "Nostr author"
    assert kind_word(COLLECTION) == "Collection"
    assert kind_word(SINGLE) == "Single post"


@pytest.mark.parametrize("address, is_list", [
    ("https://reader.example/subscriptions.opml", True),
    ("https://reader.example/export.OPML?token=1", True),
    ("https://reader.example/export.opml#top", True),
    ("https://reader.example/opml", False),
    ("https://blog.example/feed", False),
])
def test_a_list_of_sources_is_known_by_its_address(address, is_list):
    assert is_list_address(address) is is_list


def test_pasted_markup_needs_its_address():
    assert looks_like_markup("  <?xml version='1.0'?><rss/>")
    assert not looks_like_markup('{"version": "https://jsonfeed.org/version/1.1"}')


def test_the_first_titles_of_a_list_and_how_many_more():
    document = parse_opml(OPML)
    titles, more = opml_titles(document, shown=1)
    assert len(titles) == 1 and more == len(document.feeds) - 1


class TestReadingAFile:
    def write(self, tmp_path, name, data):
        path = tmp_path / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return str(path)

    def test_a_zip_export_is_read_into_posts(self, tmp_path):
        export = read_export(self.write(tmp_path, "medium-export.zip", medium_zip()))
        assert export.label == "medium-export.zip"
        assert export.items and export.sources is None

    def test_a_list_of_sources(self, tmp_path):
        export = read_export(self.write(tmp_path, "subscriptions.opml", OPML))
        assert export.sources is not None and export.sources.feeds
        assert not export.items

    def test_a_wordpress_export_is_handed_to_the_readers(self, tmp_path):
        export = read_export(self.write(tmp_path, "blog.xml", WXR))
        assert export.text.startswith("<?xml") and not export.items

    def test_a_markdown_file_is_one_post_named_by_its_heading(self, tmp_path):
        path = self.write(tmp_path, "my-first-post.md", "# Hello there\n\nSome words.\n")
        (item,) = read_export(path).items
        assert item.title == "Hello there"
        # The heading became the title, so it is not in the body twice.
        assert item.content_markdown == "Some words."

    def test_a_markdown_files_front_matter_names_it(self, tmp_path):
        path = self.write(tmp_path, "post.mdx", "---\ntitle: From the matter\n---\n"
                                                "# A heading\n\nText")
        (item,) = read_export(path).items
        assert item.title == "From the matter"
        assert item.content_markdown.startswith("# A heading")

    def test_a_markdown_files_front_matter_details_come_along(self, tmp_path):
        path = self.write(tmp_path, "post.md", (
            "---\ntitle: Sound money\ndate: 2024-03-05\n"
            "tags: Bitcoin, Nostr\ndescription: Why it matters.\n"
            "cover:\n  image: https://example.com/cover.png\n  alt: A coin\n"
            "author: Jane Doe\n---\n# Sound money\n\nThe text.\n"))
        (item,) = read_export(path).items
        assert item.title == "Sound money"
        # The title is not there twice: the heading repeating it is dropped.
        assert item_to_article(item, append_source_link=False).content == "The text."
        assert item.published_at == 1709596800
        assert item.categories == ("Bitcoin", "Nostr")
        assert item.summary == "Why it matters."
        assert item.image == "https://example.com/cover.png"
        assert item.author == "Jane Doe"

    def test_a_markdown_file_without_a_heading_is_named_by_the_file(self, tmp_path):
        (item,) = read_export(self.write(tmp_path, "notes_from-lisbon.md", "Just text.")).items
        assert item.title == "Notes from lisbon"

    def test_the_same_markdown_file_is_the_same_post(self, tmp_path):
        first = read_export(self.write(tmp_path, "a.md", "# Same\n\nBody")).items[0]
        again = read_export(self.write(tmp_path, "b.md", "# Same\n\nBody")).items[0]
        edited = read_export(self.write(tmp_path, "c.md", "# Same\n\nBody, edited")).items[0]
        assert identifier_of(first) == identifier_of(again) != identifier_of(edited)

    def test_a_zip_that_is_not_an_export(self, tmp_path):
        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "hello")
        with pytest.raises(ValueError) as failure:
            read_export(self.write(tmp_path, "other.zip", buffer.getvalue()))
        assert str(failure.value) == NOT_AN_EXPORT

    def test_a_file_too_large(self, tmp_path, monkeypatch):
        import nostr.imports.intake as intake
        monkeypatch.setattr(intake, "MAX_FILE_BYTES", 10)
        with pytest.raises(ValueError) as failure:
            read_export(self.write(tmp_path, "big.xml", "x" * 11))
        assert "too large" in str(failure.value)
        assert MAX_FILE_BYTES == 64 * 1024 * 1024

    def test_a_file_that_cannot_be_read(self, tmp_path):
        with pytest.raises(ValueError) as failure:
            read_export(str(tmp_path / "missing.xml"))
        assert str(failure.value) == (
            "That file couldn't be read: check that it is still there and try again.")


@pytest.mark.parametrize("address, whole", [
    ("https://blog.example/", True),
    ("https://blog.example", True),
    ("https://blog.example/feed", True),
    ("https://blog.example/feed/", True),
    ("https://blog.example/rss.xml", True),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=x", True),
    ("https://medium.com/feed/@someone", True),
    ("https://blog.example/index.json", True),
    ("https://blog.example/2026/10/why-self-custody/", False),
    ("https://blog.example/posts/hello", False),
    ("npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237lmu63q0uf63m", True),
])
def test_a_whole_source_or_one_post(address, whole):
    """CP2 review N5: every web address counted as a whole source."""
    from nostr.imports.intake import is_whole_source
    assert is_whole_source(address) is whole


def test_the_same_page_whatever_the_scheme_slash_or_fragment():
    from nostr.imports.intake import same_page
    assert same_page("https://www.blog.example/a/", "http://blog.example/a#top")
    assert same_page("https://blog.example/a?x=1", "https://blog.example/a/?x=1")
    assert not same_page("https://blog.example/a", "https://blog.example/b")
    assert not same_page("https://blog.example/a?x=1", "https://blog.example/a?x=2")
    assert not same_page("", "")
