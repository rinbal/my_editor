# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check against ndak: private drafts (NIP-37, kind 31234, NIP-44).

Not run by pytest (no ``test_`` prefix). Run with the ndak relays up:

    (cd ~/projects/ndak && docker compose up -d)
    .venv/bin/python tests/smoke_ndak_drafts.py

Two devices of one account, each with its own files, relay pool, signer
and DraftSync: one saves a note draft and an article draft, the other
finds and decrypts them, live edits arrive, drafts are deleted one at a
time and in bulk. Every wrap is read back with nak, decrypted with nak and
reviewed against NIP-37 and NIP-40. The window-level parts (opening a
draft into a tab, Ctrl+S, the conflict banner) are in smoke_ndak_app.py.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_ndak_common as nd  # noqa: E402

nd.bootstrap("drafts")
app = nd.qt_app()

from nostr.draft_store import DraftState, DraftStore  # noqa: E402
from nostr.draft_sync import DraftSync  # noqa: E402
from nostr.drafts import (DEFAULT_EXPIRATION_SECONDS, build_draft_wrap,  # noqa: E402
                          build_inner_event, build_tombstone_wrap, new_note_identifier,
                          serialize_inner_event)
from nostr.publisher import DraftBulkDeleteJob, DraftDeleteJob, DraftPublishJob  # noqa: E402

# Since the owner's decision D-3 only imported drafts expire: a draft written
# in the editor, and a deletion, carry no expiration. Older builds gave every
# draft and deletion 90 days. The harness checks what the build under test
# promises, read from build_draft_wrap's default.
EDITOR_DRAFTS_EXPIRE = inspect.signature(build_draft_wrap).parameters[
    "expiration_seconds"].default is not None
from nostr.ui.account_windows import local_profile  # noqa: E402

RNOSTR, NRS, STRFRY, OBELISK, KHATRU = nd.RNOSTR, nd.NRS, nd.STRFRY, nd.OBELISK, nd.KHATRU
rep = nd.Report("drafts")
a = nd.Stack("a")
alice, sk, setup = nd.local_account(a, "Alice Drafts")
pk = alice.user_pubkey
sk_hex = sk.hex()
rep.check("Alice's account is set up", setup.get("ok") is True)

b = nd.Stack("b")
b.vault.store(sk)
alice_b = local_profile(pk)
b.store.upsert(alice_b)


class Device:
    def __init__(self, stack, profile, name, entitled=()):
        self.stack, self.profile, self.name = stack, profile, name
        self.store = DraftStore()
        self.sync = DraftSync(relay_pool=stack.pool, relay_directory=stack.directory,
                              session_pool=stack.sessions, store=self.store,
                              entitled_relays=lambda: list(entitled))
        self.removed = nd.Catch(self.store.record_removed)
        self.changed = nd.Catch(self.store.record_changed)
        self.status = nd.Catch(self.sync.status_changed)

    def ready(self, identifier):
        record = self.store.get(identifier)
        return record is not None and record.state is DraftState.READY


def save(device, inner, identifier, **kwargs):
    res = {}
    job = DraftPublishJob(relay_pool=device.stack.pool, relay_directory=device.stack.directory,
                          session_pool=device.stack.sessions, profile=device.profile,
                          inner_event=inner, identifier=identifier, **kwargs)
    job.stashed.connect(lambda d, eid, ts: res.update(eid=eid, ts=ts))
    job.completed.connect(lambda r: res.update(results=r))
    job.failed.connect(lambda r: res.update(failed=r))
    job.start()
    nd.wait_until(lambda: "results" in res or "failed" in res, 30_000)
    if "eid" in res:      # what MainWindow._on_draft_stashed does
        device.store.upsert_from_inner(identifier=identifier, inner=inner, event_id=res["eid"],
                                       created_at=res["ts"], expiration=None)
    return res


def wrap_on_relays(identifier, relays=(RNOSTR, NRS, STRFRY)):
    found = nd.nak_req(list(relays), kinds=[31234], authors=[pk], tags={"d": [identifier]})
    return max(found, key=lambda e: (e["created_at"], -int(e["id"], 16))) if found else None


def nak_open(wrap):
    out = nd.nak("decrypt", "--sec", sk_hex, "-p", pk, wrap["content"])
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None


