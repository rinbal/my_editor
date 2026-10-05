# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual end-to-end check: the app's membership client through the real
sidecar to a stand-in association.

Not run by pytest (no ``test_`` prefix). Needs the sidecar's dependencies
(``pip install -r sidecar/requirements.txt``). Run from the repo root:

    .venv/bin/python tests/smoke_sidecar_e2e.py

It starts a tiny stand-in association that insists on the client key, the
sidecar (uvicorn) pointing at it with that key, and the app's own
MembershipApi signing with a throwaway local key. The app is told about
the stand-in the way a developer would tell it, with
MYEDITOR_MEMBERSHIP_UPSTREAM. It proves the two halves agree: the sidecar
accepts the app's signature (which names the association's URL), adds the
key, and the answer comes back parsed. Exits 1 when they do not.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

KEY = "e2e-key"
MEMBERSHIP = {
    "pubkey": "ab" * 32,
    "association_status": "DEFAULT",
    "association_status_value": 1,
    "membership_status": "none",
    "statutes_accepted_at": None,
    "applied_at": None,
    "current_year": {"year": 2026, "fee": 21, "currency": "CHF", "paid": False,
                     "receipt_url": None},
}

seen = []


class Association(BaseHTTPRequestHandler):
    """Answers GET /me, and only with the right client key."""

    def do_GET(self):
        key = self.headers.get("X-Api-Key")
        seen.append((self.command, self.path, key, bool(self.headers.get("Authorization"))))
        if key == KEY:
            status, document = 200, {"data": MEMBERSHIP}
        else:
            status, document = 401, {"message": "bad key"}
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_healthy(port: int, process: subprocess.Popen, timeout: float = 20.0) -> None:
    """Return once the sidecar answers /healthz; raise if it never does."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the sidecar exited with {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as answer:
                if answer.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    raise RuntimeError("the sidecar did not become healthy in time")


def run_client(sidecar_port: int) -> dict:
    """The app's own client against the sidecar: /status, then GET /me."""
    from PySide6.QtCore import QCoreApplication, QTimer

    from nostr import crypto
    from nostr.einundzwanzig_api import MembershipApi
    from nostr.local_signer import LocalSigner

    app = QCoreApplication(sys.argv)
    signer = LocalSigner(crypto.generate_secret_key())

    def sign(unsigned, on_success, on_failure):
        signer.sign_event(unsigned, on_success, on_failure)

    api = MembershipApi(sign, service_url=f"http://localhost:{sidecar_port}")
    result = {}

    def on_member(status):
        result["me"] = status.membership_status
        app.quit()

    def on_failure(error):
        result["me"] = f"FAILED {error.code} {error.status}"
        app.quit()

    api.check_service(lambda ok: result.setdefault("status", ok))
    api.me(on_member, on_failure)
    QTimer.singleShot(15000, app.quit)
    app.exec()
    return result


def main() -> int:
    association_port, sidecar_port = free_port(), free_port()
    association = ThreadingHTTPServer(("127.0.0.1", association_port), Association)
    threading.Thread(target=association.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{association_port}"
    os.environ["MYEDITOR_MEMBERSHIP_UPSTREAM"] = upstream

    env = dict(os.environ, E21_API_KEY=KEY, E21_UPSTREAM=upstream,
               SIDECAR_LOG_LEVEL="WARNING")
    sidecar = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "sidecar.app:app", "--port", str(sidecar_port),
         "--log-level", "warning", "--no-access-log"],
        cwd=ROOT, env=env)
    try:
        wait_until_healthy(sidecar_port, sidecar)
        result = run_client(sidecar_port)
    finally:
        sidecar.terminate()
        sidecar.wait(10)
        association.shutdown()

    print("status available:", result.get("status"))
    print("me:", result.get("me"))
    print("association saw:", seen)
    agreed = (result.get("status") is True and result.get("me") == "none"
              and seen == [("GET", "/api/v1/membership/me", KEY, True)])
    print("OK" if agreed else "MISMATCH")
    return 0 if agreed else 1


if __name__ == "__main__":
    sys.exit(main())
