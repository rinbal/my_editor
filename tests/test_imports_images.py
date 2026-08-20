# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nostr.imports.images``: scans, rewrite, rehost loop.

The rehost transport is a synchronous fake, so the whole loop settles
deterministically without network, Blossom, or a signer.

The last two sections cover the production transport, which downloads
each image and puts the bytes on the user's server through the shared
``nostr.blossom.replicate`` primitive. Those use a fake transport and a
fake signer for the same reason.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QApplication

from nostr.blossom.client import BlossomClient
from nostr.imports.fetch import BlobFetcher
from nostr.imports.images import (
    RehostedImage,
    blossom_rehost,
    image_label,
    rehost_images,
    rewrite_markdown_images,
    scan_html_images,
    scan_markdown_images,
)
from tests.blossom_fakes import (
    SERVER,
    FakeBlossomServer,
    FakeNam,
    FakeProfile,
    FakeReply,
    FakeSessionPool,
    FakeSigner,
    descriptor,
    json_reply,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"pretend pixels"


def setUpModule():
    # The production transport is Qt-based; the fakes are QObjects too.
    QApplication.instance() or QApplication(sys.argv)


MD = (
    "# Post\n\n"
    "![banner](https://a.example/banner.png)\n\n"
    "Some prose.\n\n"
    "![inline](https://a.example/photo%20one.jpg)\n\n"
    "![banner again](https://a.example/banner.png)\n\n"
    "![inline data](data:image/png;base64,AAAA)\n"
)


def fake_mirror(results=None):
    """url -> ("ok", mirrored_url) | ("err", reason); default mirrors."""
    table = dict(results or {})
    calls = []

    def mirror(url, on_success, on_failure):
        calls.append(url)
        kind, payload = table.get(
            url, ("ok", f"https://blossom.example/{len(calls)}"))
        if kind == "ok":
            on_success(payload)
        else:
            on_failure(payload)

    return mirror, calls


def run_rehost(markdown, mirror, **kwargs):
    out = {}
    progress = []
    rehost_images(
        markdown,
        mirror=mirror,
        on_progress=progress.append,
        on_done=lambda o: out.update(outcome=o),
        **kwargs,
    )
    assert "outcome" in out, "on_done never fired"
    return out["outcome"], progress


class TestScans(unittest.TestCase):
    def test_markdown_scan_unique_in_order_skipping_data(self):
        self.assertEqual(scan_markdown_images(MD), [
            "https://a.example/banner.png",
            "https://a.example/photo%20one.jpg",
        ])

    def test_html_scan(self):
        html = (
            '<p><img src="https://a.example/x.png"></p>'
            '<img alt="y" src="https://a.example/y.png"/>'
            '<img src="https://a.example/x.png">'
            '<img src="data:image/gif;base64,AA">'
        )
        self.assertEqual(scan_html_images(html), [
            "https://a.example/x.png",
            "https://a.example/y.png",
        ])

    def test_empty_inputs(self):
        self.assertEqual(scan_markdown_images(""), [])
        self.assertEqual(scan_html_images(None), [])

    def test_image_label(self):
        self.assertEqual(
            image_label("https://a.example/photo%20one.jpg"), "photo one.jpg")
        self.assertEqual(image_label("https://a.example"), "a.example")
        self.assertLessEqual(len(image_label("https://a.example/" + "x" * 200)), 80)


class TestRewrite(unittest.TestCase):
    def test_rewrites_mapped_and_keeps_unmapped(self):
        mapping = {"https://a.example/banner.png": "https://b.example/h1"}
        out = rewrite_markdown_images(MD, mapping)
        self.assertIn("![banner](https://b.example/h1)", out)
        self.assertIn("![banner again](https://b.example/h1)", out)
        self.assertIn("![inline](https://a.example/photo%20one.jpg)", out)

    def test_empty_mapping_is_identity(self):
        self.assertEqual(rewrite_markdown_images(MD, {}), MD)


class TestRehostLoop(unittest.TestCase):
    def test_happy_path_mirrors_unique_urls_once(self):
        mirror, calls = fake_mirror()
        outcome, progress = run_rehost(MD, mirror)
        # Two unique http URLs; the duplicate and the data: URI don't fetch.
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome.mirrored, 2)
        self.assertEqual(outcome.failed, [])
        self.assertIn("https://blossom.example/1", outcome.markdown)
        self.assertIn("https://blossom.example/2", outcome.markdown)
        self.assertNotIn("![banner](https://a.example/banner.png)",
                         outcome.markdown)

    def test_progress_lifecycle(self):
        mirror, _calls = fake_mirror()
        _outcome, progress = run_rehost(MD, mirror)
        statuses = [(p.index, p.status) for p in progress]
        # Seeded queued for both, then per-image mirroring/mirrored.
        self.assertEqual(statuses[:2], [(0, "queued"), (1, "queued")])
        self.assertIn((0, "mirroring"), statuses)
        self.assertIn((0, "mirrored"), statuses)
        self.assertIn((1, "mirrored"), statuses)
        self.assertTrue(all(p.total == 2 for p in progress))
        self.assertTrue(all(p.label for p in progress))

    def test_one_failure_keeps_original_and_continues(self):
        mirror, _ = fake_mirror({
            "https://a.example/banner.png": ("err", "server said no"),
        })
        outcome, progress = run_rehost(MD, mirror)
        self.assertEqual(outcome.mirrored, 1)
        self.assertEqual(outcome.failed, ["https://a.example/banner.png"])
        self.assertIn("![banner](https://a.example/banner.png)", outcome.markdown)
        self.assertNotIn("![inline](https://a.example/photo%20one.jpg)",
                         outcome.markdown)
        failed = [p for p in progress if p.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error, "server said no")

    def test_empty_mirror_response_counts_as_failure(self):
        mirror, _ = fake_mirror({
            "https://a.example/banner.png": ("ok", ""),
        })
        outcome, _ = run_rehost(MD, mirror)
        self.assertEqual(outcome.failed, ["https://a.example/banner.png"])

    def test_mirror_raising_counts_as_failure_and_continues(self):
        def broken(url, on_success, on_failure):
            if "banner" in url:
                raise RuntimeError("transport bug")
            on_success("https://blossom.example/ok")

        outcome, _ = run_rehost(MD, broken)
        self.assertEqual(outcome.mirrored, 1)
        self.assertEqual(len(outcome.failed), 1)

    def test_skip_urls_never_mirrored(self):
        mirror, calls = fake_mirror()
        outcome, _ = run_rehost(
            MD, mirror, skip_urls={"https://a.example/banner.png"})
        self.assertEqual(calls, ["https://a.example/photo%20one.jpg"])
        self.assertIn("![banner](https://a.example/banner.png)", outcome.markdown)

    def test_no_images_short_circuits(self):
        mirror, calls = fake_mirror()
        outcome, progress = run_rehost("plain text, no images", mirror)
        self.assertEqual(calls, [])
        self.assertEqual(progress, [])
        self.assertEqual(outcome.markdown, "plain text, no images")

    def test_cancellation_stops_but_still_delivers(self):
        cancelled = {"flag": False}

        def mirror(url, on_success, on_failure):
            cancelled["flag"] = True  # cancel after the first mirror
            on_success("https://blossom.example/first")

        outcome, _ = run_rehost(
            MD, mirror, is_cancelled=lambda: cancelled["flag"])
        # First image mirrored and rewritten; second never attempted.
        self.assertEqual(outcome.mirrored, 1)
        self.assertIn("https://blossom.example/first", outcome.markdown)
        self.assertIn("![inline](https://a.example/photo%20one.jpg)",
                      outcome.markdown)


