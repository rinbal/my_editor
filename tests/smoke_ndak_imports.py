# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check against ndak: importing feeds and Nostr articles into drafts.

Not run by pytest (no ``test_`` prefix). Run with the ndak relays up:

    (cd ~/projects/ndak && docker compose up -d)
    .venv/bin/python tests/smoke_ndak_imports.py

A small RSS 2.0, Atom and JSON Feed are served from a local web server
(tests/smoke_ndak_common.py FakeWeb) and imported with the app's resolver
and ImportItemsJob into private drafts (kind 31234), images rehosted to a
local stand-in Blossom server. Re-importing must replace, not duplicate.
Nostr sources (naddr, npub, NIP-05, a NostrHub NIP) are read from the local
relays, and the synced feed list (kind 30078) is checked across devices.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_ndak_common as nd  # noqa: E402

nd.bootstrap("imports")
app = nd.qt_app()

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

from nostr import bech32  # noqa: E402
from nostr.imports.constants import FEED_LIST_DTAG, SUBSCRIPTIONS_KIND  # noqa: E402
from nostr.imports.fetch import SourceFetcher  # noqa: E402
from nostr.imports.pipeline import ImportItemsJob  # noqa: E402
from nostr.imports.registry import ResolveInput, resolve_source  # noqa: E402
from nostr.imports.sources.nostr import RelayQueryAdapter  # noqa: E402
from nostr.imports.subscriptions import FeedSubscriptionStore  # noqa: E402
from nostr.ui.account_windows import local_profile  # noqa: E402

RNOSTR, NRS, STRFRY, OBELISK, KHATRU = nd.RNOSTR, nd.NRS, nd.STRFRY, nd.OBELISK, nd.KHATRU
W = nd.FAKE_WEB
rep = nd.Report("imports")
web = nd.FakeWeb().start()
blossom = nd.StandInBlossom(7802).start()


def png(color: str) -> bytes:
    image = QImage(32, 24, QImage.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


run = uuid.uuid4().hex[:6]
web.put("/img/cover.png", png("#aa2244"))
web.put("/img/inline.png", png("#22aa44"))
RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>ndak test blog {run}</title>
  <link>{W}/</link>
  <description>A feed for the live test</description>
  <item>
    <title>First post {run}</title>
    <link>{W}/posts/first</link>
    <guid isPermaLink="false">ndak-{run}-1</guid>
    <pubDate>Tue, 01 Sep 2026 10:00:00 +0000</pubDate>
    <category>Nostr</category><category>#Testing</category>
    <description>Teaser of the first post.</description>
    <content:encoded><![CDATA[<p>The <strong>first</strong> post, with a picture:</p>
      <p><img src="{W}/img/inline.png" alt="inline picture"></p>
      <ul><li>one</li><li>two</li></ul>]]></content:encoded>
    <enclosure url="{W}/img/cover.png" type="image/png" length="100"/>
  </item>
  <item>
    <title>Second post {run}</title>
    <link>{W}/posts/second</link>
    <guid isPermaLink="false">ndak-{run}-2</guid>
    <pubDate>Wed, 02 Sep 2026 11:30:00 +0000</pubDate>
    <description><![CDATA[<p>Only a description, as <em>HTML</em>, long enough to be the body of this post on its own without any full text recovery.</p>]]></description>
  </item>
  <item>
    <title>Third post {run}</title>
    <link>{W}/posts/third</link>
    <pubDate>Thu, 03 Sep 2026 08:15:00 +0000</pubDate>
    <description>No guid on this one, so its link is the identity.</description>
    <content:encoded><![CDATA[<h2>Section</h2><p>Body with a <a href="https://example.com">link</a>.</p>]]></content:encoded>
  </item>
</channel>
</rss>
"""
web.put("/feed.xml", RSS, "application/rss+xml")
web.put("/index.html", f"""<!doctype html><html><head><title>ndak blog</title>
<link rel="alternate" type="application/rss+xml" title="RSS" href="{W}/feed.xml"></head>
<body><p>Home</p></body></html>""", "text/html")
web.put("/atom.xml", f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>ndak atom {run}</title>
<id>urn:ndak:{run}</id><updated>2026-09-04T12:00:00Z</updated>
<entry><title>Atom entry</title><id>urn:ndak:{run}:e1</id><link href="{W}/atom/e1"/>
<published>2026-09-04T12:00:00Z</published><updated>2026-09-05T12:00:00Z</updated>
<content type="html">&lt;p&gt;Atom body that is long enough to stand as the article itself.&lt;/p&gt;</content>
</entry></feed>""", "application/atom+xml")
web.put("/feed.json", {"version": "https://jsonfeed.org/version/1.1", "title": f"ndak json {run}",
                       "items": [{"id": f"json-{run}-1", "url": f"{W}/json/1", "title": "JSON item",
                                  "content_html": "<p>JSON Feed body that is long enough to stand alone.</p>",
                                  "date_published": "2026-09-06T09:00:00Z", "tags": ["json"]}]},
        "application/feed+json")

