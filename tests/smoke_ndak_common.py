# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared bootstrap for the live ndak smoke tests (tests/smoke_ndak_*.py).

Not collected by pytest (no ``test_`` prefix), and not a test by itself.

How to run the smoke_ndak scripts:

    (cd ~/projects/ndak && docker compose up -d)      # the five local relays
    .venv/bin/python tests/smoke_ndak_<area>.py       # one area
    .venv/bin/python tests/smoke_ndak_all.py          # every area, one after another

ndak (https://github.com/DoktorShift/ndak) runs five relays on this computer:
rnostr ws://localhost:7777 (NIP-50 search), nostr-rs-relay ws://localhost:7779,
strfry ws://localhost:7780, obelisk ws://localhost:7781 and khatru
ws://localhost:7782. `nak` must be on PATH; the media script also needs Docker
for its local Blossom servers.

Every script calls ``bootstrap()`` before it imports PySide6 networking or
anything from nostr/. That:

1. Points HOME (and XDG_*) at a fresh temporary directory. Every file the
   app keeps (~/.config/my_editor, ~/.cache/my_editor: profiles, keys,
   relay lists, drafts, media settings, known people) is placed with
   Path.home() or expanduser("~") when its module is imported, so the real
   user's files are never read or written.
2. Replaces every relay, Blossom server and web service MyEditor names by
   itself with a local one, while the module that names it is imported (an
   import hook), so default arguments and ``from x import NAME`` copies see
   the local values too. LOCAL_TOPOLOGY below says which relay plays which
   part.
3. Installs a guard: a WebSocket, a QNetworkAccessManager request or a
   Python socket aimed at anything but this computer is refused (sent to a
   closed local port instead), recorded, and fails the run. Opening a link
   in the browser is recorded and never done.
4. Answers the app's alerts (alerts.Alert) without showing them: the
   default button, or what a script's ``ALERT_POLICY`` says.

Results are printed and written as JSON to $NDAK_OUT (default: a folder in
the system temp directory) for the report.
"""

from __future__ import annotations

import atexit
import importlib.abc
import importlib.machinery
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# The local topology                                                          #
# --------------------------------------------------------------------------- #

RNOSTR = "ws://localhost:7777"      # starter list, fallback, NIP-50 search
NRS = "ws://localhost:7779"         # starter list, fallback, signer meeting relay
STRFRY = "ws://localhost:7780"      # the indexer (NIP-42 AUTH for kinds 4 and 1059)
OBELISK = "ws://localhost:7781"     # stands in for the members' relay; NIP-50; NostrHub
KHATRU = "ws://localhost:7782"      # other people's relays (Bob, Carol)
ALL_RELAYS = (RNOSTR, NRS, STRFRY, OBELISK, KHATRU)
RELAY_NAMES = {RNOSTR: "rnostr", NRS: "nostr-rs-relay", STRFRY: "strfry",
               OBELISK: "obelisk", KHATRU: "khatru"}

BLOSSOM_A = "http://localhost:7801"     # default server 1 (primary)
BLOSSOM_B = "http://localhost:7802"     # default server 2 (mirror)
BLOSSOM_M = "http://localhost:7803"     # stands in for the members' Blossom server
# Each run starts its own web server; runs at the same time need ports of
# their own, so NDAK_WEB_PORT moves it (the relays above are shared).
FAKE_WEB_PORT = int(os.environ.get("NDAK_WEB_PORT") or 7811)
FAKE_WEB = f"http://localhost:{FAKE_WEB_PORT}"   # roster, prices, feeds, nostr.json
SIDECAR_PORT = 7812
ASSOCIATION_PORT = 7813

LOCAL_TOPOLOGY = {
    "nostr.outbox.defaults": {
        "STARTER_LIST": ((RNOSTR, ""), (NRS, "")),
        "FALLBACK_RELAYS": (NRS, RNOSTR, STRFRY),
        "INDEXER_RELAYS": (STRFRY,),
        "SEARCH_RELAYS": (RNOSTR, OBELISK),
    },
    "nostr.bunker": {
        "NIP46_RELAYS": (NRS, RNOSTR),
    },
    "nostr.einundzwanzig": {
        "MEMBER_RELAY": OBELISK,
        "MEMBER_BLOSSOM": BLOSSOM_M,
        "_ROSTER_URL": FAKE_WEB + "/api/members/{year}",
        "JOIN_URL": FAKE_WEB + "/association/profile",
        "BENEFITS_URL": FAKE_WEB + "/association/benefits",
    },
    "nostr.einundzwanzig_api.client": {
        "BASE_URL": f"http://127.0.0.1:{ASSOCIATION_PORT}",
    },
    "nostr.einundzwanzig_api.prices": {
        "PRICES_URL": FAKE_WEB + "/api/v1/prices",
    },
    "constants": {
        "MEMBERSHIP_SERVICE_URL": "",
    },
    "nostr.imports.constants": {
        "NOSTRHUB_RELAYS": (OBELISK,),
    },
    "nostr.imports.resolvers.nostrhub": {
        "HUB_BASE_URL": FAKE_WEB,
    },
    "nostr.imports.sources.nostr": {
        "NJUMP": FAKE_WEB,
    },
    "nostr.imports.sources.bluesky": {
        "BSKY_API": FAKE_WEB + "/xrpc/",
    },
    "nostr.blossom.servers": {
        "DEFAULT_BLOSSOM_SERVERS": (BLOSSOM_A, BLOSSOM_B),
        "RECOMMENDED_ADDON_SERVERS": (BLOSSOM_M,),
    },
}

# Extra patches a script adds before bootstrap() (module -> {name: value}).
EXTRA_PATCHES: Dict[str, Dict[str, Any]] = {}


def own_instance_socket(name: str) -> None:
    """Give this run's main window a single-instance socket of its own, so
    it never hands files to (or takes over the socket of) a MyEditor open on
    this computer. Call it before bootstrap().

    Builds with ``constants.IPC_SERVER_NAME`` read the name from
    MYEDITOR_IPC_NAME when ``constants`` loads (child processes inherit it);
    older builds kept their own copy in ``main_window._IPC_SERVER_NAME``.
    """
    os.environ["MYEDITOR_IPC_NAME"] = name
    if "IPC_SERVER_NAME" not in (ROOT / "constants.py").read_text(encoding="utf-8"):
        EXTRA_PATCHES.setdefault("main_window", {})["_IPC_SERVER_NAME"] = name


def _rebuild_member_benefits(module) -> None:
    # MEMBER_BENEFITS is built from MEMBER_RELAY and MEMBER_BLOSSOM while the
    # module runs, so it is built again from the local values.
    module.MEMBER_BENEFITS = module.Benefits(relay=module.MEMBER_RELAY,
                                             blossom_server=module.MEMBER_BLOSSOM,
                                             per_user_bytes=module.PER_USER_BYTES)


def _allow_the_test_bed_host(module) -> None:
    """HARNESS-ONLY: let images and mirror sources named by other people be
    on localhost, where every server of this test bed runs.

    url_safety.is_local_name refuses names that point into the person's
    own network (localhost, router, nas.local) for addresses a stranger's
    event names, by design. The test bed's avatars, feed images and media
    servers are all on localhost, so they can only be fetched with this
    patch. Private IP literals stay refused."""
    real = getattr(module, "is_local_name", None)
    if real is None:
        return      # an app from before the rule

    def is_local_name(host: str) -> bool:
        return False if is_local_host(host) else real(host)

    module.is_local_name = is_local_name


def _allow_the_test_bed_addresses(module) -> None:
    """HARNESS-ONLY: let the importer's network guard fetch from this
    computer, where the test bed's web server (feeds, pages, nostr.json)
    runs.

    nostr/imports/netguard.py refuses every address that is not on the
    public internet, by design, and localhost resolves to a loopback
    address. Only loopback is let through; private network addresses stay
    refused, so the guard's rule is still exercised."""
    real = getattr(module, "is_public_address", None)
    if real is None:
        return      # an app from before the guard

    def is_public_address(address: str) -> bool:
        try:
            if ipaddress.ip_address((address or "").split("%", 1)[0]).is_loopback:
                return True
        except ValueError:
            pass
        return real(address)

    module.is_public_address = is_public_address


# Callables run right after a module is imported: module name -> [fn(module)].
POST_IMPORT_HOOKS: Dict[str, List[Callable[[Any], None]]] = {
    "nostr.einundzwanzig": [_rebuild_member_benefits],
    "url_safety": [_allow_the_test_bed_host],
    "nostr.imports.netguard": [_allow_the_test_bed_addresses],
}

HOME: Optional[Path] = None
OUT_DIR: Optional[Path] = None
VIOLATIONS: List[str] = []
OPENED_LINKS: List[str] = []
ALERTS_SEEN: List[dict] = []
SLOT_ERRORS: List[str] = []         # exceptions raised inside Qt slots
_PATCHED_MODULES: List[str] = []


# --------------------------------------------------------------------------- #
# Import hook: local values as each module is imported                        #
# --------------------------------------------------------------------------- #

class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, inner, name: str) -> None:
        self._inner = inner
        self._name = name

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module) -> None:
        self._inner.exec_module(module)
        values = dict(LOCAL_TOPOLOGY.get(self._name, {}))
        values.update(EXTRA_PATCHES.get(self._name, {}))
        replaced = {}
        for attr, value in values.items():
            if not hasattr(module, attr):
                raise RuntimeError(f"ndak bootstrap: {self._name}.{attr} does not exist "
                                   "any more; update LOCAL_TOPOLOGY")
            replaced[id(getattr(module, attr))] = value
            setattr(module, attr, value)
        _replace_defaults(module, replaced)
        for hook in POST_IMPORT_HOOKS.get(self._name, ()):
            hook(module)
        _PATCHED_MODULES.append(self._name)