def measuring_mirror(failures=()):
    """A transport that reports what it measured, as the real one does."""
    refuse = set(failures)
    calls = []

    def mirror(url, on_success, on_failure):
        calls.append(url)
        if url in refuse:
            on_failure("server said no")
            return
        rehosted = f"https://blossom.example/{len(calls)}"
        on_success(rehosted, RehostedImage(
            url=rehosted,
            sha256=f"{len(calls):064x}",
            mime="image/png",
            size=11,
        ))

    return mirror, calls


class TestRehostDescriptions(unittest.TestCase):
    """What the loop reports about the images it rehosted.

    The draft is published with a NIP-92 ``imeta`` tag per rehosted
    image and these are the values it is built from, so an image that
    kept its original URL has to contribute nothing at all: it still
    lives on somebody else's server and nothing here measured it.
    """

    def test_every_rehosted_image_is_described(self):
        mirror, _calls = measuring_mirror()
        outcome, _ = run_rehost(MD, mirror)
        self.assertEqual(sorted(outcome.descriptors), [
            "https://a.example/banner.png",
            "https://a.example/photo%20one.jpg",
        ])
        described = outcome.descriptors["https://a.example/banner.png"]
        self.assertEqual(described.url, "https://blossom.example/1")
        self.assertEqual(described.size, 11)
        # The description names the address the markdown now points at.
        self.assertEqual(outcome.mapping["https://a.example/banner.png"],
                         described.url)

    def test_a_failed_image_is_described_nowhere(self):
        mirror, _calls = measuring_mirror(
            failures=["https://a.example/banner.png"])
        outcome, _ = run_rehost(MD, mirror)
        self.assertEqual(outcome.failed, ["https://a.example/banner.png"])
        self.assertEqual(list(outcome.descriptors),
                         ["https://a.example/photo%20one.jpg"])

    def test_a_skipped_image_is_described_nowhere(self):
        mirror, _calls = measuring_mirror()
        outcome, _ = run_rehost(
            MD, mirror, skip_urls={"https://a.example/banner.png"})
        self.assertEqual(list(outcome.descriptors),
                         ["https://a.example/photo%20one.jpg"])

    def test_an_empty_response_describes_nothing_even_if_it_measured(self):
        def mirror(url, on_success, on_failure):
            on_success("", RehostedImage(url="", sha256="ab" * 32))

        outcome, _ = run_rehost(MD, mirror)
        self.assertEqual(outcome.descriptors, {})
        self.assertEqual(len(outcome.failed), 2)

    def test_a_transport_that_measured_nothing_describes_nothing(self):
        """The old one-argument callback still works, and says nothing.

        Silence is the safe direction: an imeta tag nobody measured is
        the fabrication AD-14 forbids.
        """
        mirror, _calls = fake_mirror()
        outcome, _ = run_rehost(MD, mirror)
        self.assertEqual(outcome.mirrored, 2)
        self.assertEqual(len(outcome.mapping), 2)
        self.assertEqual(outcome.descriptors, {})