a = nd.Stack("a")
alice, sk, setup = nd.local_account(a, "Alice Imports")
pk, sk_hex = alice.user_pubkey, sk.hex()
rep.check("Alice's account is set up", setup.get("ok") is True)
query = RelayQueryAdapter(a.pool, timeout_ms=4_000)


KEEP = []      # fetchers live as long as the run, as the feeds panel's does


def resolve(value, fetcher=None, directory=None):
    box = {}
    fetcher = fetcher or SourceFetcher()
    KEEP.append(fetcher)
    resolve_source(ResolveInput(url=value), fetcher=fetcher,
                   on_success=lambda r: box.update(ok=r), on_failure=lambda e: box.update(err=e),
                   nostr_query=query, relay_directory=directory or a.directory)
    nd.wait_until(lambda: box, 30_000)
    return box


def run_import(result, **kwargs):
    # Newer builds make the job ask whether a post is already imported
    # (is_imported is required, so nothing can overwrite by forgetting it).
    # This area drives the bare job to check what it writes, so it answers
    # "not imported"; the app's own import path, which never overwrites an
    # edited draft, is exercised by the M3 flows.
    if "is_imported" in inspect.signature(ImportItemsJob).parameters:
        kwargs.setdefault("is_imported", lambda _identifier: False)
    job = ImportItemsJob(items=list(result.feed.items), feed_url=result.url, profile=alice,
                         relay_pool=a.pool, relay_directory=a.directory, session_pool=a.sessions,
                         entitled_relays=(), **kwargs)
    ids, failures, published = [], [], []
    job.item_succeeded.connect(lambda i, ident: ids.append(ident))
    job.item_failed.connect(lambda i, reason: failures.append(reason))
    job.item_published.connect(lambda i, ok, total: published.append((ok, total)))
    done = nd.Catch(job.completed)
    job.start()
    done.wait(120_000)
    return job, ids, failures, published, done.last


def wraps_for(identifiers, relay):
    return nd.nak_req([relay], kinds=[31234], authors=[pk], tags={"d": list(identifiers)})


def inner_of(wrap):
    out = nd.nak("decrypt", "--sec", sk_hex, "-p", pk, wrap["content"])
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None


# --------------------------------------------------------------------------- #
rep.section("RSS 2.0 into private drafts, images rehosted to Blossom")

box = resolve(f"{W}/feed.xml")
result = box.get("ok")
rep.check("the feed resolves with its three items", result is not None and len(result.feed.items) == 3,
          str(box.get("err")))