def _replace_defaults(module, replaced: Dict[int, Any]) -> None:
    """Default arguments that captured a replaced value while the module ran
    (``def f(url=PRICES_URL)``) get the local value too."""
    import inspect
    if not replaced:
        return
    functions = []
    for value in vars(module).values():
        if inspect.isfunction(value) and value.__module__ == module.__name__:
            functions.append(value)
        elif inspect.isclass(value) and value.__module__ == module.__name__:
            functions += [f for f in vars(value).values() if inspect.isfunction(f)]
    for fn in functions:
        if fn.__defaults__:
            fn.__defaults__ = tuple(replaced.get(id(d), d) for d in fn.__defaults__)
        if fn.__kwdefaults__:
            fn.__kwdefaults__ = {k: replaced.get(id(d), d) for k, d in fn.__kwdefaults__.items()}


class _PatchingFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in LOCAL_TOPOLOGY and fullname not in EXTRA_PATCHES \
                and fullname not in POST_IMPORT_HOOKS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader, fullname)
        return spec


# --------------------------------------------------------------------------- #
# The guard: nothing leaves this computer                                     #
# --------------------------------------------------------------------------- #

def is_local_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _violation(what: str) -> None:
    VIOLATIONS.append(what)
    sys.stderr.write(f"\n!!! NDAK GUARD: refused a connection off this computer: {what}\n")
    sys.stderr.write("".join(traceback.format_stack(limit=12)))