def review_wrap(label, wrap, inner_kind, inner):
    nd.review_event(rep, label, wrap, kind=31234)
    t = [x[0] for x in wrap["tags"]]
    if EDITOR_DRAFTS_EXPIRE:
        rep.check(f"{label}: NIP-37 tags d and k, NIP-40 expiration 90 days, client",
                  t == ["d", "k", "expiration", "client"]
                  and nd.tag_values(wrap, "k") == [str(inner_kind)]
                  and int(nd.tag_values(wrap, "expiration")[0]) - wrap["created_at"]
                  == DEFAULT_EXPIRATION_SECONDS == 90 * 86400, json.dumps(wrap["tags"]))
    else:
        rep.check(f"{label}: NIP-37 tags d and k, client, and no expiration (written here: D-3)",
                  t == ["d", "k", "client"] and nd.tag_values(wrap, "k") == [str(inner_kind)],
                  json.dumps(wrap["tags"]))
    opened = nak_open(wrap)
    rep.check(f"{label}: nak decrypts the content (NIP-44 to self) to the unsigned draft event",
              opened is not None and opened.get("kind") == inner_kind
              and opened.get("content") == inner["content"] and opened.get("tags") == inner["tags"]
              and opened.get("pubkey") == pk and "sig" not in opened and "id" not in opened,
              json.dumps(opened)[:200] if opened else "could not decrypt")


A = Device(a, alice, "A")
A.sync.start_for(alice)
nd.wait_until(lambda: not A.store.is_loading, 15_000)

# --------------------------------------------------------------------------- #
rep.section("Saving a note draft and an article draft on device A")

note_id = new_note_identifier()
note_inner = build_inner_event(kind=1, content="A private note draft ✍️\nsecond line", pubkey_hex=pk)
res = save(A, note_inner, note_id)
rep.check("the note draft is saved", "eid" in res and any(ok for _, ok, _ in res.get("results", [])),
          str(res.get("failed", "")))
note_wrap = wrap_on_relays(note_id)
if rep.check("the wrap is on the relays", note_wrap is not None and note_wrap["id"] == res.get("eid")):
    review_wrap("note draft", note_wrap, 1, note_inner)
    where = {r for r, ok in nd.presence(note_wrap["id"]).items() if ok}
    rep.check("drafts go to the account's own relays (write and read)", where == {RNOSTR, NRS},
              ", ".join(sorted(nd.RELAY_NAMES[r] for r in where)))
    rep.event("draft wrap, note", note_wrap, claimed=[u for u, ok, _ in res["results"] if ok],
              expected=[RNOSTR, NRS], present=nd.presence(note_wrap["id"]))

slug = f"draft-article-{int(time.time())}"
art_inner = build_inner_event(kind=30023, content="# Heading\n\nBody of a **draft** article.",
                              pubkey_hex=pk, tags=[["d", slug], ["title", "A Draft Article"],
                                                   ["summary", "drafted live"]])
res = save(A, art_inner, slug)
art_wrap = wrap_on_relays(slug)
if rep.check("the article draft is saved", art_wrap is not None and art_wrap["id"] == res.get("eid")):
    review_wrap("article draft", art_wrap, 30023, art_inner)
    rep.event("draft wrap, article", art_wrap, claimed=[u for u, ok, _ in res["results"] if ok],
              expected=[RNOSTR, NRS], present=nd.presence(art_wrap["id"]))
rep.check("a note draft's d tag is a private random id, an article's is its slug",
          note_id.startswith("note-") and len(note_id) == 37 and nd.tag_values(art_wrap or {}, "d") == [slug],
          note_id)

big = build_inner_event(kind=1, content="x" * 70_000, pubkey_hex=pk)
res = save(A, big, new_note_identifier())
# Said by its cause, never by counts (lesson 36, bf8752b): too long to be
# encrypted, with what to do instead.
refused = res.get("failed", "")
rep.check("a draft over the NIP-44 plaintext limit is refused before the signer is asked",
          "too long to be encrypted" in refused and not any(c.isdigit() for c in refused),
          refused)

# --------------------------------------------------------------------------- #
rep.section("Device B (same account, its own files) finds and decrypts them")

B = Device(b, alice_b, "B")
B.sync.start_for(alice_b)
nd.wait_until(lambda: B.ready(note_id) and B.ready(slug), 30_000)
rn, ra = B.store.get(note_id), B.store.get(slug)
rep.check("device B shows both drafts, decrypted", B.ready(note_id) and B.ready(slug),
          f"{len(B.store)} records")
