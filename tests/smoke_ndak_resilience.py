# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live check against ndak: what happens when a relay restarts under the app.

Not run by pytest (no ``test_`` prefix). Restarts the nostr-rs-relay
container of ndak once (its data is kept in its volume):

    (cd ~/projects/ndak && docker compose up -d)
    .venv/bin/python tests/smoke_ndak_resilience.py

A phone signer's relay connection drops all the time, so this matters for
every NIP-46 user: after the relay the signer channel uses comes back, does
the paired signer still answer? And does a draft another device saves while
the app is open still arrive?
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_ndak_common as nd  # noqa: E402

nd.bootstrap("resilience")
app = nd.qt_app()

from nostr.draft_store import DraftState, DraftStore  # noqa: E402
from nostr.draft_sync import DraftSync  # noqa: E402
from nostr.drafts import build_draft_wrap, build_inner_event, new_note_identifier, serialize_inner_event  # noqa: E402
from nostr.publisher import build_note  # noqa: E402
from nostr.ui.connect_dialog import ConnectDialog  # noqa: E402

RNOSTR, NRS, STRFRY = nd.RNOSTR, nd.NRS, nd.STRFRY
CONTAINER = "ndak-nostr-rs-relay-1"
rep = nd.Report("resilience")


def restart_relay() -> bool:
    subprocess.run(["docker", "restart", CONTAINER], capture_output=True, timeout=120)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            result = nd.nak("req", "-k", "0", "-l", "1", NRS, timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and "... ok" in result.stderr:
            return True
        time.sleep(1)
    return False


def try_sign(stack, profile, label, timeout_ms=45_000):
    client = stack.signer_for(profile)
    box = {}
    started = time.monotonic()
    client.sign_event(build_note(label, profile.user_pubkey), lambda ev: box.update(ok=ev),
                      lambda reason: box.update(err=reason))
    nd.wait_until(lambda: box, timeout_ms)
    return box, time.monotonic() - started


stack = nd.Stack()
_, dana_hex, dana_pk = nd.new_identity()
secret = uuid.uuid4().hex[:12]
bunker = nd.NakBunker(dana_hex, [NRS], secret=secret)
try:
    rep.section("A remote signer on one relay, and that relay restarts")
    uri = bunker.uri()
    uri = uri.split("&secret=")[0] + f"&secret={secret}" if "secret=" in uri else uri + f"&secret={secret}"
    dialog = ConnectDialog(stack.pool, stack.store, is_dark=False)
    connected = nd.Catch(dialog.profile_connected)
    dialog.details_toggle.setChecked(True)          # the connection address is in Details
    dialog._paste_edit.setText(uri)
    dialog._paste_btn.click()
    connected.wait(60_000)
    dana = connected.last[0] if connected.fired else None
    if rep.check("the signer is paired", dana is not None, dialog._status.text()) and dana:
        box, took = try_sign(stack, dana, "before the restart")
        rep.check("it signs before the restart", "ok" in box, str(box.get("err")))
        rep.check("the relay restarts and accepts connections again", restart_relay())
        nd.spin(3000)
        fresh = nd.Stack("fresh")
        fresh.store.upsert(dana)
        box2, _ = try_sign(fresh, dana, "a new channel after the restart")
        rep.check("control: the signer answers a new channel after the restart (nak bunker is fine)",
                  "ok" in box2, str(box2.get("err")))
        box3, took3 = try_sign(stack, dana, "the same channel after the restart")
        rep.check("the channel opened before the restart still gets the signer's answers",
                  "ok" in box3, f"{box3.get('err')} after {took3:.0f} s (relay.py: a Subscription "
                  "never sends its REQ again when its relay reconnects, and the session pool "
                  "keeps handing out the client)")
        if "err" in box3:
            box4, _ = try_sign(stack, dana, "the same channel, second try")
            rep.check("a second try on that channel recovers", "ok" in box4, str(box4.get("err")))
        fresh.close()
finally:
    bunker.stop()

rep.section("A draft saved elsewhere while the app is open, after a relay restart")
alice, sk, setup = nd.local_account(stack, "Alice Resilience")
pk = alice.user_pubkey
store = DraftStore()
sync = DraftSync(relay_pool=stack.pool, relay_directory=stack.directory, session_pool=stack.sessions,
                 store=store, entitled_relays=lambda: [])
sync.start_for(alice)
nd.wait_until(lambda: not store.is_loading, 15_000)
signer = stack.signer_for(alice)


def wrap_to(relays, text):
    box = {}
    signer.nip44_encrypt_self(serialize_inner_event(build_inner_event(kind=1, content=text, pubkey_hex=pk)),
                              lambda c: box.update(c=c), lambda e: box.update(e=e))
    nd.wait_until(lambda: box)
    ident = new_note_identifier()
    unsigned = build_draft_wrap(identifier=ident, inner_kind=1, encrypted_content=box["c"],
                                pubkey_hex=pk, client_name="MyEditor")
    signed = stack.sign(alice, unsigned)
    other = nd.Stack(f"other-{ident[:6]}")
    other.publish_raw(relays, signed)
    other.close()
    return ident


ident = wrap_to([NRS], "saved on another device, only nostr-rs-relay has it")
nd.wait_until(lambda: store.get(ident) is not None and store.get(ident).state is DraftState.READY, 15_000)
rep.check("before the restart, a draft saved elsewhere arrives live", store.get(ident) is not None)
rep.check("the relay restarts", restart_relay())
nd.spin(3000)
# Something the app does reopens the socket (any publish to that relay).
stack.publish_raw([NRS], stack.sign(alice, build_note("reopen the socket", pk)))
ident2 = wrap_to([NRS], "saved elsewhere after the restart")
nd.wait_until(lambda: store.get(ident2) is not None, 20_000)
rep.check("after the restart, a draft saved elsewhere still arrives live",
          store.get(ident2) is not None,
          "not seen until the 5-minute refresh: the subscription is not sent again on the "
          "reconnected socket" if store.get(ident2) is None else "")
sync.refresh()
nd.wait_until(lambda: store.get(ident2) is not None, 15_000)
rep.check("a refresh finds it", store.get(ident2) is not None)

stack.close()
sys.exit(rep.finish())