def _install_python_socket_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check(address) -> bool:
        if isinstance(address, tuple) and address:
            return is_local_host(str(address[0]))
        return True             # AF_UNIX paths

    def connect(self, address):
        if not _check(address):
            _violation(f"python socket connect {address!r}")
            raise ConnectionRefusedError("blocked by the ndak guard")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _check(address):
            _violation(f"python socket connect_ex {address!r}")
            return 111
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not is_local_host(host if isinstance(host, str)
                                                  else host.decode()):
            _violation(f"python DNS lookup {host!r}")
            raise socket.gaierror(socket.EAI_NONAME, "blocked by the ndak guard")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo


def _install_qt_guard() -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
    from PySide6.QtWebSockets import QWebSocket

    real_open = QWebSocket.open

    def ws_open(self, target, *rest):
        url = target.url() if isinstance(target, QNetworkRequest) else QUrl(target)
        if not is_local_host(url.host()):
            _violation(f"websocket {url.toString()}")
            return real_open(self, QUrl("ws://127.0.0.1:9/blocked-by-ndak-guard"), *rest)
        return real_open(self, target, *rest)

    QWebSocket.open = ws_open

    def wrap(name: str) -> None:
        real = getattr(QNetworkAccessManager, name)

        def guarded(self, request, *rest):
            url = request.url()
            if not is_local_host(url.host()):
                _violation(f"http {name} {url.toString()}")
                blocked = QNetworkRequest(request)
                blocked.setUrl(QUrl("http://127.0.0.1:9/blocked-by-ndak-guard"))
                return real(self, blocked, *rest)
            return real(self, request, *rest)

        setattr(QNetworkAccessManager, name, guarded)

    for method in ("get", "post", "put", "head", "deleteResource", "sendCustomRequest"):
        wrap(method)

    def open_url(url) -> bool:
        OPENED_LINKS.append(QUrl(url).toString())
        return True

    QDesktopServices.openUrl = staticmethod(open_url)
    import webbrowser
    webbrowser.open = lambda url, *a, **k: OPENED_LINKS.append(url) or True


# --------------------------------------------------------------------------- #
# Alerts answered without a person                                            #
# --------------------------------------------------------------------------- #

def default_alert_policy(alert: dict) -> Any:
    """The default button's value (Return), as a person in a hurry would."""
    import alerts
    for button in alert["buttons"]:
        if button.role == alerts.DEFAULT:
            return button.value
    return alert["buttons"][0].value if alert["buttons"] else None


ALERT_POLICY: Callable[[dict], Any] = default_alert_policy


def _patch_alerts(module) -> None:
    real_init = module.Alert.__init__

    def init(self, parent=None, *, title="", message="", buttons=(), **kwargs):
        real_init(self, parent, title=title, message=message, buttons=buttons, **kwargs)
        self._ndak = {"title": title, "message": message, "buttons": list(buttons)}

    def run(self):
        record = dict(self._ndak)
        value = ALERT_POLICY(record)
        record["answer"] = value
        record["buttons"] = [b.label for b in record["buttons"]]
        ALERTS_SEEN.append(record)
        print(f"    [alert] {record['title']!r} -> {value!r}")
        return value

    module.Alert.__init__ = init
    module.Alert.run = run


# --------------------------------------------------------------------------- #
# bootstrap                                                                   #
# --------------------------------------------------------------------------- #

def bootstrap(name: str) -> Path:
    """Isolate HOME, localize every service, install the guard. Call first."""
    global HOME, OUT_DIR
    already = [m for m in sys.modules if m == "nostr" or m.startswith("nostr.")
               or m in ("constants", "alerts", "main_window")]
    if already:
        raise RuntimeError(f"bootstrap() must run before importing {already}")
    HOME = Path(tempfile.mkdtemp(prefix=f"ndak-home-{name}-"))
    os.environ["HOME"] = str(HOME)
    for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache"),
                     ("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state")):
        os.environ[var] = str(HOME / sub)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.pop("MYEDITOR_MEMBERSHIP_SERVICE", None)
    os.environ.pop("MYEDITOR_MEMBERSHIP_UPSTREAM", None)
    assert Path.home() == HOME, "Path.home() does not follow HOME"
    OUT_DIR = Path(os.environ.get("NDAK_OUT") or Path(tempfile.gettempdir()) / "ndak-results")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    POST_IMPORT_HOOKS.setdefault("alerts", []).append(_patch_alerts)
    sys.meta_path.insert(0, _PatchingFinder())

    def excepthook(kind, value, tb):
        text = "".join(traceback.format_exception(kind, value, tb))
        SLOT_ERRORS.append(text)
        sys.stderr.write(f"\n!!! uncaught exception (inside a Qt slot?):\n{text}")

    sys.excepthook = excepthook
    _install_python_socket_guard()
    _install_qt_guard()
    if not os.environ.get("NDAK_KEEP_HOME"):
        atexit.register(shutil.rmtree, HOME, True)
    print(f"[ndak] HOME={HOME}")
    return HOME


def audit_remote_names() -> List[str]:
    """Remote hosts still named in loaded modules (strings, tuples, defaults).

    Links the app only shows are harmless; anything here the guard would
    stop if it were used. Reported, not failed."""
    import inspect
    import re
    pattern = re.compile(r"^(wss?|https?)://([^/:?#]+)")
    found = []

    def remote(item) -> Optional[str]:
        if isinstance(item, tuple) and item:
            item = item[0]
        if isinstance(item, str):
            match = pattern.match(item)
            if match and not is_local_host(match.group(2)) and "{" not in match.group(2):
                return item
        return None

    def defaults_of(fn) -> list:
        out = list(getattr(fn, "__defaults__", None) or ())
        out += list((getattr(fn, "__kwdefaults__", None) or {}).values())
        return out

    for mod_name, module in sorted(sys.modules.items()):
        if module is None or not (mod_name.startswith("nostr") or mod_name in ("constants",)):
            continue
        for attr, value in vars(module).items():
            for item in (value if isinstance(value, (tuple, list)) else (value,)):
                if remote(item):
                    found.append(f"{mod_name}.{attr} = {remote(item)}")
            functions = [(attr, value)] if callable(value) else []
            if inspect.isclass(value) and getattr(value, "__module__", "") == mod_name:
                functions += [(f"{attr}.{n}", f) for n, f in vars(value).items() if callable(f)]
            for fn_name, fn in functions:
                for default in defaults_of(fn):
                    for item in (default if isinstance(default, (tuple, list)) else (default,)):
                        if remote(item):
                            found.append(f"{mod_name}.{fn_name}() default = {remote(item)}")
    return sorted(set(found))