if result is not None:
    job, ids, failures, published, completed = run_import(
        result, fetch_full_text=False, rehost_images=True, blossom_server=nd.BLOSSOM_B)
    rep.check("all three items become drafts", completed == (3, 3) and len(ids) == 3 and not failures,
              f"completed {completed}, failures {failures}")
    expected_ids = ["rss-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
                    for seed in (f"ndak-{run}-1", f"ndak-{run}-2", f"{W}/posts/third")]
    rep.check("d tags are rss- plus the first 16 hex of sha256(guid, else link)",
              ids == expected_ids, f"{ids} vs {expected_ids}")
    rep.check("every draft was accepted by its relays", all(ok == total and ok > 0 for ok, total in published),
              str(published))
    first_wraps = {w["id"] for w in wraps_for(ids, RNOSTR)}
    wrap = next((w for w in wraps_for(ids[:1], RNOSTR)), None)
    if wrap:
        nd.review_event(rep, "imported draft wrap", wrap, kind=31234)
        exp = int(nd.tag_values(wrap, "expiration")[0])
        rep.check("NIP-40: an imported draft expires 90 days after it was saved",
                  exp - wrap["created_at"] == 90 * 86400 and nd.tag_values(wrap, "k") == ["30023"],
                  f"{(exp - wrap['created_at']) / 86400:.1f} days, k {nd.tag_values(wrap, 'k')}")
        inner = inner_of(wrap)
        if rep.check("the wrap decrypts to an unsigned kind 30023", inner is not None
                     and inner.get("kind") == 30023 and "sig" not in inner):
            tags = inner["tags"]
            tv = lambda n: [t[1] for t in tags if t[0] == n]
            rep.check("inner d equals the wrap's d", tv("d") == ids[:1])
            rep.check("title, summary and published_at come from the feed",
                      tv("title") == [f"First post {run}"] and tv("summary") == ["Teaser of the first post."]
                      and tv("published_at") == [str(1788256800)], json.dumps(tags)[:400])
            rep.check("categories become lowercased t tags", tv("t") == ["nostr", "testing"], str(tv("t")))
            rep.check("the source tag names the feed", tv("source") == [f"{W}/feed.xml"], str(tv("source")))
            images = tv("image")
            rep.check("the cover image (the enclosure) becomes the image tag", images == [f"{W}/img/cover.png"]
                      or (len(images) == 1 and images[0].startswith(nd.BLOSSOM_B)), str(images))
            if images and not images[0].startswith(nd.BLOSSOM_B):
                rep.info("cover image not rehosted", "only images in the body are rehosted; the "
                         "enclosure cover stays a link to the original blog (pipeline.py "
                         "maps the cover only when the same URL was rehosted from the body)")
            rep.check("the inline image is rehosted and described by an imeta tag",
                      nd.BLOSSOM_B in inner["content"] and any(t[0] == "imeta" for t in tags)
                      and f"{W}/img/inline.png" not in inner["content"],
                      json.dumps([t for t in tags if t[0] == "imeta"])[:300])
            rep.check("the body is Markdown with the source link footer",
                      ("**first**" in inner["content"] or "__first__" in inner["content"])
                      and "- one" in inner["content"]
                      and f"[{W}/posts/first]({W}/posts/first)" in inner["content"],
                      repr(inner["content"][:200]))
            rep.event("imported draft (inner 30023)", wrap, present=nd.presence(wrap["id"]))
    second = next((w for w in wraps_for(ids[1:2], RNOSTR)), None)
    inner2 = inner_of(second) if second else None
    if inner2:
        # A post whose source gives no summary gets its opening words as
        # one (nostr/rss/normalize.py summary_from_body, since 19645e2).
        summary = [t[1] for t in inner2["tags"] if t[0] == "summary"]
        rep.check("an item with only a description has the text as its body and its opening "
                  "words as summary",
                  len(summary) == 1 and summary[0].startswith("Only a description")
                  and "Only a description" in inner2["content"], str(summary))
    rehost_log = [e for e in blossom.log() if e.get("method") == "PUT"]
    rep.check("rehosting uploads (BUD-02) each body image with an auth signed by the account",
              len(rehost_log) >= 1 and all(e["verdict"] == "ok" and e["auth"]["pubkey"] == pk
                                           for e in rehost_log),
              str([(e.get("path"), e.get("verdict")) for e in rehost_log]))
    if rehost_log:
        nd.review_event(rep, "BUD-11 auth for an import rehost", rehost_log[0]["auth"], kind=24242,
                        max_age_s=900)

    # ----------------------------------------------------------------------- #
    rep.section("Re-importing the same feed replaces, never duplicates")
    time.sleep(1.1)
    job2, ids2, failures2, _published2, completed2 = run_import(
        result, fetch_full_text=False, rehost_images=False)
    rep.check("the second import keeps the same d tags", ids2 == ids, f"{ids2}")
    for relay in (RNOSTR, NRS):
        found = wraps_for(ids, relay)
        rep.check(f"{nd.RELAY_NAMES[relay]} holds exactly one wrap per item, the new ones",
                  len(found) == 3 and not ({w["id"] for w in found} & first_wraps),
                  f"{len(found)} wraps")

# --------------------------------------------------------------------------- #
rep.section("Discovery, Atom and JSON Feed")

box = resolve(f"{W}/")
rep.check("a homepage is resolved to its feed through <link rel=alternate>",
          box.get("ok") is not None and box["ok"].url == f"{W}/feed.xml",
          box["ok"].url if box.get("ok") else str(box.get("err")))
box = resolve(f"{W}/atom.xml")
item = box["ok"].feed.items[0] if box.get("ok") and box["ok"].feed.items else None
rep.check("Atom resolves; published wins over updated",
          item is not None and item.published_at == 1788523200,
          str(item.published_at if item else box.get("err")))
