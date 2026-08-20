# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Nostr source layer: entity extraction, NIP-05, event
normalisation, wiki normalisation, and the two Nostr resolvers.

Resolvers run against a scripted fake relay-query surface and the fake
HTTP fetcher, so everything settles synchronously with no relays and no
network.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nostr.bech32 import (
    decode_nevent,
    encode_naddr,
    encode_nevent,
    encode_nprofile,
    encode_npub,
    encode_note,
)
from nostr.imports.constants import NOSTRHUB_NIP_KIND
from nostr.imports.errors import ERROR_CODES
from nostr.imports.registry import ResolveInput, detect_resolver, resolve_source
from nostr.imports.sources.mdx import derive_summary
from nostr.imports.sources.nostr import (
    dedup_relays,
    event_tag,
    extract_nip05,
    extract_nostr_entity,
    nostr_event_to_item,
    resolve_nip05,
)
from nostr.imports.sources.wiki import normalize_wiki_content

from tests.imports_fakes import FakeFetcher

PK = "ab" * 32
PK2 = "cd" * 32
EVENT_ID = "ef" * 32

NPUB = encode_npub(PK)
NPROFILE = encode_nprofile(PK, ["wss://hint.example"])
NOTE = encode_note(EVENT_ID)
NEVENT = encode_nevent(EVENT_ID, ["wss://hint.example"], PK, 30023)
NADDR_ARTICLE = encode_naddr("my-post", PK, 30023, ["wss://hint.example"])
NADDR_NIP = encode_naddr("nip-x", PK, NOSTRHUB_NIP_KIND, [])


class FakeNostrQuery:
    """Scripted relay-query fake: results are consumed in call order."""

    def __init__(self, script):
        # script: list of ("latest"|"addressable", result)
        self.script = list(script)
        self.calls = []  # (method, relays, filters)

    def _next(self, method, relays, filters):
        self.calls.append((method, list(relays), list(filters)))
        assert self.script, f"unexpected {method} query: {filters}"
        expected, result = self.script.pop(0)
        assert expected == method, f"expected {expected}, got {method}"
        return result

    def latest(self, relays, filters, on_done):
        on_done(self._next("latest", relays, filters))

    def addressable(self, relays, filters, on_done):
        on_done(self._next("addressable", relays, filters))


def article_event(d_tag="my-post", pubkey=PK, kind=30023, content="# Hello\n\nBody.",
                  created_at=1700000000, tags=()):
    return {
        "id": EVENT_ID,
        "kind": kind,
        "pubkey": pubkey,
        "created_at": created_at,
        "content": content,
        "tags": [["d", d_tag], ["title", "Hello"], *map(list, tags)],
    }


class Sink:
    def __init__(self):
        self.result = None
        self.error = None

    def on_success(self, result):
        assert self.result is None and self.error is None
        self.result = result

    def on_failure(self, error):
        assert self.result is None and self.error is None
        self.error = error


def run(url, query=None, fetcher=None):
    sink = Sink()
    resolve_source(
        url,
        fetcher=fetcher or FakeFetcher(),
        on_success=sink.on_success,
        on_failure=sink.on_failure,
        nostr_query=query,
    )
    return sink


# --------------------------------------------------------------------------- #
# Pure: entities, NIP-05, event normalisation, wiki                           #
# --------------------------------------------------------------------------- #

class TestEntityExtraction(unittest.TestCase):
    def test_bare_forms(self):
        self.assertEqual(extract_nostr_entity(NPUB).type, "npub")
        self.assertEqual(extract_nostr_entity(NPUB).pubkey, PK)
        self.assertEqual(extract_nostr_entity(NPROFILE).relays,
                         ("wss://hint.example",))
        self.assertEqual(extract_nostr_entity(NOTE).event_id, EVENT_ID)
        nevent = extract_nostr_entity(NEVENT)
        self.assertEqual((nevent.event_id, nevent.pubkey, nevent.kind),
                         (EVENT_ID, PK, 30023))
        naddr = extract_nostr_entity(NADDR_ARTICLE)
        self.assertEqual((naddr.identifier, naddr.kind), ("my-post", 30023))

    def test_nostr_uri_prefix_and_embedded_urls(self):
        self.assertIsNotNone(extract_nostr_entity(f"nostr:{NPUB}"))
        self.assertIsNotNone(
            extract_nostr_entity(f"https://njump.me/{NADDR_ARTICLE}"))
        self.assertIsNotNone(
            extract_nostr_entity(f"https://habla.news/u/{NPROFILE}"))

    def test_rejects_non_entities(self):
        self.assertIsNone(extract_nostr_entity("https://example.com/feed"))
        self.assertIsNone(extract_nostr_entity("npub1invalidchecksum"))
        self.assertIsNone(extract_nostr_entity(""))
        self.assertIsNone(extract_nostr_entity(None))