# --------------------------------------------------------------------------- #
# Qt helpers                                                                  #
# --------------------------------------------------------------------------- #

def qt_app():
    """The application, set up as main.py sets it up, so the flows draw as
    the app does: its style, and every widget given a style sheet polished
    at once (theme.install_style_sheet_guard)."""
    from PySide6.QtWidgets import QApplication
    import theme
    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0]])
        app.setStyle(theme.AppStyle("Fusion"))
    theme.install_style_sheet_guard(app)
    return app


def spin(ms: int) -> None:
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(predicate: Callable[[], bool], timeout_ms: int = 15_000, step_ms: int = 25) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return True
        spin(step_ms)
    return bool(predicate())


class Catch:
    """Records every emission of the signals it is connected to."""

    def __init__(self, *signals) -> None:
        self.calls: List[tuple] = []
        for signal in signals:
            signal.connect(lambda *args: self.calls.append(args))

    def __call__(self, *args) -> None:
        self.calls.append(args)

    @property
    def fired(self) -> bool:
        return bool(self.calls)

    def wait(self, timeout_ms: int = 15_000) -> bool:
        return wait_until(lambda: self.fired, timeout_ms)

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


# --------------------------------------------------------------------------- #
# nak                                                                         #
# --------------------------------------------------------------------------- #

def nak(*args: str, stdin: Optional[str] = None, timeout: float = 30,
        env_home: Optional[Path] = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_home is not None:
        env["HOME"] = str(env_home)
    # nak reads events and filters from a piped stdin, so without input it
    # must get none: an inherited open pipe (an agent's shell, a CI step)
    # made every call wait until it timed out.
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(["nak", *args], capture_output=True, text=True,
                          timeout=timeout, env=env, **feed)


def nak_req(relays: Sequence[str], *, kinds: Iterable[int] = (), authors: Iterable[str] = (),
            ids: Iterable[str] = (), tags: Optional[Dict[str, Iterable[str]]] = None,
            limit: Optional[int] = None, search: Optional[str] = None,
            timeout: float = 20) -> List[dict]:
    args = ["req"]
    for k in kinds:
        args += ["-k", str(k)]
    for a in authors:
        args += ["-a", a]
    for i in ids:
        args += ["-i", i]
    for name, values in (tags or {}).items():
        for value in values:
            args += ["-t", f"{name}={value}"]
    if limit is not None:
        args += ["-l", str(limit)]
    if search is not None:
        args += ["--search", search]
    args += list(relays)
    result = None
    for attempt in range(3):
        try:
            result = nak(*args, timeout=timeout)
            break
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"  (nak req timed out, attempt {attempt + 1}: {' '.join(args[:12])})\n")
    if result is None:
        return []
    out = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def presence(event_id: str, relays: Sequence[str] = ALL_RELAYS) -> Dict[str, bool]:
    """Which relays return the event with this id (one REQ per relay)."""
    return {relay: any(e.get("id") == event_id for e in nak_req([relay], ids=[event_id]))
            for relay in relays}


def nak_verify(event: dict) -> bool:
    result = nak("verify", stdin=json.dumps(event))
    return result.returncode == 0


def nak_publish(sec_hex: str, relays: Sequence[str], *, kind: int, content: str = "",
                tags: Sequence[Sequence[str]] = (), created_at: Optional[int] = None) -> dict:
    """Sign and publish an event as someone else (a test identity) with nak."""
    args = ["event", "--sec", sec_hex, "-k", str(kind), "-c", content]
    for tag in tags:
        args += ["-t", f"{tag[0]}=" + ";".join(tag[1:])]
    if created_at is not None:
        args += ["--ts", str(created_at)]
    args += list(relays)
    result = nak(*args)
    for line in result.stdout.splitlines() + result.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"nak event failed: {result.stderr[-400:]}")


class NakBunker:
    """``nak bunker`` as a NIP-46 remote signer on local relays, with its
    own HOME so nothing of the real user's nak setup is read or written."""

    def __init__(self, sec_hex: str, relays: Sequence[str], *, secret: str = "",
                 authorized_keys: Sequence[str] = (), profile: str = "") -> None:
        import threading
        self.home = Path(tempfile.mkdtemp(prefix="ndak-nak-"))
        args = ["nak", "bunker", "--sec", sec_hex]
        if secret:
            args += ["-s", secret]
        for key in authorized_keys:
            args += ["-k", key]
        if profile:
            args += ["--persist", "--profile", profile]
        args += list(relays)
        self.lines: List[str] = []
        env = dict(os.environ, HOME=str(self.home))
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, env=env)
        threading.Thread(target=self._read, daemon=True).start()
        self.profile = profile

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def uri(self, timeout_s: float = 15) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if "bunker://" in line:
                    return line[line.index("bunker://"):].split()[0]
            time.sleep(0.1)
        raise RuntimeError("nak bunker printed no bunker:// URI:\n" + "\n".join(self.lines))

    def connect(self, nostrconnect_uri: str) -> subprocess.CompletedProcess:
        """``nak bunker connect`` hands a nostrconnect:// URI to this bunker."""
        args = ["bunker", "connect"]
        if self.profile:
            args += ["--profile", self.profile]
        args.append(nostrconnect_uri)
        return nak(*args, timeout=30, env_home=self.home)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.home, ignore_errors=True)