HOSTILE_MD = (
    "# Post\n\n"
    "![a](file:///etc/passwd)\n\n"
    "![b](http://169.254.169.254/latest/meta-data/)\n\n"
    "![c](javascript:alert(1))\n\n"
    "![ok](https://a.example/banner.png)\n"
)


class TestUrlPolicy(unittest.TestCase):
    """A feed body is attacker-authored, and its URLs are handed to a
    server to fetch and to the review dialog to preview."""

    def test_non_web_schemes_never_reach_the_scans(self):
        # The scans feed the review dialog's preview as well as the
        # mirror loop, so anything that is not an http(s) image is
        # dropped here. Address-level policy is the loop's job.
        self.assertEqual(scan_markdown_images(HOSTILE_MD), [
            "http://169.254.169.254/latest/meta-data/",
            "https://a.example/banner.png",
        ])
        self.assertNotIn("file:///etc/passwd", scan_markdown_images(HOSTILE_MD))
        self.assertEqual(
            scan_html_images(
                '<img src="file:///etc/passwd">'
                '<img src="javascript:alert(1)">'
                '<img src="https://a.example/x.png">'
            ),
            ["https://a.example/x.png"],
        )

    def test_refused_urls_are_never_handed_to_the_mirror(self):
        mirror, calls = fake_mirror()
        outcome, progress = run_rehost(HOSTILE_MD, mirror)
        # Only the legitimate image is mirrored.
        self.assertEqual(calls, ["https://a.example/banner.png"])
        self.assertEqual(outcome.mirrored, 1)
        # The refused ones are reported, not silently dropped, and their
        # markdown is left exactly as the author wrote it.
        self.assertIn("http://169.254.169.254/latest/meta-data/",
                      outcome.failed)
        self.assertIn("![a](file:///etc/passwd)", outcome.markdown)
        self.assertIn("![b](http://169.254.169.254/latest/meta-data/)",
                      outcome.markdown)
        failed = [p for p in progress if p.status == "failed"]
        self.assertEqual([p.error for p in failed], ["URL was not allowed"])

    def test_private_address_is_refused_even_when_scanned_in(self):
        # The scan lets http through, so the mirror loop is the gate
        # that stops a server being pointed at the local network.
        markdown = "![m](http://10.0.0.1/x.png)\n"
        mirror, calls = fake_mirror()
        outcome, _ = run_rehost(markdown, mirror)
        self.assertEqual(calls, [])
        self.assertEqual(outcome.failed, ["http://10.0.0.1/x.png"])
        self.assertEqual(outcome.markdown, markdown)

    def test_plain_http_images_still_mirror(self):
        markdown = "![m](http://blog.example/x.png)\n"
        mirror, calls = fake_mirror()
        outcome, _ = run_rehost(markdown, mirror)
        self.assertEqual(calls, ["http://blog.example/x.png"])
        self.assertEqual(outcome.mirrored, 1)