class TestNip05(unittest.TestCase):
    def test_bare_address(self):
        addr = extract_nip05("alice@example.com")
        self.assertEqual((addr.name, addr.domain), ("alice", "example.com"))
        self.assertEqual(addr.address, "alice@example.com")

    def test_nostr_prefix_and_case(self):
        addr = extract_nip05("nostr:Alice@Example.COM")
        self.assertEqual(addr.address, "alice@example.com")

    def test_urls_with_at_are_not_claimed(self):
        self.assertIsNone(extract_nip05("https://x.com/a@b.com"))
        self.assertIsNone(extract_nip05("mailto:a@b.com x"))

    def test_resolution_happy_path(self):
        body = json.dumps({
            "names": {"alice": PK},
            "relays": {PK: ["wss://alice.example"]},
        })
        fetcher = FakeFetcher({
            "https://example.com/.well-known/nostr.json?name=alice":
                ("ok", body),
        })
        out = {}
        resolve_nip05(fetcher, extract_nip05("alice@example.com"),
                      lambda r: out.update(result=r))
        self.assertEqual(out["result"], (PK, ["wss://alice.example"]))

    def test_resolution_failures_return_none(self):
        cases = [
            ("err", "HTTP 404"),
            ("ok", "not json"),
            ("ok", json.dumps({"names": {}})),
            ("ok", json.dumps({"names": {"alice": "nothex"}})),
        ]
        for kind, payload in cases:
            fetcher = FakeFetcher({
                "https://example.com/.well-known/nostr.json?name=alice":
                    (kind, payload),
            })
            out = {}
            resolve_nip05(fetcher, extract_nip05("alice@example.com"),
                          lambda r: out.update(result=r))
            self.assertIsNone(out["result"], msg=str((kind, payload)))


class TestEventNormalisation(unittest.TestCase):
    def test_article_event(self):
        item = nostr_event_to_item(article_event(
            tags=[["t", "bitcoin"], ["summary", "A summary"],
                  ["image", "https://img.example/x.png"],
                  ["published_at", "1600000000"]]))
        self.assertEqual(item.guid, f"30023:{PK}:my-post")
        self.assertEqual(item.title, "Hello")
        self.assertEqual(item.published_at, 1600000000)
        self.assertEqual(item.categories, ("bitcoin",))
        self.assertEqual(item.summary, "A summary")
        self.assertEqual(item.image, "https://img.example/x.png")
        self.assertTrue(item.link.startswith("https://njump.me/naddr1"))
        self.assertEqual(item.content_markdown, "# Hello\n\nBody.")
        self.assertEqual(item.content_html, "")

    def test_untitled_note_derives_title_from_content(self):
        event = {"id": EVENT_ID, "kind": 1, "pubkey": PK,
                 "created_at": 1700000000,
                 "content": "## A heading line\n\nMore prose.", "tags": []}
        item = nostr_event_to_item(event)
        self.assertEqual(item.title, "A heading line")
        self.assertEqual(item.guid, EVENT_ID)
        self.assertTrue(item.link.startswith("https://njump.me/nevent1"))

    def test_image_led_note_skips_the_hero_image(self):
        # A body that opens with a hero image used to title the row
        # "![One Class, One Purpose](https://...)", which the drafts
        # panel then cut mid-word.
        event = {"id": EVENT_ID, "kind": 1, "pubkey": PK,
                 "created_at": 1700000000,
                 "content": ("![One Class, One Purpose](https://x/hero.png)\n"
                             "\n# One Class, One Purpose\n\nBody."),
                 "tags": []}
        item = nostr_event_to_item(event)
        self.assertEqual(item.title, "One Class, One Purpose")

    def test_link_led_note_uses_the_link_text(self):
        event = {"id": EVENT_ID, "kind": 1, "pubkey": PK,
                 "created_at": 1700000000,
                 "content": "[Read the full post](https://example.com/x)",
                 "tags": []}
        item = nostr_event_to_item(event)
        self.assertEqual(item.title, "Read the full post")
        self.assertNotIn("http", item.title)

    def test_all_image_note_falls_back_to_the_event_placeholder(self):
        # Nothing in the body reads as words, so the derivation yields
        # nothing and the caller's "Nostr note <id>" placeholder wins,
        # rather than the row showing image syntax.
        event = {"id": EVENT_ID, "kind": 1, "pubkey": PK,
                 "created_at": 1700000000,
                 "content": "![a](1)\n![b](2)\n", "tags": []}
        item = nostr_event_to_item(event)
        self.assertNotIn("![", item.title)
        self.assertTrue(item.title.startswith("Nostr note "))

    def test_wiki_event_normalised_and_tagged(self):
        event = article_event(kind=30818,
                              content="== Section\n\nSee [[Other Page|that page]].")
        item = nostr_event_to_item(event)
        self.assertIn("# Section", item.content_markdown)
        self.assertIn("that page", item.content_markdown)
        self.assertNotIn("[[", item.content_markdown)
        self.assertIn("wiki", item.categories)


