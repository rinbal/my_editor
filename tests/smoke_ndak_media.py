# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check against ndak and local Blossom servers: the media library.

Not run by pytest (no ``test_`` prefix). Needs Docker for the Blossom
servers, which this script starts and removes itself:

    (cd ~/projects/ndak && docker compose up -d)
    docker pull ghcr.io/hzrd149/blossom-server:master
    .venv/bin/python tests/smoke_ndak_media.py

Servers: A http://localhost:7801 and M http://localhost:7803 are
hzrd149/blossom-server (the default primary, and the stand-in for the
members' server); B http://localhost:7802 is a small stand-in written for
this test (tests/smoke_ndak_common.py) because the real server refuses to
mirror from loopback addresses. B logs every kind 24242 auth event it is
sent, which this script reviews against BUD-11.

Covers upload (BUD-02) with mirroring (BUD-04) to every server, listing,
images in a note with NIP-92 imeta, delete across servers, the kind 10063
server list, private files (kind 34578, written the way Lotus writes them,
since MyEditor only reads them), and the public copy maker.

The Media Library window is driven too, against the same servers and a
fourth address where nothing answers (D, http://localhost:7809): adding
a file with progress on its tile, Cancel, a server that is down (the
file lands on the others, the window says one plain sentence), Stop
Sharing a private image's public copy, and Delete with one server
unreachable (the file stays, and the window says so).
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_ndak_common as nd  # noqa: E402

nd.bootstrap("media")
app = nd.qt_app()

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

import image_safety  # noqa: E402
from nostr import crypto, events  # noqa: E402
from nostr.blossom.names import MediaNames  # noqa: E402
from nostr.blossom.server_list import UserServerList, build_server_list_event  # noqa: E402
from nostr.blossom.settings import BlossomSettings  # noqa: E402
from nostr.blossom.store import MediaStore  # noqa: E402
from nostr.imports.sources.nostr import RelayQueryAdapter  # noqa: E402
from nostr.media.filecrypto import decrypt_file, encrypt_file  # noqa: E402
from nostr.media.manager import AssetManager  # noqa: E402
from nostr.media.media_visibility import MediaVisibility  # noqa: E402
from nostr.media.private_library import PrivateLibrary  # noqa: E402
from nostr.media.publish_copy import PublicCopyMaker  # noqa: E402
from nostr.media.visibility import PublicLedger  # noqa: E402
from nostr.publisher import PublishedMedia, PublishJob, build_note  # noqa: E402
from nostr.ui.account_windows import local_profile  # noqa: E402
from nostr.ui.media_grid import SENDING  # noqa: E402
from nostr.ui.media_library_dialog import MediaLibraryDialog  # noqa: E402
from nostr.ui.thumbnail_loader import ThumbnailLoader  # noqa: E402

RNOSTR, NRS, STRFRY = nd.RNOSTR, nd.NRS, nd.STRFRY
A, B, M = nd.BLOSSOM_A, nd.BLOSSOM_B, nd.BLOSSOM_M
rep = nd.Report("media")
D = "http://localhost:7809"   # a media server that is down: nothing listens here


def click(label: str):
    """An alert policy that clicks the button named ``label``."""
    def policy(alert: dict):
        for button in alert["buttons"]:
            if button.label == label:
                return button.value
        return nd.default_alert_policy(alert)
    return policy


def select(window, key: str) -> None:
    model = window._model
    window._grid.setCurrentIndex(model.index(model.row_of(key)))


def png(color: str, w: int = 64, h: int = 48, text_chunk: bytes = b"") -> bytes:
    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    raw = bytes(data)
    if text_chunk:
        chunk = b"tEXt" + text_chunk
        crc = struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        iend = raw.rindex(b"IEND") - 4
        raw = raw[:iend] + struct.pack(">I", len(text_chunk)) + chunk + crc + raw[iend:]
    return raw


servers = []
try:
    rep.section("Starting the Blossom servers")
    servers = [nd.DockerBlossom(7801, "a").start(), nd.StandInBlossom(7802).start(),
               nd.DockerBlossom(7803, "m").start()]
    standin = servers[1]
    rep.info("servers", "A hzrd149 :7801, B stand-in :7802, M hzrd149 :7803")

    stack = nd.Stack()
    alice, sk, setup = nd.local_account(stack, "Alice Media")
    pk = alice.user_pubkey
    rep.check("Alice's account is set up", setup.get("ok") is True)
    settings = BlossomSettings()
    rep.check("with nothing configured, the default servers are used (patched to A and B)",
              settings.configured_servers() == [A, B] if callable(getattr(settings, "configured_servers", None))
              else list(settings.configured_servers) == [A, B], str(settings.configured_servers))
    loader = ThumbnailLoader()
    store = MediaStore(session_pool=stack.sessions, profile_provider=lambda: stack.store.default(),
                       settings=settings, blob_cache=loader, entitled_servers=lambda: [M])
    mirror_failed = nd.Catch(store.mirror_failed)
    upload_status = nd.Catch(store.upload_status)

    # ----------------------------------------------------------------------- #
    rep.section("Upload with mirroring (BUD-02, BUD-04)")
    picture = png("#cc3366", text_chunk=b"Comment\x00ndak " + os.urandom(8).hex().encode())
    sha = hashlib.sha256(picture).hexdigest()
    done = nd.Catch(store.upload_finished, store.upload_failed)
    store.upload_bytes(picture, name="ndak.png", mime_type="image/png")
    done.wait(90_000)
    media = done.last[1] if done.fired and not isinstance(done.last[1], str) else None
    rep.check("the upload finishes", media is not None, str(done.last))
    if media is not None:
        origins = sorted({u["server"] for u in media.urls})
        rep.check("the file is on the primary (A) and was mirrored to B",
                  A in origins and B in origins, str(origins))
        rep.check("A serves the blob by its hash", nd.http_status(f"{A}/{sha}") == 200)
        rep.check("B serves the blob by its hash (mirrored)", nd.http_status(f"{B}/{sha}") == 200)
        m_has = nd.http_status(f"{M}/{sha}") == 200
        if m_has:
            rep.check("the members' server (M) ends up with the file too", True)
        else:
            rep.check("a mirror the server refuses is reported (mirror_failed), the upload "
                      "still succeeds", bool(mirror_failed.calls), str(mirror_failed.calls))
            rep.skip("the members' server (M, hzrd149) ends up with the file too",
                     "hzrd149/blossom-server refuses BUD-04 mirrors from loopback sources "
                     "(SSRF guard, no switch), so local mirroring to it cannot succeed. "
                     "Observed design gap: store.py _replicate has no fallback to a direct "
                     "upload when a server refuses or lacks /mirror")
        rep.info("upload status lines", " | ".join(c[1] for c in upload_status.calls)[:400])
        mirror_log = [e for e in standin.log() if e.get("path") == "/mirror"]
        if rep.check("B received a BUD-04 mirror request naming A's URL",
                     bool(mirror_log) and mirror_log[-1]["source"].startswith(A),
                     json.dumps(mirror_log[-1] if mirror_log else {})[:300]):
            auth = mirror_log[-1]["auth"]
            nd.review_event(rep, "BUD-11 auth for mirror", auth, kind=24242, max_age_s=600)
            rep.check("mirror auth: t=upload, x=sha256, expiration, server=localhost, accepted",
                      nd.tag_values(auth, "t") == ["upload"] and nd.tag_values(auth, "x") == [sha]
                      and nd.tag_values(auth, "server") == ["localhost"]
                      and int(nd.tag_values(auth, "expiration")[0]) > time.time()
                      and mirror_log[-1]["verdict"] == "ok", json.dumps(auth["tags"]))
            rep.info("auth event content and timing",
                     f"content {auth['content']!r}, created_at now-{int(time.time()) - auth['created_at']}s, "
                     f"expires in {int(nd.tag_values(auth, 'expiration')[0]) - int(time.time())}s")
            rep.event("BUD-11 auth (mirror)", auth)

    # ----------------------------------------------------------------------- #
    rep.section("Listing (BUD-02 /list)")
    fetched = nd.Catch(store.fetch_finished, store.fetch_error)
    store.fetch(force=True)
    fetched.wait(30_000)
    entry = store.files.get(sha)
    rep.check("the library lists the file once, with every server that has it",
              entry is not None and {u["server"] for u in entry.urls} >= {A, B},
              str([u["server"] for u in entry.urls]) if entry else str(store.files.keys()))
    listings = store.server_listings() if hasattr(store, "server_listings") else {}
    rep.info("server listings", str({k: getattr(v, "ok", v) for k, v in listings.items()})[:300])
    list_log = [e for e in standin.log() if e.get("path", "").startswith("/list/")]
    if list_log and list_log[-1].get("auth"):
        auth = list_log[-1]["auth"]
        nd.review_event(rep, "BUD-11 auth for list", auth, kind=24242, max_age_s=600)
        rep.check("list auth: t=list, no x tag", nd.tag_values(auth, "t") == ["list"]
                  and not nd.tag_values(auth, "x"), json.dumps(auth["tags"]))

    # ----------------------------------------------------------------------- #
    rep.section("An image in a note: upload through the asset manager, NIP-92 imeta")
    assets = AssetManager(blob_store=loader, uploader=store,
                          profile_provider=lambda: stack.store.default(),
                          decoder=image_safety.decode_image_bytes)
    photo = png("#33aa66", 80, 60)
    asset = assets.adopt_bytes(photo, alt="A green\nrectangle, alt text")
    assets.request_upload(asset.sha256)
    nd.wait_until(lambda: assets.get(asset.sha256).is_uploaded
                  or assets.get(asset.sha256).upload_state.name == "FAILED", 90_000)
    asset = assets.get(asset.sha256)
    rep.check("the document image is uploaded", asset.is_uploaded,
              f"{asset.upload_state} {asset.failure_code}")
    if asset.is_uploaded:
        record = PublishedMedia(url=asset.remote_url, sha256=asset.sha256, mime=asset.mime,
                                width=asset.width, height=asset.height,
                                alt="A green\nrectangle, alt text", size=asset.size,
                                servers=tuple(asset.servers))
        content = f"A picture from the ndak test {asset.remote_url} "
        unsigned = build_note(content, pk, media=[record])
        box = {}
        job = PublishJob(relay_pool=stack.pool, relay_directory=stack.directory,
                         session_pool=stack.sessions, profile=alice, unsigned_event=unsigned)
        job.signed.connect(lambda i: box.update(id=i))
        job.completed.connect(lambda r: box.update(r=r))
        job.start()
        nd.wait_until(lambda: "r" in box, 20_000)
        ev = nd.nak_req([RNOSTR], ids=[box.get("id", "")])
        if rep.check("the note with the image is published", bool(ev)):
            ev = ev[0]
            nd.review_event(rep, "note with imeta", ev, kind=1)
            imeta = nd.first_tag(ev, "imeta") or []
            fields = dict(part.split(" ", 1) for part in imeta[1:])
            rep.check("NIP-92 imeta: url in the content, m, x, dim, alt (one line), size",
                      fields.get("url") == asset.remote_url and fields.get("url") in ev["content"]
                      and fields.get("m") == "image/png" and fields.get("x") == asset.sha256
                      and fields.get("dim") == "80x60" and fields.get("alt") == "A green rectangle, alt text"
                      and fields.get("size") == str(len(photo)), json.dumps(imeta))
            fallbacks = [p.split(" ", 1)[1] for p in imeta[1:] if p.startswith("fallback ")]
            rep.check("fallback URLs are the other servers that confirmed the blob, and they serve it",
                      fallbacks and all(nd.http_status(u) == 200 for u in fallbacks)
                      and all(not u.startswith(fields.get("url", "")[:21]) for u in fallbacks),
                      str(fallbacks))
            rep.event("note with imeta", ev, present=nd.presence(ev["id"]))

    # ----------------------------------------------------------------------- #
    rep.section("Delete across every server")
    store.fetch(force=True)
    nd.Catch(store.fetch_finished).wait(20_000)
    deleted = nd.Catch(store.file_deleted, store.delete_failed)
    store.delete_file(sha)
    deleted.wait(30_000)
    rep.check("delete reports success", deleted.fired and deleted.last == (sha,), str(deleted.last))
    rep.check("A and B no longer serve the blob",
              nd.http_status(f"{A}/{sha}") == 404 and nd.http_status(f"{B}/{sha}") == 404,
              f"A {nd.http_status(f'{A}/{sha}')}, B {nd.http_status(f'{B}/{sha}')}")
    del_log = [e for e in standin.log() if e.get("method") == "DELETE"]
    if del_log:
        auth = del_log[-1]["auth"]
        nd.review_event(rep, "BUD-11 auth for delete", auth, kind=24242, max_age_s=600)
        rep.check("delete auth: t=delete, x=sha256, accepted",
                  nd.tag_values(auth, "t") == ["delete"] and nd.tag_values(auth, "x") == [sha]
                  and del_log[-1]["verdict"] == "ok", json.dumps(auth["tags"]))

    # ----------------------------------------------------------------------- #
    rep.section("The Media Library window: progress, Cancel, a server down, Delete")
    window_settings = BlossomSettings(nd.HOME / "window-blossom.json")
    window_settings.set_custom_servers([D, A, B])          # the first one is down
    store3 = MediaStore(session_pool=stack.sessions, profile_provider=lambda: stack.store.default(),
                        settings=window_settings, blob_cache=loader,
                        names=MediaNames(nd.HOME / "media_names.json"))
    window = MediaLibraryDialog(store=store3, is_dark=False)
    window.show()
    talk = nd.HOME / "Talk.mp4"
    talk.write_bytes(os.urandom(3 * 1024 * 1024))
    tsha = hashlib.sha256(talk.read_bytes()).hexdigest()
    seen_states = []
    store3.upload_progress.connect(lambda n, a, b: seen_states.append(
        (window._model.tile(n).state, window._model.tile(n).meter) if window._model.tile(n) else None))
    progress = nd.Catch(store3.upload_progress)
    landed = nd.Catch(store3.upload_finished, store3.upload_failed)
    missed = nd.Catch(store3.mirror_failed)
    window._add_paths([str(talk)])
    rep.check("an added file is a tile at once, under its own name",
              window._model.tile("Talk.mp4") is not None)
    landed.wait(120_000)
    rep.check("its upload reports progress on the tile, up to the whole file",
              any(sent == total > 0 for _n, sent, total in progress.calls)
              and any(state and state[0] == SENDING for state in seen_states),
              f"{len(progress.calls)} progress reports, last {progress.calls[-1] if progress.calls else None}")
    rep.check("with the first server down, the file still lands on the others",
              landed.fired and landed.last[0] == "Talk.mp4" and not isinstance(landed.last[1], str)
              and nd.http_status(f"{A}/{tsha}") == 200 and nd.http_status(f"{B}/{tsha}") == 200,
              str(landed.last)[:200])
    rep.check("the server that was down is reported as one without a copy",
              [c[1] for c in missed.calls] == ["localhost:7809"], str(missed.calls))
    rep.check("the window says one plain sentence about it, naming no server",
              window._status.text() == "“Talk.mp4” is added, but one of your media servers "
                                       "didn’t keep a copy.", window._status.text())
    tile = window._model.tile(tsha)
    rep.check("the file's tile carries the name it was added with, and no mark (it is public)",
              tile is not None and tile.name == "Talk.mp4" and not tile.private,
              f"{tile.name if tile else None}")

    lecture = nd.HOME / "Lecture.mp4"
    lecture.write_bytes(os.urandom(2 * 1024 * 1024))
    lsha = hashlib.sha256(lecture.read_bytes()).hexdigest()
    started = nd.Catch(store3.upload_started)
    cancelled = nd.Catch(store3.upload_cancelled)
    outcome = nd.Catch(store3.upload_finished, store3.upload_failed)
    store3.upload_started.connect(lambda n: n == "Lecture.mp4" and window._cancel_upload(n))
    window._add_paths([str(lecture)])
    nd.wait_until(lambda: cancelled.fired, 10_000)
    nd.wait_until(lambda: outcome.fired, 4_000)
    rep.check("Cancel stops an upload: nothing else is reported, and no server has the file",
              cancelled.calls == [("Lecture.mp4",)] and not outcome.fired
              and nd.http_status(f"{A}/{lsha}") == 404 and nd.http_status(f"{B}/{lsha}") == 404,
              f"cancelled {cancelled.calls}, outcome {outcome.calls}")
    rep.check("the cancelled file's tile is gone", window._model.tile("Lecture.mp4") is None)

    # Delete with one server unreachable: the record names D too, as a
    # listing from before D went down would.
    store3.files[tsha].urls.append({"server": D, "url": f"{D}/{tsha}"})
    select(window, tsha)
    nd.ALERT_POLICY = click("Delete")
    gone = nd.Catch(store3.file_deleted, store3.delete_failed)
    window._delete()
    gone.wait(30_000)
    nd.ALERT_POLICY = nd.default_alert_policy
    rep.check("Delete asked first", nd.ALERTS_SEEN and nd.ALERTS_SEEN[-1]["title"] == "Delete “Talk.mp4”?",
              str(nd.ALERTS_SEEN[-1:]))
    rep.check("the servers that answered deleted it",
              nd.http_status(f"{A}/{tsha}") == 404 and nd.http_status(f"{B}/{tsha}") == 404)
    rep.check("with one server unreachable the file is not called gone: it stays, on that server",
              gone.last == (tsha, "NETWORK_UNAVAILABLE") and tsha in store3.files
              and [u["server"] for u in store3.files[tsha].urls] == [D], str(gone.last))
    rep.check("and the window says so in one plain sentence",
              window._status.text().startswith("“Talk.mp4” is still on one of your media servers."),
              window._status.text())
    window.close()

    # ----------------------------------------------------------------------- #
    rep.section("The account's media server list (kind 10063, BUD-03)")
    server_list = build_server_list_event([B, M], pk)
    signed_list = stack.sign(alice, server_list)
    stack.publish_raw([RNOSTR, NRS, STRFRY], signed_list)
    nd.review_event(rep, "kind 10063 built by build_server_list_event", signed_list, kind=10063)
    rep.check("10063 has one server tag per server, in order",
              [t for t in signed_list["tags"] if t[0] == "server"] == [["server", B], ["server", M]],
              json.dumps(signed_list["tags"]))
    fresh = BlossomSettings(nd.HOME / "fresh-blossom.json")
    usl = UserServerList(stack.pool, relay_directory=stack.directory, settings=fresh)
    adopted = nd.Catch(usl.adopted)
    discovered = nd.Catch(usl.discovered)
    usl.refresh(alice, force=True)
    nd.wait_until(lambda: discovered.fired, 15_000)
    rep.check("the published list is read", discovered.fired and list(discovered.last[0]) == [B, M],
              str(discovered.last))
    rep.check("with nothing configured here, it is adopted as the media servers",
              adopted.fired and list(fresh.configured_servers if not callable(fresh.configured_servers)
                                     else fresh.configured_servers()) == [B, M], str(adopted.last))
    configured = BlossomSettings(nd.HOME / "configured-blossom.json")
    configured.set_custom_servers([A])
    usl2 = UserServerList(stack.pool, relay_directory=stack.directory, settings=configured)
    suggestions = nd.Catch(usl2.suggestions_changed)
    discovered2 = nd.Catch(usl2.discovered)
    usl2.refresh(alice, force=True)
    nd.wait_until(lambda: discovered2.fired, 15_000)
    cfg = configured.configured_servers() if callable(configured.configured_servers) \
        else configured.configured_servers
    rep.check("a list configured here is never replaced; the published one is only suggested",
              list(cfg) == [A], f"configured {cfg}, suggestions {suggestions.calls}")

    # ----------------------------------------------------------------------- #
    rep.section("Private files (kind 34578, written as Lotus writes them) and public copies")
    secret_picture = png("#112233", 40, 30, text_chunk=b"Location\x00GPS 47.3769 8.5417 secret")
    enc = encrypt_file(secret_picture)
    esha = hashlib.sha256(enc.envelope).hexdigest()
    done = nd.Catch(store.upload_finished, store.upload_failed)
    store.upload_bytes(enc.envelope, name="private.bin", mime_type="application/octet-stream")
    done.wait(60_000)
    rep.check("the encrypted envelope uploads", done.fired and not isinstance(done.last[1], str),
              str(done.last))
    record = {"hash": esha, "encryptionKey": enc.key_hex, "server": A, "servers": [A, B],
              "size": len(secret_picture), "type": "image/png", "name": "secret.png",
              "uploadedAt": int(time.time())}
    content = crypto.encrypt_to(json.dumps(record), sk, bytes.fromhex(pk))
    private_event = events.build_event(kind=34578, content=content, tags=[["d", esha]], sk=sk)
    stack.publish_raw([RNOSTR, NRS], private_event)

    dev2 = nd.Stack("second")
    dev2.vault.store(sk)
    alice2 = local_profile(pk)
    dev2.store.upsert(alice2)
    library = PrivateLibrary(session_pool=dev2.sessions, relay_directory=dev2.directory,
                             query=RelayQueryAdapter(dev2.pool), entitled_relays=lambda: [])
    changed = nd.Catch(library.library_changed)
    library.bind_profile(alice2)
    nd.wait_until(lambda: changed.fired and not library.loading, 30_000)
    blob = library.get(esha)
    rep.check("a second device reads the private file record and its key",
              blob is not None and blob.key_hex == enc.key_hex and blob.mime == "image/png",
              str(blob))
    if blob is not None:
        rep.check("the file decrypts to the original bytes",
                  decrypt_file(enc.envelope, blob.key_hex) == secret_picture)
        ledger = PublicLedger(nd.HOME / "media_public.json")
        visibility = MediaVisibility(library=library, ledger=ledger)
        rep.check("its visibility is private", visibility.state_of(esha) == "private",
                  visibility.state_of(esha))
        loader2 = ThumbnailLoader(cache_dir=nd.HOME / "second-cache")
        store2 = MediaStore(session_pool=dev2.sessions, profile_provider=lambda: alice2,
                            settings=BlossomSettings(nd.HOME / "second-blossom.json"),
                            blob_cache=loader2)
        maker = PublicCopyMaker(ledger=ledger, fetcher=loader2, uploader=store2)
        answer = {}
        maker.ensure_public_copies([blob], consent=lambda blobs: True,
                                   on_done=lambda result: answer.update(r=result))
        nd.wait_until(lambda: answer, 120_000)
        result = answer.get("r")
        rep.check("the public copy maker mints a public copy",
                  result is not None and result.ok and len(result.blobs) == 1,
                  str(result.failures if result else "no answer"))
        if result is not None and result.ok:
            public = result.blobs[0]
            body = None
            import urllib.request
            try:
                with urllib.request.urlopen(public.url, timeout=5) as r:
                    body = r.read()
            except Exception as exc:  # noqa: BLE001
                body = None
                rep.note(f"fetching the public copy failed: {exc}")
            rep.check("the copy is a new blob, scrubbed, traced to the original",
                      public.sha256 != esha and public.scrubbed and public.source_hash == esha,
                      f"{public.sha256[:12]} scrubbed={public.scrubbed}")
            rep.check("the public copy is a plaintext PNG", body is not None
                      and body.startswith(b"\x89PNG"), f"{len(body or b'')} bytes")
            rep.check("the public copy no longer carries the hidden location text "
                      "(a PNG tEXt chunk)", body is not None and b"GPS" not in body,
                      "the copy still contains 'Location: GPS 47.3769 8.5417': scrub.py "
                      "re-encodes through QImage, which keeps PNG tEXt/iTXt and JPEG COM "
                      "text and writes it back" if body and b"GPS" in body else "")
            rep.check("the ledger records it and the original reads as published-copy",
                      ledger.copy_of(esha) is not None and visibility.state_of(esha) == "published-copy",
                      visibility.state_of(esha))
            again = {}
            maker.ensure_public_copies([blob], consent=lambda blobs: True,
                                       on_done=lambda r: again.update(r=r))
            nd.wait_until(lambda: again, 30_000)
            rep.check("asking again reuses the copy instead of uploading another",
                      again.get("r") is not None and again["r"].ok and not again["r"].minted,
                      str(again.get("r")))
            rep.section("Stop Sharing, from the Media Library window")
            listed = nd.Catch(store2.fetch_finished)
            store2.fetch(force=True)
            listed.wait(30_000)
            window2 = MediaLibraryDialog(store=store2, is_dark=False, visibility=visibility)
            window2.show()
            original = window2._model.tile(esha)
            rep.check("the private original and its public copy are one tile, with no lock",
                      original is not None and not original.private
                      and original.name == "secret.png"
                      and window2._model.tile(public.sha256) is None,
                      str([t.name for t in window2._model.tiles]))
            select(window2, esha)
            nd.ALERT_POLICY = click("Stop Sharing")
            unshared = nd.Catch(store2.file_deleted, store2.delete_failed)
            window2._stop_sharing()
            unshared.wait(30_000)
            nd.ALERT_POLICY = nd.default_alert_policy
            rep.check("Stop Sharing asked first",
                      nd.ALERTS_SEEN and nd.ALERTS_SEEN[-1]["title"] == "Stop sharing “secret.png”?",
                      str(nd.ALERTS_SEEN[-1:]))
            rep.check("the public copy is deleted from every server it was on",
                      unshared.last == (public.sha256,)
                      and all(nd.http_status(f"{origin}/{public.sha256}") == 404
                              for origin in (A, B)), str(unshared.last))
            rep.check("the private original stays, locked again, and the record forgets the copy",
                      nd.http_status(f"{A}/{esha}") == 200 and ledger.copy_of(esha) is None
                      and visibility.state_of(esha) == "private"
                      and window2._model.tile(esha).private
                      and window2._status.text() == "“secret.png” is no longer shared.",
                      window2._status.text())
            window2.close()
        rep.note("MyEditor never writes kind 34578 itself (no write path in nostr/media); the "
                 "copy maker runs without `records` in main_window.py, so the publicCopy "
                 "pointer on the private record is never written.")
    dev2.close()
    stack.close()
finally:
    for server in servers:
        server.stop()

sys.exit(rep.finish())