def fake_fetch(responses=None, *, mime="image/png"):
    """url -> ("ok", bytes) | ("err", reason); default returns PNG bytes."""
    table = dict(responses or {})
    calls = []

    def fetch(url, *, on_success, on_failure):
        calls.append(url)
        kind, payload = table.get(url, ("ok", PNG))
        if kind == "ok":
            on_success(payload, mime)
        else:
            on_failure(payload)

    return fetch, calls


def tag(event, name):
    return [t[1] for t in event["tags"] if t[0] == name]


class RehostCtx:
    """A wired-up production transport plus what it produced."""

    def __init__(self, *, responder=None, replies=None, fetch=None,
                 signer=None):
        self.nam = FakeNam(replies, responder=responder)
        self.signer = signer or FakeSigner()
        self.pool = FakeSessionPool(self.signer)
        self.fetch, self.fetched = (fetch if fetch else fake_fetch())
        self.rehost, self.client = blossom_rehost(
            session_pool=self.pool,
            profile=FakeProfile(),
            server=SERVER,
            client=BlossomClient(nam=self.nam),
            fetch_bytes=self.fetch,
        )
        self.results = []
        self.described = []
        self.failures = []

    def _ok(self, url, description=None):
        """The success half of the transport callback, both arguments.

        A one-argument collector would still look like a passing test:
        the callback runs inside a Qt slot, so the ``TypeError`` would
        be printed and swallowed rather than raised.
        """
        self.results.append(url)
        self.described.append(description)

    def one(self, url):
        """Rehost a single URL and settle the transport."""
        self.rehost(url, self._ok, self.failures.append)
        self.nam.settle()

    def run(self, markdown, **kwargs):
        """Drive the whole loop, settling between images."""
        out = {}
        progress = []
        rehost_images(
            markdown,
            mirror=self.rehost,
            on_progress=progress.append,
            on_done=lambda o: out.update(outcome=o),
            **kwargs,
        )
        self.nam.settle()
        assert "outcome" in out, "on_done never fired"
        return out["outcome"], progress

    def verbs(self):
        return [verb for verb, _r, _b in self.nam.calls]