def new_identity():
    """(secret key bytes, secret hex, pubkey hex) of a fresh test key."""
    from nostr import crypto
    sk = crypto.generate_secret_key()
    return sk, sk.hex(), crypto.get_public_key(sk).hex()


# --------------------------------------------------------------------------- #
# Event review                                                                #
# --------------------------------------------------------------------------- #

def review_event(rep: "Report", label: str, event: dict, *, kind: int,
                 max_age_s: int = 900) -> bool:
    """NIP-01 checks every event gets: kind, id and signature (by nak and by
    the app's own verifier), created_at sanity, tags well formed."""
    from nostr import events as nostr_events
    ok = True
    problems = []
    if event.get("kind") != kind:
        problems.append(f"kind {event.get('kind')} != {kind}")
    if not nak_verify(event):
        problems.append("nak verify failed")
    if not nostr_events.verify_event(event):
        problems.append("events.verify_event failed")
    created = event.get("created_at")
    now = int(time.time())
    if not isinstance(created, int) or abs(now - created) > max_age_s:
        problems.append(f"created_at {created} is {now - (created or 0)} s from now")
    for tag in event.get("tags", []):
        if not isinstance(tag, list) or not tag:
            problems.append(f"empty tag {tag!r}")
        elif any(not isinstance(x, str) for x in tag):
            problems.append(f"non-string tag value {tag!r}")
        elif len(tag) >= 2 and tag[1] == "" and tag[0] not in ("d",):
            problems.append(f"tag with empty value {tag!r}")
    names = [t[0] for t in event.get("tags", []) if isinstance(t, list) and t]
    for single in ("d", "client", "title", "summary", "image", "published_at", "expiration", "k"):
        if names.count(single) > 1:
            problems.append(f"tag {single!r} appears {names.count(single)} times")
    if problems:
        ok = False
    rep.check(f"{label}: NIP-01 shape, id and signature", ok, "; ".join(problems) or
              f"id {event.get('id', '')[:16]}, {len(event.get('tags', []))} tags")
    return ok


def tag_values(event: dict, name: str) -> List[str]:
    return [t[1] for t in event.get("tags", []) if isinstance(t, list) and len(t) >= 2
            and t[0] == name]


def first_tag(event: dict, name: str) -> Optional[List[str]]:
    for t in event.get("tags", []):
        if isinstance(t, list) and t and t[0] == name:
            return t
    return None


# --------------------------------------------------------------------------- #
# The app's Nostr stack, built the way main_window.py builds it               #
# --------------------------------------------------------------------------- #

class Stack:
    """RelayPool, ProfileStore, RelayDirectory, KeyVault and BunkerSessionPool,
    wired as MainWindow.__init__ wires them. ``device`` gives a second (or
    third) device its own files under the temporary HOME, as another
    computer with the same account would have."""

    def __init__(self, device: str = "") -> None:
        from nostr import key_vault
        from nostr.bunker import BunkerSessionPool
        from nostr.key_vault import KeyVault
        from nostr.outbox.directory import RelayDirectory, relay_lists_file
        from nostr.profiles import ProfileStore, profiles_file
        from nostr.relay import RelayPool
        base = HOME / f"device-{device}" if device else None
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        self.base = base or (HOME / ".config" / "my_editor")
        self.pool = RelayPool()
        self.store = ProfileStore(base / "nostr_profiles.json" if base else profiles_file())
        self.directory = RelayDirectory(
            self.pool, store_path=(base / "nostr_relay_lists.json") if base else relay_lists_file(),
            own_pubkeys=lambda: [p.user_pubkey for p in self.store.list()])
        self.vault = KeyVault(base / "nostr_keys.json" if base else key_vault.keys_file())
        self.sessions = BunkerSessionPool(self.pool, vault=self.vault)

    def signer_for(self, profile, timeout_ms: int = 20_000):
        """The signer the session pool hands out for ``profile`` (or raises)."""
        box = {}
        self.sessions.get(profile, lambda client: box.update(ok=client),
                          lambda reason: box.update(err=reason))
        if not wait_until(lambda: box, timeout_ms):
            raise RuntimeError("the session pool never answered")
        if "err" in box:
            raise RuntimeError(f"no signer: {box['err']}")
        return box["ok"]

    def sign(self, profile, unsigned: dict) -> dict:
        client = self.signer_for(profile)
        box = {}
        client.sign_event(unsigned, lambda ev: box.update(ok=ev),
                          lambda reason: box.update(err=reason))
        if not wait_until(lambda: box, 30_000) or "err" in box:
            raise RuntimeError(f"signing failed: {box.get('err', 'timeout')}")
        return box["ok"]

    def publish_raw(self, relays: Sequence[str], event: dict, timeout_ms: int = 10_000) -> list:
        job = self.pool.publish(list(relays), event, timeout_ms=timeout_ms)
        done = Catch(job.all_done)
        done.wait(timeout_ms + 2_000)
        return done.last[0] if done.fired else []

    def lookup(self, pubkey: str, fresh: bool = True):
        box = {}
        self.directory.lookup(pubkey, lambda rl: box.update(rl=rl), fresh=fresh)
        wait_until(lambda: box, 15_000)
        return box.get("rl")

    def close(self) -> None:
        self.sessions.close_all()
        self.pool.close_all()
        spin(100)


