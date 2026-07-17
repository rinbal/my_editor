# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual network smoke test: full NIP-37 draft round-trip on real relays.

Not run by pytest (no ``test_`` prefix). Run with:

    .venv/bin/python tests/smoke_relay_drafts.py

What it does, end to end:

  1. Generate a throwaway secp256k1 keypair in-memory.
  2. Build a kind-1 short-note inner event.
  3. NIP-44-encrypt the serialized inner to the *same* pubkey (self-
     encryption — the NIP-37 contract). We use ``crypto.encrypt_to``
     directly rather than the bunker because this test owns the key.
  4. Wrap the ciphertext in a kind-31234 draft event with the spec
     tag set (d / k / expiration / client), sign locally.
  5. Publish to the curated relay set.
  6. Subscribe back with ``{"kinds":[31234], "authors":[<pk>]}``.
  7. ``parse_wrap_event`` the returned event, decrypt the content,
     ``parse_inner_event`` the plaintext, and verify the round-trip:
     same content, same kind, same identifier.
  8. Print a NostrLens search URL so a human can also verify the
     wrap shows up in a web UI.

Exits 0 if at least one relay accepted AND the round-trip decoded
successfully.

This is the protocol-level proof that:
  - The NIP-37 wrap shape we emit is accepted by mainstream relays.
  - Other clients (in principle) would be able to discover, decrypt,
    and parse the draft.
  - Our NIP-44 v2 ciphertext round-trips through real relays.