box = resolve(f"{W}/feed.json")
item = box["ok"].feed.items[0] if box.get("ok") and box["ok"].feed.items else None
rep.check("JSON Feed resolves with its tags", item is not None and item.categories == ("json",),
          str(item.categories if item else box.get("err")))

# --------------------------------------------------------------------------- #
rep.section("Nostr sources: an naddr, an npub's articles, a NIP-05 name")

_, bob_hex, bob = nd.new_identity()
nd.nak_publish(bob_hex, [STRFRY, KHATRU], kind=0, content=json.dumps({"name": "Bob Writer"}))
nd.nak_publish(bob_hex, [STRFRY, KHATRU], kind=10002, tags=[["r", KHATRU]])
slug = f"bob-article-{run}"
article = nd.nak_publish(bob_hex, [KHATRU], kind=30023, content=f"# Bob's article\n\nBody {run}.",
                         tags=[["d", slug], ["title", "Bob's article"], ["published_at", "1788000000"]])
naddr_hint = bech32.encode_naddr(slug, bob, 30023, [KHATRU])
naddr_bare = bech32.encode_naddr(slug, bob, 30023, [])
box = resolve(naddr_bare)
rep.check("as shipped: an article only on the author's ws://localhost relay is not found "
          "(other people's ws:// relays are refused)",
          box.get("err") is not None or not (box.get("ok") and box["ok"].feed.items
                                             and box["ok"].feed.items[0].content_markdown),
          str(box.get("err") or "found"))
undo = nd.allow_local_relays_of_others()
box = resolve(naddr_bare)
item = box["ok"].feed.items[0] if box.get("ok") and box["ok"].feed.items else None
rep.check("with the harness patch, the naddr is read from the author's write relays (NIP-65)",
          item is not None and item.content_markdown == article["content"], str(box.get("err") or item))
box = resolve("nostr:" + naddr_hint)
rep.check("a nostr: URI with a relay hint resolves too",
          box.get("ok") is not None and box["ok"].feed.items, str(box.get("err")))
box = resolve(bech32.encode_npub(bob))
feed = box["ok"].feed if box.get("ok") else None
rep.check("an npub resolves to the author's articles, titled with their name",
          feed is not None and len(feed.items) == 1 and feed.title == "Bob Writer · Articles",
          f"{feed.title if feed else box.get('err')}")
if item is not None:
    job3, ids3, failures3, _p3, completed3 = run_import(
        box["ok"] if box.get("ok") else None, fetch_full_text=False, rehost_images=False)
    expected = "rss-" + hashlib.sha256(f"30023:{bob}:{slug}".encode()).hexdigest()[:16]
    rep.check("importing it makes one draft whose d tag comes from the article's address",
              ids3 == [expected], f"{ids3} vs {expected}")
    w = next((x for x in wraps_for(ids3, RNOSTR)), None)
    inner3 = inner_of(w) if w else None
    # The article's first heading only repeats its title, which is shown
    # above the body: it is dropped (normalize.py, since 6244138), the
    # rest is taken as written.
    rep.check("the draft carries the article's body, less the heading that repeats its title, "
              "and its original published_at",
              inner3 is not None and inner3["content"].startswith(f"Body {run}.")
              and "# Bob's article" not in inner3["content"]
              and ["published_at", "1788000000"] in inner3["tags"],
              json.dumps([inner3["content"][:80], inner3["tags"]] if inner3 else None)[:300])


class RewritingFetcher(SourceFetcher):
    """HARNESS-ONLY: sends https://<name>.test/.well-known/nostr.json to the
    local web server, since NIP-05 lookups are https only and need a
    public-looking domain."""

    def fetch(self, url, *, on_success, on_failure):
        if url.startswith("https://ndak.test/"):
            url = W + url[len("https://ndak.test"):]
        super().fetch(url, on_success=on_success, on_failure=on_failure)


web.put("/.well-known/nostr.json", {"names": {"bob": bob}, "relays": {bob: [KHATRU]}})
box = resolve("bob@ndak.test", fetcher=RewritingFetcher())
feed = box["ok"].feed if box.get("ok") else None
rep.check("a NIP-05 name resolves through nostr.json to the author's articles",
          feed is not None and len(feed.items) == 1, str(box.get("err") or feed.title))
rep.check("the nostr.json request asked for the name",
          any("/.well-known/nostr.json?name=bob" in line for line in web.requests()),
          str([line for line in web.requests() if "nostr.json" in line]))
undo()

