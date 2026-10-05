# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check which relays deserve a place in nostr/outbox/defaults.py.

Not run by pytest (no ``test_`` prefix). Run it before changing the
default relays, and after, update QUALIFIED_ON there:

    .venv/bin/python tests/smoke_relay_qualify.py            # the built-in candidates
    .venv/bin/python tests/smoke_relay_qualify.py wss://x    # or relays of your choice
    .venv/bin/python tests/smoke_relay_qualify.py --json     # machine-readable
    .venv/bin/python tests/smoke_relay_qualify.py --wait=6   # read back after 6 s, not 2.5

``--wait=SECONDS`` sets how long to wait between a write and its
read-back, for relays that store what they accept with a delay; a relay
that only passes with a longer wait is slow to keep things, and worth
knowing about.

For each relay it asks four questions, using MyEditor's own relay code so
the answer is what the app will meet:

  1. Its NIP-11 document: does it ask for payment or a login, or limit
     who may write? (Fetched over https for wss://, http for ws://, on
     worker threads, so a relay whose web server never answers holds up
     nothing else.)
  2. A read: does it end the stored events (EOSE) for an ordinary query,
     and how quickly?
  3. A write: does it accept a relay list (kind 10002) and a profile
     (kind 0) from a key it has never seen?
  4. A read-back: can what it accepted be fetched again by id? An "OK"
     alone proves nothing; some relays acknowledge and keep nothing.

The writes come from a throwaway key made for this run and carry no
content of note. They are marked to expire after ten minutes (NIP-40),
so relays that honour expiration drop them; a relay that does not keeps
two small events from a key nobody uses again.

A relay qualifies as a home relay when it is free, open, answers in
under three seconds, and passes write and read-back for both kinds. An
indexer needs only the relay list and the profile.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QCoreApplication, QTimer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from nostr import crypto, events  # noqa: E402
from nostr.outbox import defaults  # noqa: E402
from nostr.relay import RelayPool  # noqa: E402

CANDIDATES = [
    # in use today
    *defaults.FALLBACK_RELAYS,
    *defaults.INDEXER_RELAYS,
    # previous defaults and common picks
    "wss://nostr-01.yakihonne.com", "wss://nostr-02.yakihonne.com",
    "wss://nostr.oxtr.dev", "wss://theforest.nostr1.com", "wss://relay.nostr.band",
    "wss://relay.snort.social", "wss://nostr.mom", "wss://offchain.pub",
    "wss://relay.nos.social", "wss://nostr.bitcoiner.social", "wss://nostr21.com",
    "wss://purplerelay.com", "wss://relay.0xchat.com", "wss://nostr.wine",
    "wss://directory.yabu.me", "wss://search.nos.today", "wss://relay.nsec.app",
    # the association's members' relay (writes restricted to members)
    "wss://nostr.einundzwanzig.space",
]

# A well-known author, for a read that should find something.
READ_AUTHOR = "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
READ_TIMEOUT_MS = 8_000
WRITE_TIMEOUT_MS = 8_000
READ_BACK_DELAY_MS = 2_500     # --wait=SECONDS for relays that store with a delay
FAST_EOSE_S = 3.0
NIP11_TIMEOUT_S = 6