class TestWikiNormalisation(unittest.TestCase):
    def test_wikilinks(self):
        self.assertEqual(normalize_wiki_content("See [[Target]]."), "See Target.")
        self.assertEqual(
            normalize_wiki_content("See [[target|Display Text]]."),
            "See Display Text.")
        self.assertEqual(normalize_wiki_content("See [display][]."), "See display.")

    def test_asciidoc_headings(self):
        self.assertEqual(normalize_wiki_content("== Section =="), "## Section")
        self.assertEqual(normalize_wiki_content("= Top\nbody"), "# Top\nbody")

    def test_markdown_left_untouched(self):
        md = "# Title\n\nA [link](https://x.example) and *emphasis*."
        self.assertEqual(normalize_wiki_content(md), md)

    def test_fenced_code_protected(self):
        md = "```\n[[not a link]]\n== not a heading\n```"
        self.assertEqual(normalize_wiki_content(md), md)


class TestDeriveSummary(unittest.TestCase):
    def test_prefers_substantial_line(self):
        md = "# Title\n\nTEIL 00\n\nThe real opening sentence with plenty of words."
        self.assertEqual(
            derive_summary(md),
            "The real opening sentence with plenty of words.")

    def test_falls_back_to_short_line(self):
        self.assertEqual(derive_summary("# T\n\nShort."), "Short.")

    def test_empty(self):
        self.assertEqual(derive_summary(""), "")
        self.assertEqual(derive_summary("# only a heading"), "")