class TestBlossomRehost(unittest.TestCase):
    """The production transport: fetch the bytes, then upload them.

    The old transport asked the destination server to pull the source
    URL itself. BUD-11 requires an ``x`` tag on ``PUT /mirror`` whose
    value is the hash of the mirrored blob, which a client that never
    saw the bytes cannot produce, so that request went out missing a tag
    the spec requires. Holding the bytes makes the hash a measurement
    rather than a guess.
    """

    def test_happy_path_rewrites_the_markdown_to_the_uploaded_url(self):
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER))
        markdown = "![a](https://a.example/x.png)\n"
        outcome, progress = ctx.run(markdown)

        self.assertEqual(ctx.fetched, ["https://a.example/x.png"])
        self.assertEqual(outcome.mirrored, 1)
        self.assertEqual(outcome.failed, [])
        sha = hashlib.sha256(PNG).hexdigest()
        self.assertIn(f"{SERVER}/{sha}.png", outcome.markdown)
        self.assertEqual(outcome.mapping,
                         {"https://a.example/x.png": f"{SERVER}/{sha}.png"})
        # The per-image statuses the feeds panel renders are unchanged.
        self.assertEqual([p.status for p in progress],
                         ["queued", "mirroring", "mirrored"])

    def test_the_auth_token_carries_the_hash_of_the_fetched_bytes(self):
        body = b"\x89PNG\r\n\x1a\nsomething else entirely"
        fetch = fake_fetch({"https://a.example/x.png": ("ok", body)})
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER), fetch=fetch)
        ctx.one("https://a.example/x.png")

        expected = hashlib.sha256(body).hexdigest()
        self.assertEqual(tag(ctx.signer.requests[0], "x"), [expected])
        self.assertEqual(tag(ctx.signer.requests[0], "t"), ["upload"])
        # BUD-11 scopes the token by bare domain, never a full URL.
        self.assertEqual(tag(ctx.signer.requests[0], "server"),
                         ["good.example"])
        header = bytes(ctx.nam.calls[0][1].rawHeader("X-SHA-256")).decode()
        self.assertEqual(header, expected)
        self.assertEqual(ctx.failures, [])

    def test_the_uploaded_body_is_exactly_what_was_fetched(self):
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER))
        ctx.one("https://a.example/x.png")
        verb, request, body = ctx.nam.calls[0]
        self.assertEqual(verb, "put")
        self.assertEqual(request.url().path(), "/upload")
        self.assertEqual(body, PNG)

    def test_the_mime_the_fetcher_reported_is_what_gets_sent(self):
        fetch = fake_fetch(mime="image/webp")
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER), fetch=fetch)
        ctx.one("https://a.example/x.png")
        content_type = bytes(
            ctx.nam.calls[0][1].rawHeader("Content-Type")).decode()
        self.assertEqual(content_type, "image/webp")

    def test_a_fetch_failure_keeps_the_original_url_and_the_run_goes_on(self):
        fetch = fake_fetch({
            "https://a.example/one.png": ("err", "Could not download the image"),
        })
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER), fetch=fetch)
        markdown = (
            "![one](https://a.example/one.png)\n\n"
            "![two](https://a.example/two.png)\n"
        )
        outcome, progress = ctx.run(markdown)

        self.assertEqual(outcome.failed, ["https://a.example/one.png"])
        self.assertEqual(outcome.mirrored, 1)
        self.assertIn("![one](https://a.example/one.png)", outcome.markdown)
        self.assertNotIn("![two](https://a.example/two.png)", outcome.markdown)
        # The image that could not be downloaded cost no signer prompt.
        self.assertEqual(ctx.pool.calls, 1)
        failed = [p for p in progress if p.status == "failed"]
        self.assertEqual([p.error for p in failed],
                         ["Could not download the image"])

    def test_nothing_is_signed_before_the_bytes_are_in_hand(self):
        pending = []

        def fetch(url, *, on_success, on_failure):
            pending.append((url, on_success))

        ctx = RehostCtx(responder=FakeBlossomServer(SERVER),
                        fetch=(fetch, pending))
        ctx.rehost("https://a.example/x.png",
                   ctx._ok, ctx.failures.append)
        self.assertEqual(ctx.pool.calls, 0)
        self.assertEqual(ctx.nam.calls, [])

        pending[0][1](PNG, "image/png")
        self.assertEqual(ctx.pool.calls, 1)
        self.assertEqual(ctx.verbs(), ["put"])

    def test_a_refused_url_is_never_fetched_and_never_signed(self):
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER))
        ctx.rehost("http://10.0.0.1/x.png",
                   ctx._ok, ctx.failures.append)
        self.assertEqual(ctx.fetched, [])
        self.assertEqual(ctx.pool.calls, 0)
        self.assertEqual(ctx.nam.calls, [])
        self.assertEqual(ctx.failures, ["URL was not allowed"])

    def test_the_description_is_measured_here_not_taken_from_the_server(self):
        """AD-14: describe media with what this process measured.

        The descriptor a server returns is a claim. Its hash is checked
        against the bytes that were sent, so that one is trustworthy,
        but its ``type`` and ``size`` are not checked against anything
        and publishing them would put somebody else's assertion in an
        event the user signs.
        """
        sha = hashlib.sha256(PNG).hexdigest()
        boastful = json_reply(descriptor(
            sha, size=999999, mime="image/gif", url=f"{SERVER}/{sha}.png"))
        fetch = fake_fetch(mime="image/webp")
        ctx = RehostCtx(replies=[boastful], fetch=fetch)
        ctx.one("https://a.example/x.png")

        self.assertEqual(ctx.failures, [])
        described = ctx.described[0]
        self.assertEqual(described.url, f"{SERVER}/{sha}.png")
        self.assertEqual(described.sha256, sha)
        self.assertEqual(described.mime, "image/webp")   # the download's
        self.assertEqual(described.size, len(PNG))       # the buffer's

    def test_the_described_hash_is_the_one_the_token_authorised(self):
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER))
        ctx.one("https://a.example/x.png")
        described = ctx.described[0]
        self.assertEqual(tag(ctx.signer.requests[0], "x"), [described.sha256])
        header = bytes(ctx.nam.calls[0][1].rawHeader("X-SHA-256")).decode()
        self.assertEqual(header, described.sha256)

    def test_an_image_that_failed_is_described_by_nothing(self):
        fetch = fake_fetch({
            "https://a.example/x.png": ("err", "Could not download the image"),
        })
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER), fetch=fetch)
        ctx.one("https://a.example/x.png")
        self.assertEqual(ctx.described, [])
        self.assertEqual(ctx.failures, ["Could not download the image"])

    def test_a_signer_refusal_is_reported_and_the_run_still_delivers(self):
        markdown = "![a](https://a.example/x.png)\n"
        ctx = RehostCtx(responder=FakeBlossomServer(SERVER),
                        signer=FakeSigner(failure="user declined"))
        outcome, progress = ctx.run(markdown)

        self.assertEqual(ctx.verbs(), [])          # refused before the PUT
        self.assertEqual(outcome.failed, ["https://a.example/x.png"])
        self.assertEqual(outcome.markdown, markdown)
        failed = [p for p in progress if p.status == "failed"]
        self.assertEqual(
            [p.error for p in failed],
            ["signer rejected the Blossom auth event: user declined"],
        )