if rn and ra:
    rep.check("the note's text, kind and title are right",
              rn.content == note_inner["content"] and rn.inner_kind == 1
              and rn.title == "A private note draft ✍️", repr(rn.title))
    rep.check("the article's body, title and summary are right",
              ra.content == art_inner["content"] and ra.title == "A Draft Article"
              and ["summary", "drafted live"] in ra.inner_tags, repr(ra.title))
    if EDITOR_DRAFTS_EXPIRE:
        rep.check("the expiration is known on the record",
                  ra.expiration == int(nd.tag_values(art_wrap, "expiration")[0]),
                  str(ra.expiration))
    else:
        rep.check("the record knows the draft never expires", ra.expiration is None,
                  str(ra.expiration))
nd.wait_until(lambda: not B.store.is_loading, 10_000)
rep.check("device B finished loading (all read relays answered)", not B.store.is_loading,
          B.status.last[0] if B.status.fired else "")

# --------------------------------------------------------------------------- #
rep.section("A live edit on A reaches B while B is open")

time.sleep(1.1)
note_inner2 = build_inner_event(kind=1, content="A private note draft, edited on A", pubkey_hex=pk)
res = save(A, note_inner2, note_id)
nd.wait_until(lambda: B.ready(note_id) and B.store.get(note_id).event_id == res.get("eid"), 20_000)
rep.check("B's open subscription delivers the edit and B decrypts it",
          B.store.get(note_id) is not None and B.store.get(note_id).content == note_inner2["content"],
          B.store.get(note_id).content if B.store.get(note_id) else "")
on_relay = wrap_on_relays(note_id)
rep.check("each relay keeps only the newest wrap for the d tag",
          all(len(nd.nak_req([r], kinds=[31234], authors=[pk], tags={"d": [note_id]})) == 1
              for r in (RNOSTR, NRS)) and on_relay["id"] == res.get("eid"))

# --------------------------------------------------------------------------- #
rep.section("Two saves within one second (relays keep the lowest id, NIP-01)")

time.sleep(1.1)
x1 = build_inner_event(kind=1, content="same second, first", pubkey_hex=pk)
x2 = build_inner_event(kind=1, content="same second, second", pubkey_hex=pk)
same_id = new_note_identifier()
# Two wraps signed for the same second, as two quick saves (or two devices) make them.
now = int(time.time())
signer = a.signer_for(alice)
wraps = []
for inner in (x1, x2):
    box = {}
    signer.nip44_encrypt_self(serialize_inner_event(inner),
                              lambda c: box.update(c=c), lambda e: box.update(e=e))
    nd.wait_until(lambda: box)
    unsigned = build_draft_wrap(identifier=same_id, inner_kind=1, encrypted_content=box["c"],
                                pubkey_hex=pk, client_name="MyEditor", created_at=now)
    wraps.append(a.sign(alice, unsigned))
# The one with the higher id arrives first (arrival order is up to the network).
for wrap in sorted(wraps, key=lambda w: w["id"], reverse=True):
    a.publish_raw([RNOSTR, NRS], wrap)
nd.wait_until(lambda: B.ready(same_id), 15_000)
kept = wrap_on_relays(same_id, (RNOSTR,))
rep.check("relays keep the wrap with the lowest id", kept is not None
          and kept["id"] == min(w["id"] for w in wraps), kept["id"][:12] if kept else "")
# B decrypts whichever version reaches it first and must then move to the
# one the relays keep once that arrives too; checking at the first
# decryption would race the second wrap's delivery.
nd.wait_until(lambda: kept is not None and B.store.get(same_id) is not None
              and B.store.get(same_id).event_id == kept["id"] and B.ready(same_id), 5_000)
b_record = B.store.get(same_id)
rep.check("device B shows the version the relays keep",
          b_record is not None and kept is not None and b_record.event_id == kept["id"],
          f"B shows {b_record.event_id[:12] if b_record else None}, relays keep "
          f"{kept['id'][:12] if kept else None}")
B.sync.refresh()
nd.wait_until(lambda: not B.store.is_loading, 10_000)
nd.spin(1500)
b_record = B.store.get(same_id)
rep.check("after a refresh, device B still shows what the relays keep",
          b_record is not None and kept is not None and b_record.event_id == kept["id"],
          f"B shows {b_record.event_id[:12] if b_record else None}")