def local_account(stack: Stack, name: str = "", *, sk: Optional[bytes] = None,
                  setup: bool = True, relays: Sequence[str] = ()):
    """A key kept on this computer, saved the way CreateAccountWindow saves
    one, made the active account and (``setup``) published with
    AccountSetup: relay list, then profile. Returns (profile, sk, outcome)."""
    from nostr import crypto
    from nostr.outbox import writer as outbox_writer
    from nostr.ui.account_windows import local_profile
    sk = sk or crypto.generate_secret_key()
    pubkey = stack.vault.store(sk)
    profile = local_profile(pubkey, relays=list(relays) or None)
    profile.display_name = name
    profile.setup_pending = setup
    stack.store.upsert(profile)
    stack.store.set_default(pubkey)
    outcome: dict = {}
    if setup:
        account_setup = outbox_writer.AccountSetup(
            name=name, deps={"pool": stack.pool, "directory": stack.directory,
                             "session_pool": stack.sessions, "profile": profile}, fresh=True)
        steps = []
        account_setup.step.connect(lambda key, state, detail: steps.append((key, state, detail)))
        account_setup.finished.connect(lambda ok, msg: outcome.update(ok=ok, message=msg))
        account_setup.start()
        wait_until(lambda: "ok" in outcome, 30_000)
        outcome["steps"] = steps
        outcome["setup"] = account_setup
    return profile, sk, outcome


def allow_local_relays_of_others():
    """HARNESS-ONLY: let relays other people name be ws://localhost.

    policy.is_public_relay refuses ws:// and localhost relays from other
    people's relay lists and hints, by design, so a stranger's event cannot
    make the app connect into the user's own network. Every relay in this
    test bed is ws://localhost, so routing to other people's relays can
    only be observed with this patch. Returns a function that undoes it."""
    from urllib.parse import urlsplit

    from nostr.outbox import policy
    real = policy.is_public_relay

    def is_public_relay(url) -> bool:
        if real(url):
            return True
        normalized = policy.normalize_relay_url(url)
        return normalized is not None and is_local_host(urlsplit(normalized).hostname or "")

    policy.is_public_relay = is_public_relay

    def undo() -> None:
        policy.is_public_relay = real
    return undo


# --------------------------------------------------------------------------- #
# Results                                                                     #
# --------------------------------------------------------------------------- #

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "NOT TESTABLE", "INFO"


class Report:
    """What one script checked, the events it produced, and where they went."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.checks: List[dict] = []
        self.events: List[dict] = []
        self.notes: List[str] = []
        self.started = time.time()

    def check(self, what: str, ok: bool, detail: str = "") -> bool:
        status = PASS if ok else FAIL
        self.checks.append({"check": what, "status": status, "detail": detail})
        print(f"  [{status}] {what}" + (f": {detail}" if detail else ""))
        return ok

    def skip(self, what: str, why: str) -> None:
        self.checks.append({"check": what, "status": SKIP, "detail": why})
        print(f"  [{SKIP}] {what}: {why}")

    def info(self, what: str, detail: str = "") -> None:
        self.checks.append({"check": what, "status": INFO, "detail": detail})
        print(f"  [{INFO}] {what}" + (f": {detail}" if detail else ""))

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  note: {text}")

    def section(self, title: str) -> None:
        print(f"\n== {title}")

    def event(self, label: str, event: dict, *, claimed: Sequence[str] = (),
              expected: Sequence[str] = (), present: Optional[Dict[str, bool]] = None) -> None:
        """Record a published event with where the app said it went, where it
        should have gone, and where nak finds it."""
        self.events.append({
            "label": label, "id": event.get("id"), "kind": event.get("kind"),
            "pubkey": event.get("pubkey"), "created_at": event.get("created_at"),
            "tags": event.get("tags"), "content_len": len(event.get("content") or ""),
            "claimed": list(claimed), "expected": list(expected),
            "present": {RELAY_NAMES.get(k, k): v for k, v in (present or {}).items()},
        })

    def finish(self) -> int:
        # Said as checks, so they print the lines the runners count.
        self.check("nothing contacted a host off this computer", not VIOLATIONS,
                   "; ".join(VIOLATIONS[:10]) if VIOLATIONS else "guard saw no attempt")
        self.check("no exception escaped into Qt", not SLOT_ERRORS,
                   " | ".join(e.strip().splitlines()[-1] for e in SLOT_ERRORS[:5])
                   if SLOT_ERRORS else "no slot raised")
        failed = [c for c in self.checks if c["status"] == FAIL]
        remote = audit_remote_names()
        data = {"name": self.name, "seconds": round(time.time() - self.started, 1),
                "checks": self.checks, "events": self.events, "notes": self.notes,
                "violations": VIOLATIONS, "opened_links": OPENED_LINKS,
                "alerts": ALERTS_SEEN, "remote_names_left": remote,
                "slot_errors": SLOT_ERRORS,
                "patched_modules": _PATCHED_MODULES}
        path = OUT_DIR / f"{self.name}.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        counts = {}
        for c in self.checks:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        print(f"\n--- {self.name}: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
              + f" (results in {path})")
        for c in failed:
            print(f"    FAIL {c['check']}: {c['detail']}")
        return 1 if failed else 0


# --------------------------------------------------------------------------- #
# A small local web server (roster, prices, feeds, nostr.json)                #
# --------------------------------------------------------------------------- #

class FakeWeb:
    """Serves files from a folder on FAKE_WEB_PORT, in a child process, and
    logs every request so a script can see what the app asked for."""

    def __init__(self, port: int = FAKE_WEB_PORT) -> None:
        self.port = port
        self.root = Path(tempfile.mkdtemp(prefix="ndak-web-"))
        self.proc: Optional[subprocess.Popen] = None

    def put(self, path: str, body, content_type: str = "") -> None:
        target = self.root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        target.write_bytes(data)
        if content_type:
            (target.parent / (target.name + ".ctype")).write_text(content_type, encoding="utf-8")

    def requests(self) -> List[str]:
        log = self.root / "_requests.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def start(self) -> "FakeWeb":
        _require_free_port(self.port)
        self.proc = subprocess.Popen([sys.executable, __file__, "--serve", str(self.port),
                                      str(self.root), str(os.getpid())])
        atexit.register(self.stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("the fake web server did not start")

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        shutil.rmtree(self.root, ignore_errors=True)


def _require_free_port(port: int) -> None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return
    raise RuntimeError(f"port {port} is already in use (a server left over from an "
                       "interrupted run?); stop it first")


def _exit_with_parent(parent_pid: int) -> None:
    """A child server ends when the script that started it is gone, even
    when that one crashed and never ran its cleanup."""
    import threading

    def watch() -> None:
        while True:
            try:
                os.kill(parent_pid, 0)
            except OSError:
                os._exit(0)
            time.sleep(1)

    threading.Thread(target=watch, daemon=True).start()


def _serve(port: int, root: str) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    types = {".json": "application/json", ".xml": "application/rss+xml",
             ".rss": "application/rss+xml", ".atom": "application/atom+xml",
             ".html": "text/html; charset=utf-8", ".png": "image/png",
             ".jpg": "image/jpeg", ".txt": "text/plain"}

    class Handler(BaseHTTPRequestHandler):
        def _log(self):
            with open(os.path.join(root, "_requests.log"), "a", encoding="utf-8") as log:
                log.write(f"{self.command} {self.path}\n")

        def do_GET(self):
            self._log()
            path = self.path.split("?", 1)[0].lstrip("/") or "index.html"
            target = os.path.realpath(os.path.join(root, path))
            if not target.startswith(os.path.realpath(root)) or not os.path.isfile(target):
                if os.path.isfile(os.path.join(target, "index.html")):
                    target = os.path.join(target, "index.html")
                else:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"not found"}')
                    return
            data = open(target, "rb").read()
            ctype_file = target + ".ctype"
            if os.path.isfile(ctype_file):
                ctype = open(ctype_file, encoding="utf-8").read().strip()
            else:
                ctype = types.get(os.path.splitext(target)[1], "application/json")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_HEAD(self):
            self._log()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


# --------------------------------------------------------------------------- #
# Blossom servers: hzrd149/blossom-server in Docker, and a small stand-in     #
# --------------------------------------------------------------------------- #

BLOSSOM_IMAGE = "ghcr.io/hzrd149/blossom-server:master"
_BLOSSOM_CONFIG = """host: 0.0.0.0
port: 3000
database:
  path: data/sqlite.db