nip = nd.nak_publish(bob_hex, [OBELISK], kind=30817, content=f"# NIP-XX\n\nA NostrHub NIP {run}.",
                     tags=[["d", f"nip-{run}"], ["title", "NIP-XX test"]])
box = resolve(bech32.encode_naddr(f"nip-{run}", bob, 30817, []))
item = box["ok"].feed.items[0] if box.get("ok") and box["ok"].feed.items else None
rep.check("a NostrHub NIP (kind 30817) is read from the NostrHub relays",
          item is not None and "A NostrHub NIP" in (item.content_markdown or ""),
          str(box.get("err") or item))

# --------------------------------------------------------------------------- #
rep.section("The synced feed list (kind 30078, NIP-78), across two devices")

subs_a = FeedSubscriptionStore(session_pool=a.sessions, relay_pool=a.pool, relay_directory=a.directory,
                               cache_dir=nd.HOME / "subs-a", debounce_ms=200)
subs_a.bind_profile(alice)
nd.spin(1500)
rep.check("adding a feed works", subs_a.add_feed(f"{W}/feed.xml", "ndak blog").get("added") is True)
rep.check("anything a resolver accepts can be subscribed (an npub)",
          subs_a.add_feed(bech32.encode_npub(bob), "Bob").get("added") is True)
rep.check("a duplicate is refused", subs_a.add_feed(f"{W}/FEED.xml").get("duplicate") is True)
subs_a.flush()
nd.wait_until(lambda: bool(nd.nak_req([RNOSTR], kinds=[SUBSCRIPTIONS_KIND], authors=[pk])), 20_000)
list_event = next(iter(nd.nak_req([RNOSTR], kinds=[SUBSCRIPTIONS_KIND], authors=[pk])), None)
if rep.check("the feed list is published as kind 30078", list_event is not None):
    nd.review_event(rep, "feed list", list_event, kind=30078)
    rep.check("its d tag namespaces it, and it says it is NIP-44 encrypted",
              nd.tag_values(list_event, "d") == [FEED_LIST_DTAG]
              # "nip44" since the list is shared with STANDUP; older builds wrote "nip44_v2".
              and nd.tag_values(list_event, "encrypted") in (["nip44"], ["nip44_v2"]),
              json.dumps(list_event["tags"]))
    opened = nd.nak("decrypt", "--sec", sk_hex, "-p", pk, list_event["content"]).stdout
    try:
        payload = json.loads(opened)
    except ValueError:
        payload = {}
    rep.check("its content decrypts (nak) to the list of feeds",
              [f["url"] for f in payload.get("feeds", [])] == [f"{W}/feed.xml", bech32.encode_npub(bob)],
              opened[:200])
    where = {r for r, ok in nd.presence(list_event["id"]).items() if ok}
    rep.check("it goes to the account's private relays", where == {RNOSTR, NRS},
              ", ".join(sorted(nd.RELAY_NAMES[r] for r in where)))
    rep.event("feed list (30078)", list_event, present=nd.presence(list_event["id"]))

b = nd.Stack("b")
b.vault.store(sk)
alice_b = local_profile(pk)
b.store.upsert(alice_b)
subs_b = FeedSubscriptionStore(session_pool=b.sessions, relay_pool=b.pool, relay_directory=b.directory,
                               cache_dir=nd.HOME / "subs-b", debounce_ms=200)
changed = nd.Catch(subs_b.feeds_changed)
subs_b.bind_profile(alice_b)
nd.wait_until(lambda: len(subs_b.feeds) == 2, 20_000)
rep.check("a second device gets the same feed list", [f.url for f in subs_b.feeds]
          == [f"{W}/feed.xml", bech32.encode_npub(bob)], str([f.url for f in subs_b.feeds]))
subs_b.remove_feed(bech32.encode_npub(bob))
subs_b.flush()
nd.spin(3000)
subs_a2 = FeedSubscriptionStore(session_pool=a.sessions, relay_pool=a.pool, relay_directory=a.directory,
                                cache_dir=nd.HOME / "subs-a", debounce_ms=200)
subs_a2.bind_profile(local_profile(pk))
nd.wait_until(lambda: len(subs_a2.feeds) == 1, 20_000)
rep.check("a removal on the second device reaches the first",
          [f.url for f in subs_a2.feeds] == [f"{W}/feed.xml"], str([f.url for f in subs_a2.feeds]))

web.stop()
blossom.stop()
a.close()
b.close()
sys.exit(rep.finish())