rep.section("One device saving twice within a second (Ctrl+S, edit, Ctrl+S)")
lost = []
tries = 0
for attempt in range(6):
    ident = new_note_identifier()
    first = build_inner_event(kind=1, content=f"first save {attempt}", pubkey_hex=pk)
    second = build_inner_event(kind=1, content=f"second save {attempt}", pubkey_hex=pk)
    r1 = save(A, first, ident)
    r2 = save(A, second, ident)
    if r1.get("ts") != r2.get("ts"):
        continue            # the two saves fell into different seconds
    tries += 1
    kept_wrap = wrap_on_relays(ident, (RNOSTR,))
    opened = nak_open(kept_wrap) if kept_wrap else None
    if not opened or opened.get("content") != second["content"]:
        lost.append((r1.get("eid", "")[:8], r2.get("eid", "")[:8]))
if tries:
    rep.check("the later of two saves in one second is what the relays keep",
              not lost, f"{len(lost)} of {tries} same-second save pairs kept the EARLIER text "
              f"on the relays (equal created_at, lowest id wins; first/second ids {lost}); "
              "device A itself shows the later one")
else:
    rep.skip("two saves in one second", "no pair of saves fell into the same second")

# --------------------------------------------------------------------------- #
rep.section("A stale deletion from a lagging relay")

stale = build_tombstone_wrap(identifier=slug, inner_kind=30023, pubkey_hex=pk, client_name="MyEditor")
stale["created_at"] = (art_wrap or {"created_at": int(time.time())})["created_at"] - 3600
stale_signed = a.sign(alice, stale)
a.publish_raw([STRFRY], stale_signed)       # strfry is among B's read relays, not a draft relay
nd.spin(2000)
rep.check("an older empty replacement (a deletion signed an hour before the draft) "
          "does not remove the newer draft", B.ready(slug),
          "removed on B: draft_sync.py drops the record for any empty-content wrap "
          "without comparing created_at" if not B.ready(slug) else "kept")
B.sync.refresh()
nd.spin(4000)
rep.check("after a refresh the newer draft is shown again", B.ready(slug),
          "present" if B.ready(slug) else "missing")

# --------------------------------------------------------------------------- #
rep.section("NIP-40: a draft past its expiration")

exp_id = new_note_identifier()
box = {}
signer.nip44_encrypt_self(
    serialize_inner_event(build_inner_event(kind=1, content="expired", pubkey_hex=pk)),
    lambda c: box.update(c=c), lambda e: box.update(e=e))
nd.wait_until(lambda: box)
# Saved 100 days ago, so its 90-day expiration passed 10 days ago.
expired = build_draft_wrap(identifier=exp_id, inner_kind=1, encrypted_content=box["c"],
                           pubkey_hex=pk, client_name="MyEditor",
                           created_at=int(time.time()) - 100 * 86400,
                           expiration_seconds=90 * 86400)
expired_signed = a.sign(alice, expired)
accepted = a.publish_raw([RNOSTR, NRS, STRFRY], expired_signed)
served = {r for r in (RNOSTR, NRS, STRFRY) if nd.nak_req([r], ids=[expired_signed["id"]])}
B.sync.refresh()
nd.spin(4000)
rep.info("relays and an expired draft",
         f"accepted by {[nd.RELAY_NAMES[u] for u, ok, _ in accepted if ok]}, still served by "
         f"{sorted(nd.RELAY_NAMES[r] for r in served)}")
if served:
    rep.check("an expired draft is not shown (NIP-40: clients SHOULD ignore expired events)",
              B.store.get(exp_id) is None,
              "shown on B" if B.store.get(exp_id) else "not shown")
else:
    rep.skip("an expired draft is not shown (NIP-40)",
             "every ndak relay refuses an already expired event, so none reaches the app; "
             "draft_sync.py has no expiration check of its own (code review)")

# --------------------------------------------------------------------------- #
rep.section("Deleting one draft, then two at once")

time.sleep(1.1)
res = {}
job = DraftDeleteJob(relay_pool=a.pool, relay_directory=a.directory, session_pool=a.sessions,
                     profile=alice, identifier=note_id, inner_kind=1)