storage:
  backend: local
  local:
    dir: ./data/blobs
  rules:
    - type: "*"
      expiration: "1 month"
upload:
  enabled: true
  requireAuth: true
  requirePubkeyInRule: false
mirror:
  enabled: true
  requireAuth: true
delete:
  requireAuth: true
list:
  enabled: true
  requireAuth: false
  allowListOthers: true
landing:
  enabled: false
dashboard:
  enabled: false
report:
  enabled: false
"""


class DockerBlossom:
    """hzrd149/blossom-server (BUD-01/02/04/06/11) in a throwaway container
    bound to 127.0.0.1. Its BUD-04 mirror refuses loopback and private
    sources (an SSRF guard with no switch), so it cannot mirror from
    another local server."""

    def __init__(self, port: int, name: str) -> None:
        self.port, self.name = port, f"ndak-myeditor-blossom-{name}"
        self.dir = Path(tempfile.mkdtemp(prefix=f"ndak-blossom-{name}-"))
        (self.dir / "config.yml").write_text(_BLOSSOM_CONFIG, encoding="utf-8")

    def start(self) -> "DockerBlossom":
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        result = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name, "-p", f"127.0.0.1:{self.port}:3000",
             "-v", f"{self.dir / 'config.yml'}:/app/config.yml:ro", BLOSSOM_IMAGE],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"docker run failed: {result.stderr[-400:]}")
        _wait_http(f"http://127.0.0.1:{self.port}/list/{'0' * 64}", 60)
        return self

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)


class StandInBlossom:
    """A small BUD-01/02/04/11 server in a child process that, unlike the
    real one, mirrors from localhost. Every request is logged with its
    decoded kind 24242 auth event and the verdict, for the event review."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.dir = Path(tempfile.mkdtemp(prefix="ndak-standin-blossom-"))
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> "StandInBlossom":
        _require_free_port(self.port)
        self.proc = subprocess.Popen([sys.executable, __file__, "--blossom", str(self.port),
                                      str(self.dir), str(os.getpid())])
        atexit.register(self.stop)
        _wait_http(f"http://127.0.0.1:{self.port}/list/{'0' * 64}", 15)
        return self

    def log(self) -> List[dict]:
        path = self.dir / "requests.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        shutil.rmtree(self.dir, ignore_errors=True)


def _wait_http(url: str, timeout_s: float) -> None:
    import urllib.request
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not answer")