def nip11(url: str) -> dict:
    """The relay's NIP-11 document, at the same address over http(s).
    Blocking: run it on a worker thread, never on the Qt event loop."""
    scheme, rest = url.split("://", 1)
    http = ("http://" if scheme.lower() == "ws" else "https://") + rest
    request = urllib.request.Request(http, headers={"Accept": "application/nostr+json",
                                                    "User-Agent": "MyEditor relay check"})
    try:
        with urllib.request.urlopen(request, timeout=NIP11_TIMEOUT_S) as response:
            data = json.loads(response.read(256 * 1024).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001, a missing document is a finding, not a crash
        return {"error": type(exc).__name__}
    limits = data.get("limitation") or {}
    return {
        "name": str(data.get("name") or "")[:40],
        "payment_required": bool(limits.get("payment_required")),
        "auth_required": bool(limits.get("auth_required")),
        "restricted_writes": bool(limits.get("restricted_writes")),
        "nips": sorted(n for n in data.get("supported_nips") or [] if isinstance(n, int)),
    }


class Probe:
    """Read, write and read back on one relay; ``done`` is called once."""

    def __init__(self, pool: RelayPool, url: str, key: bytes, done) -> None:
        self.url = url
        self.pool = pool
        self.key = key
        self.done = done
        self.result = {"relay": url, "eose_s": None, "writes": {}, "read_back": {}}
        self._ids = {}
        self._finished = False

    def start(self) -> None:
        started = time.monotonic()
        sub = self.pool.subscribe([self.url], [{"kinds": [10002], "authors": [READ_AUTHOR],
                                                "limit": 1}])

        def eose():
            self.result["eose_s"] = round(time.monotonic() - started, 2)
            sub.close()
            self._write()

        sub.eose.connect(eose)
        sub.closed.connect(lambda reason: self._note("read_closed", reason))
        QTimer.singleShot(READ_TIMEOUT_MS, lambda: self.result["eose_s"] is None
                          and (sub.close(), self._write()))

    def _note(self, key, value) -> None:
        self.result[key] = value

    def _write(self) -> None:
        if self._ids:
            return
        expires = str(int(time.time()) + 600)
        templates = {
            10002: {"kind": 10002, "content": "",
                    "tags": [["r", "wss://relay.example.invalid"], ["expiration", expires]]},
            0: {"kind": 0, "content": "{}", "tags": [["expiration", expires]]},
        }
        pending = {"count": len(templates)}
        for kind, template in templates.items():
            event = events.sign_event(dict(template, created_at=int(time.time())), self.key)
            self._ids[kind] = event["id"]
            job = self.pool.publish([self.url], event, timeout_ms=WRITE_TIMEOUT_MS)

            def finished(results, k=kind, j=job):
                ok, message = (results[0][1], results[0][2]) if results else (False, "no answer")
                self.result["writes"][k] = {"ok": ok, "message": str(message)[:80]}
                pending["count"] -= 1
                if pending["count"] == 0:
                    QTimer.singleShot(READ_BACK_DELAY_MS, self._read_back)

            job.all_done.connect(finished)

    def _read_back(self) -> None:
        ids = list(self._ids.values())
        author = crypto.get_public_key(self.key).hex()
        # Asked two ways: some relays answer one form of query and not the other.
        sub = self.pool.subscribe([self.url], [{"ids": ids},
                                               {"authors": [author], "kinds": [0, 10002]}])
        found = set()
        sub.event.connect(lambda event: found.add(event.get("id")))

        def finish():
            if self._finished:
                return
            self._finished = True
            sub.close()
            for kind, event_id in self._ids.items():
                self.result["read_back"][kind] = event_id in found
            self.done(self.result)

        sub.eose.connect(finish)
        QTimer.singleShot(READ_TIMEOUT_MS, finish)


def verdict(row: dict) -> str:
    info = row["nip11"]
    if info.get("payment_required"):
        return "paid"
    if info.get("auth_required"):
        return "login required"
    if row["eose_s"] is None:
        return "unreachable"
    wrote = {k: row["writes"].get(k, {}).get("ok") and row["read_back"].get(k)
             for k in (10002, 0)}
    if not any(wrote.values()):
        return "restricted writes" if info.get("restricted_writes") else "keeps nothing"
    if not all(wrote.values()):
        return "partial (" + ", ".join(f"kind {k}" for k, v in wrote.items() if v) + " only)"
    if row["eose_s"] > FAST_EOSE_S:
        return "slow"
    return "qualifies"


def main(argv) -> int:
    global READ_BACK_DELAY_MS
    as_json = "--json" in argv
    for arg in argv:
        if arg.startswith("--wait="):
            READ_BACK_DELAY_MS = int(float(arg.split("=", 1)[1]) * 1000)
    relays = [a for a in argv if a.startswith("wss://") or a.startswith("ws://")] or CANDIDATES
    relays = list(dict.fromkeys(relays))
    app = QCoreApplication(sys.argv[:1])
    pool = RelayPool()
    key = crypto.generate_secret_key()
    rows = []
    # NIP-11 documents come over plain HTTP, which blocks: on worker
    # threads, started now, so the relay probes below never wait on them.
    workers = ThreadPoolExecutor(max_workers=16)
    documents = {url: workers.submit(nip11, url) for url in relays}

    def done(result):
        rows.append(result)
        if len(rows) == len(relays):
            app.quit()

    for url in relays:
        Probe(pool, url, key, done).start()
    QTimer.singleShot(60_000 + READ_BACK_DELAY_MS, app.quit)
    app.exec()
    pool.close_all()
    for row in rows:
        try:
            row["nip11"] = documents[row["relay"]].result(timeout=NIP11_TIMEOUT_S + 2)
        except Exception as exc:  # noqa: BLE001, a missing document is a finding
            row["nip11"] = {"error": type(exc).__name__}
        row["verdict"] = verdict(row)
    workers.shutdown(wait=False, cancel_futures=True)

    rows.sort(key=lambda r: relays.index(r["relay"]))
    if as_json:
        print(json.dumps({"checked": time.strftime("%Y-%m-%d"), "relays": rows}, indent=2))
        return 0
    print(f"{'relay':38} {'verdict':24} {'eose':>6}  writes(10002,0)  nip11")
    for r in rows:
        w = "".join("y" if r["writes"].get(k, {}).get("ok") and r["read_back"].get(k) else
                    ("a" if r["writes"].get(k, {}).get("ok") else "-") for k in (10002, 0))
        flags = ",".join(f for f in ("payment_required", "auth_required", "restricted_writes")
                         if r["nip11"].get(f)) or r["nip11"].get("error", "")
        eose = f"{r['eose_s']:.2f}" if r["eose_s"] is not None else "-"
        print(f"{r['relay']:38} {r['verdict']:24} {eose:>6}  {w:15}  {flags}")
    missing = set(relays) - {r["relay"] for r in rows}
    for url in missing:
        print(f"{url:38} {'no result (timed out)':24}")
    print("\nwrites: y = accepted and read back, a = accepted but not found again, - = refused")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