job.tombstoned.connect(lambda d, eid: (res.update(eid=eid), A.store.remove(d)))
job.completed.connect(lambda r: res.update(results=r))
job.failed.connect(lambda r: res.update(failed=r))
job.start()
nd.wait_until(lambda: "results" in res or "failed" in res, 30_000)
tomb = wrap_on_relays(note_id)
if rep.check("the deletion is an empty replacement on the relays (NIP-37, not NIP-09)",
             tomb is not None and tomb["id"] == res.get("eid") and tomb["content"] == ""):
    nd.review_event(rep, "draft deletion", tomb, kind=31234)
    if EDITOR_DRAFTS_EXPIRE:
        rep.check("the deletion keeps d and k, with a fresh 90-day expiration",
                  [x[0] for x in tomb["tags"]] == ["d", "k", "expiration", "client"]
                  and nd.tag_values(tomb, "d") == [note_id] and nd.tag_values(tomb, "k") == ["1"])
    else:
        # An expiring deletion would let a lagging relay's copy of the draft
        # come back once it lapsed.
        rep.check("the deletion keeps d and k and never expires",
                  [x[0] for x in tomb["tags"]] == ["d", "k", "client"]
                  and nd.tag_values(tomb, "d") == [note_id] and nd.tag_values(tomb, "k") == ["1"],
                  json.dumps(tomb["tags"]))
    where = {r for r, ok in nd.presence(tomb["id"]).items() if ok}
    rep.check("the deletion goes wherever drafts are read (own relays plus the fallback set)",
              where == {RNOSTR, NRS, STRFRY}, ", ".join(sorted(nd.RELAY_NAMES[r] for r in where)))
    rep.event("draft deletion", tomb, claimed=[u for u, ok, _ in res["results"] if ok],
              expected=[RNOSTR, NRS, STRFRY], present=nd.presence(tomb["id"]))
nd.wait_until(lambda: B.store.get(note_id) is None, 15_000)
rep.check("device B drops the deleted draft", B.store.get(note_id) is None)

time.sleep(1.1)
bulk = {}
job = DraftBulkDeleteJob(relay_pool=a.pool, relay_directory=a.directory, session_pool=a.sessions,
                         profile=alice, targets=[(slug, 30023), (same_id, 1)])
progress = nd.Catch(job.progress)
job.tombstoned.connect(lambda d, eid: A.store.remove(d))
job.finished.connect(lambda n, failures: bulk.update(n=n, failures=failures))
job.start()
nd.wait_until(lambda: bulk, 40_000)
rep.check("bulk delete: both deleted, progress reported per draft",
          bulk.get("n") == 2 and bulk.get("failures") == [] and progress.calls == [(1, 2), (2, 2)],
          f"{bulk} progress {progress.calls}")
nd.wait_until(lambda: B.store.get(slug) is None and B.store.get(same_id) is None, 15_000)
rep.check("device B drops both", B.store.get(slug) is None and B.store.get(same_id) is None)
for ident in (slug, same_id):
    t = wrap_on_relays(ident)
    rep.check(f"relays hold an empty replacement for {ident[:20]}", t is not None and t["content"] == "")

# --------------------------------------------------------------------------- #
rep.section("A new device after the deletions")

c = nd.Stack("c")
c.vault.store(sk)
alice_c = local_profile(pk)
c.store.upsert(alice_c)
C = Device(c, alice_c, "C")
C.sync.start_for(alice_c)
nd.wait_until(lambda: not C.store.is_loading, 20_000)
nd.spin(1500)
rep.check("a device that starts later shows none of the deleted drafts",
          all(C.store.get(i) is None for i in (note_id, slug, same_id)),
          f"{[r.identifier for r in C.store]}")

# --------------------------------------------------------------------------- #
rep.section("One read relay that cannot be reached (a membership relay that is down)")

d = nd.Stack("d")
d.vault.store(sk)
alice_d = local_profile(pk)
d.store.upsert(alice_d)
keep_id = new_note_identifier()
save(A, build_inner_event(kind=1, content="still here", pubkey_hex=pk), keep_id)
D = Device(d, alice_d, "D", entitled=["ws://localhost:7798"])
D.sync.start_for(alice_d)
nd.wait_until(lambda: D.ready(keep_id), 20_000)
rep.check("the drafts on the relays that answer are shown", D.ready(keep_id))
nd.wait_until(lambda: not D.store.is_loading, 15_000)
rep.check("loading ends although one relay never answered",
          not D.store.is_loading,
          f"still loading after 15 s, status {D.status.last[0] if D.status.fired else ''!r} "
          "(draft_sync.py listens only for the all-relays eose, which never fires when one "
          "relay fails)" if D.store.is_loading else "")

for s in (a, b, c, d):
    s.close()
sys.exit(rep.finish())