def http_status(url: str, method: str = "HEAD") -> int:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def _serve_blossom(port: int, root: str) -> None:
    import base64
    import hashlib
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    blobs = os.path.join(root, "blobs")
    os.makedirs(blobs, exist_ok=True)
    owners: Dict[str, dict] = {}        # sha -> {"type", "size", "uploaded", "owners": set}

    def log(entry: dict) -> None:
        with open(os.path.join(root, "requests.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def auth_event(header: str):
        if not header or not header.startswith("Nostr "):
            return None, "no Nostr authorization"
        token = header[6:].strip()
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            event = json.loads(raw)
        except Exception:  # noqa: BLE001
            try:
                event = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
            except Exception:  # noqa: BLE001
                return None, "authorization is not base64 JSON"
        return event, ""

    def check(event, verb: str, sha: Optional[str]) -> str:
        if event is None:
            return "missing auth"
        if event.get("kind") != 24242:
            return "kind is not 24242"
        verified = subprocess.run(["nak", "verify"], input=json.dumps(event), capture_output=True,
                                  text=True)
        if verified.returncode != 0:
            return "bad signature"
        now = int(time.time())
        tags = event.get("tags", [])
        values = lambda n: [t[1] for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == n]
        if event.get("created_at", 0) > now + 60:
            return "created_at in the future"
        exp = values("expiration")
        if not exp or int(exp[0]) <= now:
            return "expiration missing or past"
        if values("t") != [verb]:
            return f"t is {values('t')}, expected {verb}"
        if sha is not None and sha not in values("x"):
            return f"x does not name {sha}"
        servers = values("server")
        if servers and "localhost" not in servers:
            return f"server tag {servers} is not this server"
        return ""

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body, reason: str = "") -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if reason:
                self.send_header("X-Reason", reason)
            self.end_headers()
            self.wfile.write(data)

        def _descriptor(self, sha: str) -> dict:
            meta = owners[sha]
            ext = {"image/png": ".png", "image/jpeg": ".jpg"}.get(meta["type"], "")
            return {"url": f"http://localhost:{port}/{sha}{ext}", "sha256": sha,
                    "size": meta["size"], "type": meta["type"], "uploaded": meta["uploaded"]}

        def _store(self, data: bytes, mime: str, pubkey: str) -> str:
            sha = hashlib.sha256(data).hexdigest()
            with open(os.path.join(blobs, sha), "wb") as fh:
                fh.write(data)
            meta = owners.setdefault(sha, {"type": mime or "application/octet-stream",
                                           "size": len(data), "uploaded": int(time.time()),
                                           "owners": set()})
            meta["owners"].add(pubkey)
            return sha

        def _sha_from_path(self) -> str:
            name = self.path.split("?", 1)[0].lstrip("/")
            return name.split(".", 1)[0].lower()

        def do_HEAD(self):
            sha = self._sha_from_path()
            log({"method": "HEAD", "path": self.path})
            if sha == "upload":
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(200 if sha in owners else 404)
            if sha in owners:
                self.send_header("Content-Type", owners[sha]["type"])
                self.send_header("Content-Length", str(owners[sha]["size"]))
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/list/"):
                pubkey = path[len("/list/"):].lower()
                log({"method": "GET", "path": self.path,
                     "auth": auth_event(self.headers.get("Authorization", ""))[0]})
                self._json(200, [self._descriptor(s) for s, m in owners.items()
                                 if pubkey in m["owners"]])
                return
            sha = self._sha_from_path()
            log({"method": "GET", "path": self.path})
            if sha not in owners:
                self._json(404, {"error": "not found"}, "not found")
                return
            data = open(os.path.join(blobs, sha), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", owners[sha]["type"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            event, problem = auth_event(self.headers.get("Authorization", ""))
            entry = {"method": "PUT", "path": self.path, "auth": event,
                     "x_sha_256": self.headers.get("X-SHA-256"),
                     "content_type": self.headers.get("Content-Type")}
            if path == "/upload":
                sha = hashlib.sha256(body).hexdigest()
                verdict = problem or check(event, "upload", sha)
                entry["verdict"] = verdict or "ok"
                log(entry)
                if verdict:
                    self._json(401, {"error": verdict}, verdict)
                    return
                self._store(body, self.headers.get("Content-Type", ""), event["pubkey"])
                self._json(200, self._descriptor(sha))
                return
            if path == "/mirror":
                try:
                    source = json.loads(body)["url"]
                except Exception:  # noqa: BLE001
                    entry["verdict"] = "bad body"
                    log(entry)
                    self._json(400, {"error": "bad body"}, "bad body")
                    return
                entry["source"] = source
                from urllib.parse import urlsplit
                if (urlsplit(source).hostname or "") not in ("localhost", "127.0.0.1"):
                    entry["verdict"] = "refused: not a local source"
                    log(entry)
                    self._json(400, {"error": "only local sources"}, "only local sources")
                    return
                try:
                    with urllib.request.urlopen(source, timeout=10) as answer:
                        data = answer.read()
                        mime = answer.headers.get("Content-Type", "")
                except Exception as exc:  # noqa: BLE001
                    entry["verdict"] = f"fetch failed: {exc}"
                    log(entry)
                    self._json(502, {"error": "fetch failed"}, "fetch failed")
                    return
                sha = hashlib.sha256(data).hexdigest()
                verdict = problem or check(event, "upload", sha)
                entry["verdict"] = verdict or "ok"
                log(entry)
                if verdict:
                    self._json(401, {"error": verdict}, verdict)
                    return
                self._store(data, mime, event["pubkey"])
                self._json(200, self._descriptor(sha))
                return
            entry["verdict"] = "unknown endpoint"
            log(entry)
            self._json(404, {"error": "not found"})

        def do_DELETE(self):
            sha = self._sha_from_path()
            event, problem = auth_event(self.headers.get("Authorization", ""))
            verdict = problem or check(event, "delete", sha)
            log({"method": "DELETE", "path": self.path, "auth": event, "verdict": verdict or "ok"})
            if verdict:
                self._json(401, {"error": verdict}, verdict)
                return
            meta = owners.get(sha)
            if meta is None:
                self._json(404, {"error": "not found"}, "not found")
                return
            meta["owners"].discard(event["pubkey"])
            if not meta["owners"]:
                owners.pop(sha, None)
                try:
                    os.unlink(os.path.join(blobs, sha))
                except OSError:
                    pass
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] in ("--serve", "--blossom"):
    _exit_with_parent(int(sys.argv[4]))
    (_serve if sys.argv[1] == "--serve" else _serve_blossom)(int(sys.argv[2]), sys.argv[3])
