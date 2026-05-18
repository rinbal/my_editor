"""Manual network smoke test: publish a kind 1 note to real relays.

Not run by pytest (no ``test_`` prefix). Run with:

    .venv/bin/python tests/smoke_relay_publish.py

Generates a throwaway keypair in-memory, builds a self-signed kind 1
event with a "this is a smoke test, please ignore" content, and
publishes it to the curated Buho_go relay set. Exits 0 if at least one
relay returned OK true. Prints per-relay outcome.

This is the protocol-level proof that:
  - canonical event serialization matches what relays expect
  - the schnorr signature is verifiable by relays
  - QWebSocket TLS + framing work
  - parallel publish + per-relay timeout work as designed
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication, QTimer

# Allow running this file directly from the repo root.
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nostr import CLIENT_NAME, DEFAULT_RELAYS, crypto, events  # noqa: E402
from nostr.relay import RelayPool  # noqa: E402


RELAYS = list(DEFAULT_RELAYS)


def main() -> int:
    app = QCoreApplication(sys.argv)

    sk = crypto.generate_secret_key()
    pk_hex = crypto.get_public_key(sk).hex()
    print(f"throwaway pubkey: {pk_hex}")

    event = events.build_event(
        kind=1,
        content=(
            "minimal-texteditor smoke test. Please ignore. "
            "If you see this on your feed it means the new Nostr publish path works."
        ),
        tags=[["client", CLIENT_NAME]],
        sk=sk,
    )
    print(f"event id:         {event['id']}")
    print(f"publishing to {len(RELAYS)} relays...")
    print()

    pool = RelayPool()
    job = pool.publish(RELAYS, event, timeout_ms=10_000)

    exit_code = {"value": 1}

    def on_first(url: str) -> None:
        print(f"  ✓ first ack from {url}")

    def on_result(url: str, ok: bool, msg: str) -> None:
        marker = "OK " if ok else "FAIL"
        print(f"  [{marker}] {url:42s} {msg!r}")

    def on_done(results: list) -> None:
        accepted = sum(1 for _, ok, _ in results if ok)
        print()
        print(f"--- {accepted}/{len(results)} relays accepted ---")
        exit_code["value"] = 0 if accepted > 0 else 1
        pool.close_all()
        # Give Qt a tick to flush close frames before quitting.
        QTimer.singleShot(100, app.quit)

    job.first_accept.connect(on_first)
    job.relay_result.connect(on_result)
    job.all_done.connect(on_done)

    # Hard ceiling — should never trigger; protects against signal-wiring bugs.
    QTimer.singleShot(20_000, lambda: (print("HARD TIMEOUT"), app.exit(2)))

    app.exec()
    return exit_code["value"]


if __name__ == "__main__":
    sys.exit(main())