class TestBech32Nevent(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_nevent(EVENT_ID, ["wss://r.example"], PK, 30023)
        event_id, relays, author, kind = decode_nevent(encoded)
        self.assertEqual(event_id, EVENT_ID)
        self.assertEqual(relays, ["wss://r.example"])
        self.assertEqual(author, PK)
        self.assertEqual(kind, 30023)

    def test_minimal_form(self):
        encoded = encode_nevent(EVENT_ID)
        event_id, relays, author, kind = decode_nevent(encoded)
        self.assertEqual(event_id, EVENT_ID)
        self.assertEqual((relays, author, kind), ([], None, None))


class TestDedupRelays(unittest.TestCase):
    def test_order_preserving_case_insensitive(self):
        out = dedup_relays(
            ["wss://A.example/", "wss://b.example"],
            ["wss://a.example", "wss://c.example", ""],
        )
        self.assertEqual(out, ["wss://A.example/", "wss://b.example",
                               "wss://c.example"])


# --------------------------------------------------------------------------- #
# Resolvers                                                                   #
# --------------------------------------------------------------------------- #

class TestDetection(unittest.TestCase):
    def test_npub_claims_nostr_resolver(self):
        self.assertEqual(detect_resolver(NPUB).id, "nostr")
        self.assertEqual(detect_resolver(f"nostr:{NPUB}").id, "nostr")

    def test_nip05_claims_nostr_resolver(self):
        self.assertEqual(detect_resolver("alice@example.com").id, "nostr")

    def test_nostrhub_url_claims_nostrhub_before_nostr(self):
        self.assertEqual(
            detect_resolver(f"https://nostrhub.io/{NPUB}").id, "nostrhub")

    def test_bare_nip_naddr_claims_nostrhub(self):
        self.assertEqual(detect_resolver(NADDR_NIP).id, "nostrhub")

    def test_bare_article_naddr_claims_nostr(self):
        self.assertEqual(detect_resolver(NADDR_ARTICLE).id, "nostr")

    def test_plain_urls_stay_with_rss(self):
        self.assertEqual(detect_resolver("https://example.com/feed").id, "rss")


class TestNostrResolver(unittest.TestCase):
    def test_single_naddr_event(self):
        query = FakeNostrQuery([("latest", article_event())])
        sink = run(f"nostr:{NADDR_ARTICLE}", query)
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(feed.format, "nostr")
        self.assertEqual(len(feed.items), 1)
        self.assertEqual(feed.items[0].content_markdown, "# Hello\n\nBody.")
        # naddr hint relays included in the query.
        method, relays, filters = query.calls[0]
        self.assertIn("wss://hint.example", relays)
        self.assertEqual(filters[0]["#d"], ["my-post"])

    def test_single_event_by_nevent_id(self):
        query = FakeNostrQuery([("latest", article_event())])
        sink = run(NEVENT, query)
        self.assertIsNone(sink.error)
        _method, _relays, filters = query.calls[0]
        self.assertEqual(filters[0]["ids"], [EVENT_ID])

    def test_missing_event_yields_not_found(self):
        query = FakeNostrQuery([("latest", None)])
        sink = run(f"nostr:{NADDR_ARTICLE}", query)
        self.assertEqual(sink.error.code, ERROR_CODES.NOSTR_NOT_FOUND)

    def test_author_flow_via_npub(self):
        outbox = {"kind": 10002, "pubkey": PK, "created_at": 1,
                  "tags": [["r", "wss://write.example", "write"],
                           ["r", "wss://read.example", "read"]],
                  "content": ""}
        articles = [
            article_event(d_tag="a", created_at=100),
            article_event(d_tag="b", created_at=200),
        ]
        meta = {"kind": 0, "pubkey": PK, "created_at": 1,
                "content": json.dumps({"display_name": "Alice"}), "tags": []}
        query = FakeNostrQuery([
            ("latest", outbox),
            ("addressable", articles),
            ("latest", meta),
        ])
        sink = run(NPUB, query)
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(feed.title, "Alice · Articles")
        self.assertEqual(len(feed.items), 2)
        # The article query ran against the author's write relays.
        _m, relays, _f = query.calls[1]
        self.assertIn("wss://write.example", relays)
        # A bare npub canonicalises to an njump URL.
        self.assertTrue(sink.result.url.startswith("https://njump.me/"))

    def test_author_with_no_articles_yields_empty_feed(self):
        query = FakeNostrQuery([
            ("latest", None),          # no outbox event
            ("addressable", []),       # no articles
            ("latest", None),          # no metadata
        ])
        sink = run(NPUB, query)
        self.assertIsNone(sink.error)
        self.assertEqual(len(sink.result.feed.items), 0)

    def test_nip05_flow(self):
        body = json.dumps({"names": {"alice": PK}})
        fetcher = FakeFetcher({
            "https://example.com/.well-known/nostr.json?name=alice":
                ("ok", body),
        })
        query = FakeNostrQuery([
            ("latest", None),
            ("addressable", [article_event()]),
            ("latest", None),
        ])
        sink = run("alice@example.com", query, fetcher)
        self.assertIsNone(sink.error)
        # The NIP-05 address stays the canonical, re-resolvable source.
        self.assertEqual(sink.result.url, "alice@example.com")

    def test_nip05_miss(self):
        fetcher = FakeFetcher({
            "https://example.com/.well-known/nostr.json?name=alice":
                ("err", "HTTP 404"),
        })
        sink = run("alice@example.com", FakeNostrQuery([]), fetcher)
        self.assertEqual(sink.error.code, ERROR_CODES.NIP05_NOT_FOUND)

    def test_no_relay_access_is_a_clear_error(self):
        sink = run(NPUB, query=None)
        self.assertEqual(sink.error.code, ERROR_CODES.NO_RELAY_ACCESS)


class TestNostrhubResolver(unittest.TestCase):
    def _nip_event(self, d_tag="nip-x", created_at=100):
        return {
            "id": EVENT_ID, "kind": NOSTRHUB_NIP_KIND, "pubkey": PK,
            "created_at": created_at,
            "content": "NIP body prose that is long enough to summarise.",
            "tags": [["d", d_tag], ["title", f"NIP {d_tag}"],
                     ["k", "30023"]],
        }

    def test_single_nip_naddr(self):
        meta = {"kind": 0, "pubkey": PK, "created_at": 1,
                "content": json.dumps({"name": "bob"}), "tags": []}
        query = FakeNostrQuery([
            ("latest", self._nip_event()),
            ("latest", meta),
        ])
        sink = run(NADDR_NIP, query)
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(feed.title, "bob · NostrHub NIPs")
        item = feed.items[0]
        self.assertEqual(item.guid, f"{NOSTRHUB_NIP_KIND}:{PK}:nip-x")
        self.assertIn("kind-30023", item.categories)
        self.assertIn("nip", item.categories)
        self.assertTrue(item.link.startswith("https://nostrhub.io/naddr1"))
        # Ditto relays are always queried.
        _m, relays, _f = query.calls[0]
        self.assertIn("wss://relay.ditto.pub", relays)

    def test_author_flow_on_hub_url(self):
        query = FakeNostrQuery([
            ("addressable", [self._nip_event("a", 100),
                             self._nip_event("b", 200)]),
            ("latest", None),
        ])
        sink = run(f"https://nostrhub.io/{NPUB}", query)
        self.assertIsNone(sink.error)
        feed = sink.result.feed
        self.assertEqual(len(feed.items), 2)
        # Newest first.
        self.assertEqual(feed.items[0].guid.endswith(":b"), True)
        # The pasted hub URL stays canonical.
        self.assertEqual(sink.result.url, f"https://nostrhub.io/{NPUB}")

    def test_missing_nip_yields_not_found(self):
        query = FakeNostrQuery([("latest", None)])
        sink = run(NADDR_NIP, query)
        self.assertEqual(sink.error.code, ERROR_CODES.NOSTR_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
