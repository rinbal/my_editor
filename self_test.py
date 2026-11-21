# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Starting the app just far enough to prove a build is whole.

``my-editor --self-test`` loads the app (which imports nearly every module)
and checks the pieces a frozen build can lose: translations and icons left out
of the bundle, a Qt module or the TLS backend missing, a library whose
native part did not come along. It prints one line per check and exits with
0 when every check passed, 1 otherwise. The release workflow runs it on each
build before packaging, so a broken bundle never becomes an installer.

A Windows build has no console, so the report also goes to the file named
by ``MYEDITOR_SELF_TEST_REPORT`` when that is set.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Callable, List, Tuple

import constants

Check = Tuple[str, Callable[[], str]]

# Systems that always have a spell checker of their own: there, a missing
# one means the build lost the code that reaches it.
_SPELLING_EXPECTED = ("darwin", "win32")


def _translations() -> str:
    import i18n
    found = i18n.available()
    if "de" not in found:
        raise RuntimeError(f"the German catalogue is missing (found {sorted(found)})")
    return ", ".join(sorted(found))


def _icon() -> str:
    # Not from main: importing main chooses the app's language.
    from resources import resource_path
    path = resource_path("packaging/icons/icon-256.png")
    if not os.path.isfile(path):
        raise RuntimeError(f"{path} is missing")
    return "packaging/icons/icon-256.png"


def _language_catalogue() -> str:
    """Exercise the actual offline resources, including in frozen builds."""
    import hashlib
    import json
    from pathlib import Path
    from resources import resource_path
    from nostr.content_language import normalize_language, search_languages
    catalogue = Path(resource_path("nostr/language_catalog.json"))
    contract = json.loads(Path(resource_path("nostr/multilingual_contract.json")).read_text(encoding="utf-8"))
    if hashlib.sha256(catalogue.read_bytes()).hexdigest() != contract["catalog_sha256"]:
        raise RuntimeError("the bundled language catalogue does not match its contract")
    for query, code in (("ไทย", "th"), ("ລາວ", "lo"), ("ខ្មែរ", "km"), ("日本語", "ja"), ("Hmong", "hmn")):
        if normalize_language(code) != code or search_languages(query, limit=1)[0]["code"] != code:
            raise RuntimeError(f"the bundled language catalogue cannot resolve {code}")
    return f'{len(json.loads(catalogue.read_text(encoding="utf-8"))["languages"])} offline languages'


def _secure_connections() -> str:
    # Relays, media servers and updates are all reached over TLS.
    from PySide6.QtNetwork import QSslSocket
    if not QSslSocket.supportsSsl():
        raise RuntimeError("Qt has no TLS backend")
    return QSslSocket.activeBackend() or "available"


def _keys_and_encryption() -> str:
    # The native secp256k1 code and NIP-44 encryption, with a fresh key.
    import hashlib

    from nostr import crypto
    secret = crypto.generate_secret_key()
    public = crypto.get_public_key(secret)
    digest = hashlib.sha256(b"self-test").digest()
    if not crypto.verify_schnorr(public, crypto.sign_schnorr(secret, digest), digest):
        raise RuntimeError("a signature did not verify")
    key = crypto.conversation_key(secret, public)
    if crypto.decrypt(crypto.encrypt("self-test", key), key) != "self-test":
        raise RuntimeError("NIP-44 did not round-trip")
    return "sign, verify, encrypt, decrypt"


# Imported lazily by the app, so a missing one shows only when used.
_LAZY_MODULES = (
    "feedparser", "markdownify", "readability",            # imports
    "openpyxl", "sheet_tab",                                # spreadsheets
    "segno",                                                # QR codes for pairing and payment
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "pdf_viewer",  # PDF
    "PySide6.QtTextToSpeech", "PySide6.QtWebSockets",
    "export_html", "export_pdf",
    # The file viewers: pictures, films and sound, reading views and
    # preview pictures of office documents, and Move to Trash.
    "media_tabs", "office_preview", "lxml.etree", "send2trash",
)
# Linux reaches the desktop's Open With and file manager over D-Bus; the
# app goes without them when the module is missing, so only this says so.
_LINUX_MODULES = ("PySide6.QtDBus",)


def _modules() -> str:
    names = _LAZY_MODULES + (_LINUX_MODULES if sys.platform.startswith("linux") else ())
    for name in names:
        importlib.import_module(name)
    return "PDF, spreadsheets, file viewers, web sockets, speech, exports, imports, QR codes"


def _spelling() -> str:
    from spelling import SpellChecker
    checker = SpellChecker()
    if checker.is_available():
        return "available"
    problem = checker.backend.problem or "unavailable"
    if sys.platform in _SPELLING_EXPECTED:
        raise RuntimeError(problem)
    return f"not offered by this system ({problem})"


def _editor() -> str:
    # The text stack end to end: an editor, Markdown in and out. Building
    # the whole main window would restore the person's session and start
    # the single-instance server, which a self-test must not do.
    from editor import HtmlEditor
    from markdown_writer import READ_FEATURES, document_to_markdown
    editor = HtmlEditor()
    try:
        editor.document().setMarkdown("# Title\n\nSome **bold** text.\n", READ_FEATURES)
        written = document_to_markdown(editor.document(), lambda _fmt: None)
    finally:
        editor.deleteLater()
    if "# Title" not in written or "**bold**" not in written:
        raise RuntimeError(f"Markdown did not round-trip: {written!r}")
    return "Markdown in and out"


def _multimedia() -> str:
    # Importing the module alone does not load its backend plugin. This
    # check catches a bundle missing the plugin or its FFmpeg libraries.
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    player = QMediaPlayer()
    video = QVideoWidget()
    try:
        if not player.isAvailable():
            raise RuntimeError("the local media playback backend is unavailable")
        player.setVideoOutput(video)
        player.setVideoOutput(None)
    finally:
        player.deleteLater()
        video.deleteLater()
    return "local playback backend and video widget"


def _dictation() -> str:
    # Dictation on this computer (dictation/on_device.py) on Windows and
    # Linux: its engine, imported only when someone dictates.
    if sys.platform == "darwin":
        return "the system's own"
    import sherpa_onnx
    from dictation import on_device  # noqa: F401
    if not hasattr(sherpa_onnx, "OfflineRecognizer"):
        raise RuntimeError("the speech engine is incomplete")
    return "on this computer"


CHECKS: List[Check] = [
    ("translations", _translations),
    ("article languages", _language_catalogue),
    ("icon", _icon),
    ("secure connections", _secure_connections),
    ("keys and encryption", _keys_and_encryption),
    ("modules", _modules),
    ("spelling", _spelling),
    ("editor", _editor),
    ("media playback", _multimedia),
    ("dictation", _dictation),
]


def run(checks: List[Check] = CHECKS) -> int:
    """Run every check and report; 0 when all passed."""
    lines = [f"MyEditor {constants.APP_VERSION} self-test ({sys.platform})"]
    failed = 0
    for name, check in checks:
        try:
            lines.append(f"ok      {name}: {check()}")
        except Exception as exc:     # a self-test reports every failure, of any kind
            failed += 1
            lines.append(f"FAILED  {name}: {type(exc).__name__}: {exc}")
    lines.append("all checks passed" if not failed else f"{failed} check(s) failed")
    _report("\n".join(lines) + "\n")
    return 0 if not failed else 1


def _report(text: str) -> None:
    if sys.stdout is not None:
        sys.stdout.write(text)
        sys.stdout.flush()
    target = os.environ.get("MYEDITOR_SELF_TEST_REPORT")
    if target:
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