"""

from __future__ import annotations

import json
import sys
import time

from PySide6.QtCore import QCoreApplication, QTimer

# Allow running from the repo root.
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nostr import CLIENT_NAME, DEFAULT_RELAYS, crypto, events  # noqa: E402
from nostr.bech32 import encode_npub  # noqa: E402
from nostr.drafts import (  # noqa: E402
    DRAFT_WRAP_KIND,
    INNER_KIND_SHORT_NOTE,
    build_draft_wrap,
    build_inner_event,
    new_note_identifier,
    parse_inner_event,
    parse_wrap_event,
    serialize_inner_event,
)
from nostr.queries import fetch_addressable_events  # noqa: E402
from nostr.relay import RelayPool  # noqa: E402


RELAYS = list(DEFAULT_RELAYS)
DRAFT_BODY = (
    "minimal-texteditor NIP-37 round-trip smoke test. "
    "If you are reading this, the draft wrap was decrypted successfully. "
    f"Run at {int(time.time())}."
)


def main() -> int:
    app = QCoreApplication(sys.argv)
    pool = RelayPool()

    # -- step 1: keypair ----------------------------------------------------
    sk = crypto.generate_secret_key()
    pk_bytes = crypto.get_public_key(sk)
    pk_hex = pk_bytes.hex()
    npub = encode_npub(pk_hex)
    print(f"throwaway pubkey hex: {pk_hex}")
    print(f"throwaway npub:       {npub}")
    print()

    # -- step 2-4: build inner, encrypt to self, wrap, sign -----------------
    identifier = new_note_identifier()
    inner = build_inner_event(
        kind=INNER_KIND_SHORT_NOTE,
        content=DRAFT_BODY,
        pubkey_hex=pk_hex,
    )
    plaintext = serialize_inner_event(inner)

    # Self-encryption: peer pubkey is our own pubkey.
    ciphertext = crypto.encrypt_to(plaintext, sk, pk_bytes)

    wrap_unsigned = build_draft_wrap(
        identifier=identifier,
        inner_kind=INNER_KIND_SHORT_NOTE,
        encrypted_content=ciphertext,
        pubkey_hex=pk_hex,
        client_name=CLIENT_NAME,
    )
    # Locally sign — same code path the bunker would use server-side.
    wrap_signed = events.build_event(
        kind=wrap_unsigned["kind"],
        content=wrap_unsigned["content"],
        tags=wrap_unsigned["tags"],
        sk=sk,
        created_at=wrap_unsigned["created_at"],
    )
    print(f"draft d-tag:    {identifier}")
    print(f"wrap event id:  {wrap_signed['id']}")
    print(f"wrap kind:      {wrap_signed['kind']}")
    print(f"wrap tags:      {wrap_signed['tags']}")
    print()

    exit_code = {"value": 1}

    # -- step 5: publish ----------------------------------------------------
    print(f"publishing wrap to {len(RELAYS)} relays...")
    pub = pool.publish(RELAYS, wrap_signed, timeout_ms=10_000)

    def on_pub_result(url: str, ok: bool, msg: str) -> None:
        marker = "OK  " if ok else "FAIL"
        print(f"  [{marker}] {url:42s} {msg!r}")

    def on_pub_done(results: list) -> None:
        accepted = sum(1 for _, ok, _ in results if ok)
        print()
        print(f"--- publish: {accepted}/{len(results)} relays accepted ---")
        if accepted == 0:
            print("FAIL: no relay accepted the wrap; aborting roundtrip.")
            pool.close_all()
            QTimer.singleShot(100, app.quit)
            return
        # Give the network 1.5s to propagate before subscribing back —
        # some relays index asynchronously after returning OK.
        QTimer.singleShot(1500, lambda: roundtrip_fetch(accepted_urls(results)))

    def accepted_urls(results: list) -> list:
        return [url for url, ok, _ in results if ok]

    pub.relay_result.connect(on_pub_result)
    pub.all_done.connect(on_pub_done)

    # -- step 6-7: subscribe back, decrypt, verify --------------------------
    def roundtrip_fetch(urls: list) -> None:
        print()
        print(f"subscribing back on {len(urls)} relays to fetch the wrap...")
        fetch_addressable_events(
            pool,
            urls,
            filters=[{"kinds": [DRAFT_WRAP_KIND], "authors": [pk_hex]}],
            on_done=on_fetch_done,
            timeout_ms=8_000,
            parent=pool,
        )

    def on_fetch_done(found: list) -> None:
        print(f"  fetched {len(found)} addressable event(s) by (kind,pubkey,d)")
        if not found:
            print("FAIL: relay did not return the wrap we just published")
            pool.close_all()
            QTimer.singleShot(100, app.quit)
            return

        ev = found[0]
        meta = parse_wrap_event(ev)
        if meta is None:
            print(f"FAIL: parse_wrap_event returned None for {ev!r}")
            pool.close_all()
            QTimer.singleShot(100, app.quit)
            return
        print(f"  parsed wrap: d={meta.identifier!r} k={meta.inner_kind} "
              f"created_at={meta.created_at} expiration={meta.expiration}")

        # Decrypt: same conversation key as encryption since we own sk.
        try:
            decrypted = crypto.decrypt_from(meta.ciphertext, sk, pk_bytes)
            inner_back = parse_inner_event(decrypted)
        except Exception as exc:  # noqa: BLE001 — surface decrypt errors to console
            print(f"FAIL: decrypt/parse error: {exc}")
            pool.close_all()
            QTimer.singleShot(100, app.quit)
            return

        # Round-trip integrity check.
        problems = []
        if inner_back["kind"] != INNER_KIND_SHORT_NOTE:
            problems.append(f"kind mismatch {inner_back['kind']}")
        if inner_back["content"] != DRAFT_BODY:
            problems.append("body mismatch")
        if inner_back["pubkey"] != pk_hex:
            problems.append(f"pubkey mismatch {inner_back['pubkey']}")
        if meta.identifier != identifier:
            problems.append(f"identifier mismatch {meta.identifier}")

        if problems:
            print(f"FAIL: round-trip integrity: {problems}")
            pool.close_all()
            QTimer.singleShot(100, app.quit)
            return

        print()
        print(f"PASS: round-trip OK")
        print(f"      decrypted body preview: {inner_back['content'][:80]!r}")
        print()
        print(f"Inspect the wrap on NostrLens:")
        print(f"  https://nostr-dev.netlify.app/")
        print(f"  Search for event id: {ev['id']}")
        print(f"  Or by author:        {pk_hex}")
        print(f"  Or by npub:          {npub}")
        print(f"  Filter kind:         {DRAFT_WRAP_KIND}")
        print()
        print(f"Note: the wrap will expire from relays after ~90 days "
              f"(NIP-40 expiration tag). Some relays may evict sooner.")
        exit_code["value"] = 0
        pool.close_all()
        QTimer.singleShot(100, app.quit)

    # Hard ceiling for the whole pipeline (publish + propagate + fetch).
    QTimer.singleShot(30_000, lambda: (print("HARD TIMEOUT — exiting"), app.exit(2)))

    app.exec()
    return exit_code["value"]


if __name__ == "__main__":
    sys.exit(main())