class TestBlobFetcher(unittest.TestCase):
    """The download half. The bytes belong to a stranger until checked."""

    def fetcher(self, replies=None, *, max_bytes=25 * 1024 * 1024):
        nam = FakeNam(replies)
        return BlobFetcher(max_bytes=max_bytes, nam=nam), nam

    def fetch(self, fetcher, url):
        out = {}
        fetcher.fetch(
            url,
            on_success=lambda data, mime: out.update(data=data, mime=mime),
            on_failure=lambda reason: out.update(error=reason),
        )
        return out

    def test_a_private_address_is_refused_without_a_request(self):
        fetcher, nam = self.fetcher()
        out = self.fetch(fetcher, "http://169.254.169.254/latest/meta-data/")
        self.assertEqual(nam.calls, [])
        self.assertEqual(out["error"], "URL was not allowed")

    def test_a_declared_size_over_the_cap_aborts_the_transfer(self):
        reply = FakeReply(body=PNG)
        fetcher, nam = self.fetcher([reply], max_bytes=1024)
        out = self.fetch(fetcher, "https://a.example/huge.png")
        reply.progress(0, 5000)
        self.assertTrue(reply.aborted)
        reply.finish()
        self.assertEqual(out["error"], "Image is too large to rehost")

    def test_a_body_over_the_cap_is_refused_even_without_progress(self):
        reply = FakeReply(body=b"x" * 2048)
        fetcher, nam = self.fetcher([reply], max_bytes=1024)
        out = self.fetch(fetcher, "https://a.example/huge.png")
        reply.finish()
        self.assertEqual(out["error"], "Image is too large to rehost")

    def test_bytes_from_a_refused_redirect_target_are_not_read(self):
        # Redirects are followed, so the bytes can arrive from an origin
        # the caller never named.
        reply = FakeReply(body=PNG, url="http://127.0.0.1:9000/x.png")
        fetcher, _nam = self.fetcher([reply])
        out = self.fetch(fetcher, "https://a.example/x.png")
        reply.finish()
        self.assertEqual(out["error"], "URL was not allowed")
        self.assertNotIn("data", out)

    def test_the_content_type_header_names_the_mime(self):
        reply = FakeReply(body=PNG, content_type="image/webp; charset=binary")
        fetcher, _nam = self.fetcher([reply])
        out = self.fetch(fetcher, "https://a.example/x.png")
        reply.finish()
        self.assertEqual(out["data"], PNG)
        self.assertEqual(out["mime"], "image/webp")

    def test_a_generic_content_type_falls_back_to_the_magic_bytes(self):
        reply = FakeReply(body=PNG, content_type="application/octet-stream")
        fetcher, _nam = self.fetcher([reply])
        out = self.fetch(fetcher, "https://a.example/x.png")
        reply.finish()
        self.assertEqual(out["mime"], "image/png")

    def test_an_unrecognisable_body_gets_the_generic_type(self):
        reply = FakeReply(body=b"not an image at all")
        fetcher, _nam = self.fetcher([reply])
        out = self.fetch(fetcher, "https://a.example/x.png")
        reply.finish()
        self.assertEqual(out["mime"], "application/octet-stream")

    def test_a_transport_failure_never_leaks_a_qt_string(self):
        reply = FakeReply(
            error=QNetworkReply.HostNotFoundError,
            error_string="Host a.example not found",
        )
        fetcher, _nam = self.fetcher([reply])
        out = self.fetch(fetcher, "https://a.example/x.png")
        reply.finish()
        self.assertEqual(out["error"], "Could not download the image")

    def test_an_empty_body_is_reported_rather_than_uploaded(self):
        reply = FakeReply(body=b"")
        fetcher, _nam = self.fetcher([reply])
        out = self.fetch(fetcher, "https://a.example/x.png")
        reply.finish()
        self.assertEqual(out["error"], "Image was empty")


if __name__ == "__main__":
    unittest.main()
