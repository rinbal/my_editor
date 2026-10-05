# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create a Nostr account, restore one from its backup, or back one up.

In this module, in order: the backup form and Back Up Account (any time,
for an account whose key is kept here), then Create Account, then Restore
Account. Each window is an assistant (nostr/ui/assistant.py), reached from
the Nostr menu and from the connect dialog. They follow nostrdesign.org as
well as Apple's guidelines:

- Plain words. A private key is explained as a password that can never be
  reset; protocol names stay out of the copy.
- The private key is never shown on screen, not even partly. It can be
  copied (marked as a secret, and taken off the clipboard again after a
  minute) or saved in a backup file. The one exception is the code Amber
  scans, which is shown only on request, on the page that says what it is
  for.
- The backup is asked for right after the key exists, because there is no
  reset. Skipping is allowed, after an alert that says what it costs.
- The backup file is protected by a password by default (NIP-49, which
  other Nostr apps import as well); an unprotected file needs a second,
  explicit choice. Protecting it takes a second of deliberate work, done
  off the UI thread like unlocking one.
- Pasting a private key is the least prominent way in, with a warning,
  because a pasted key is how keys leak.

Create Account then asks where the key should live: on this computer
(MyEditor signs with it, nostr/local_signer.py), or in Amber on the
person's phone, with a guided import and pairing (the Lotus "move to a
signer" flow). Either way the window hands the finished profile to the
account controller (nostr/account_controller.py), which publishes the
relay list and then the profile, and reports each step back through a
SetupReport. The window can be closed while that runs; the work goes on.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QEvent, QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from alerts import DEFAULT as ALERT_DEFAULT, DESTRUCTIVE, Button, ask
from nostr import bech32, crypto, nip49
from nostr.outbox.defaults import STARTER_LIST
from nostr.account_backup import (
    BackupError, FoundKey, backup_text, find_key, password_problem, suggested_file_name,
)
from nostr.profiles import Profile
from nostr.qr import make_qr_pixmap
from nostr.ui.assistant import (
    DEFAULT, LEADING, NORMAL, AssistantWindow, StepList, busy_bar, copy_secret,
    link_button, page, text_label,
)

AMBER_URL = "https://zapstore.dev/apps/com.greenart7c3.nostrsigner"

# Create Account pages.
NAME = "name"
BACKUP = "backup"
CHOOSE = "choose"
AMBER_GET = "amber_get"
AMBER_IMPORT = "amber_import"
AMBER_CONNECT = "amber_connect"
SETUP = "setup"
DONE = "done"

# Restore Account pages.
OPEN = "open"
PASSWORD = "password"

# Setup steps, in the order the account controller works through them.
STEP_SAVED = "saved"
STEP_RELAYS = "relays"
STEP_PROFILE = "profile"

KEY_NOT_SAVED = ("Your key couldn’t be saved on this computer. Make sure the disk "
                 "has free space, then try again.")
CLOSE_ANY_TIME = "You can close this window. MyEditor keeps going in the background."
FINISH_LATER = "If you finish later, MyEditor tries again the next time you open it."


def short_npub(pubkey_hex: str) -> str:
    npub = bech32.encode_npub(pubkey_hex)
    return f"{npub[:12]}…{npub[-6:]}"


def local_profile(pubkey_hex: str, *, relays=None) -> Profile:
    """A profile that signs with a key kept on this computer."""
    return Profile(user_pubkey=pubkey_hex, bunker_pubkey="",
                   bunker_relays=list(relays or [url for url, _marker in STARTER_LIST]),
                   local_secret_hex="", signer="local")


class SetupReport(QObject):
    """How the account controller reports publishing a new or restored
    account to the window that shows it.

    ``retry``, when the controller sets it, sends the same work again (for
    a new account: the very events signed the first time). The report
    belongs to the window; the controller never relies on it outliving
    the window.
    """

    step = Signal(str, str, str)     # step key, state, detail
    finished = Signal(bool, str)     # ok, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.retry: Optional[Callable[[], None]] = None


class _Job(QRunnable):
    """Run ``work`` off the UI thread (protecting or unlocking a key takes a
    second of deliberate effort) and deliver its result on the UI thread."""

    class _Signals(QObject):
        done = Signal(object, object)   # result, error

    def __init__(self, work: Callable[[], object]):
        super().__init__()
        self.signals = self._Signals()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001, delivered to the UI as an answer
            self.signals.done.emit(None, exc)
            return
        self.signals.done.emit(result, None)


def run_in_background(work: Callable[[], object], on_done: Callable[[object, object], None],
                      *, keep_alive: list) -> None:
    job = _Job(work)
    keep_alive.append(job.signals)   # the signals object must outlive the run
    job.signals.done.connect(on_done)
    QThreadPool.globalInstance().start(job)


def _password_row(form: QFormLayout, label: str, placeholder: str = "") -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    field.setPlaceholderText(placeholder)
    form.addRow(label, field)
    return field


def choose_backup_path(parent, pubkey_hex: str) -> Optional[str]:
    """Ask where the backup goes; None when canceled."""
    start = os.path.join(os.path.expanduser("~"), "Documents", suggested_file_name(pubkey_hex))
    path, _ = QFileDialog.getSaveFileName(parent, "Save Backup File", start,
                                          "Text files (*.txt)")
    return path or None


def write_backup_file(path: str, text: str) -> None:
    """Write ``text`` to ``path``, readable by this user only from the
    first byte on: the file is created with those permissions, and an
    existing file gets them before anything is written to it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


# =============================================================================
# The backup form, shared by Create Account and Back Up Account
# =============================================================================

class BackupForm(QWidget):
    """The public key, a password-protected backup file, and the two
    less safe ways to keep the key: copying it, or a file without a
    password. ``backed_up`` fires with a line saying what was done."""

    backed_up = Signal(str)

    def __init__(self, *, secret: Callable[[], Optional[bytes]],
                 choose_path=choose_backup_path, write_file=write_backup_file,
                 is_dark: bool = True, parent=None):
        super().__init__(parent)
        self._secret = secret
        self._choose_path = choose_path
        self._write_file = write_file
        self._is_dark = is_dark
        self._saving = False
        self._keep_alive: list = []
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        key_row = QHBoxLayout()
        key_row.addWidget(text_label("Your public key", "item_title", wrap=False))
        self._npub_label = text_label("", "mono", wrap=False)
        key_row.addWidget(self._npub_label, 1)
        key_row.addWidget(link_button("Copy", self._copy_npub))
        col.addSpacing(4)
        col.addLayout(key_row)
        col.addWidget(text_label("Share it so people can find you.", "help"))

        col.addSpacing(8)
        col.addWidget(text_label("Backup file", "item_title"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.password = _password_row(form, "Password:", "At least 8 characters")
        self.confirm = _password_row(form, "Confirm:")
        col.addLayout(form)
        self._password_error = text_label("", "error")
        self._password_error.hide()
        col.addWidget(self._password_error)
        col.addWidget(text_label(
            "The password protects the file. You need both to restore the account.",
            "help"))
        for field in (self.password, self.confirm):
            field.textChanged.connect(self._check_password)
            # Return in a password field saves the file; it must never reach
            # the window's default button, which would close the window or
            # move on without a backup.
            field.installEventFilter(self)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Save Backup File…")
        self.save_button.setAutoDefault(False)
        self.save_button.clicked.connect(self.save_protected)
        actions.addWidget(self.save_button)
        actions.addSpacing(16)
        self._copy_link = link_button("Copy Private Key", self.copy_private_key)
        actions.addWidget(self._copy_link)
        actions.addSpacing(16)
        self._plain_link = link_button("Save Without a Password…", self.save_plain)
        actions.addWidget(self._plain_link)
        actions.addStretch(1)
        col.addLayout(actions)
        self.note = text_label("", "muted")
        self.note.hide()
        col.addWidget(self.note)

    def refresh(self) -> None:
        secret = self._secret()
        if secret is not None:
            pubkey = crypto.get_public_key(secret).hex()
            self._npub_label.setText(short_npub(pubkey))
            self._npub_label.setToolTip(bech32.encode_npub(pubkey))
        self._check_password()

    @property
    def saving(self) -> bool:
        return self._saving

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        # Only Return is looked at, and nothing else is touched for any
        # other event: filters also see the fields' events while the form
        # is being torn down.
        if event.type() != QEvent.KeyPress or event.key() not in (Qt.Key_Return,
                                                                   Qt.Key_Enter):
            return False
        if watched is self.password and not self.confirm.text():
            self.confirm.setFocus()
        else:
            self.save_protected()
        return True

    def _check_password(self) -> None:
        problem = password_problem(self.password.text(), self.confirm.text())
        self._password_error.setText(problem or "")
        self._password_error.setVisible(bool(problem) and bool(self.confirm.text()))
        self.save_button.setEnabled(problem is None and not self._saving)

    def _done(self, note: str) -> None:
        self.note.setText(note)
        self.note.show()
        self.backed_up.emit(note)

    def save_protected(self) -> None:
        if password_problem(self.password.text(), self.confirm.text()):
            self._check_password()
            return
        self._write(self.password.text())

    def save_plain(self) -> None:
        choice = ask(self.window(), title="Save your private key without a password?",
                     message="Anyone who opens the file can use your account. A "
                             "password-protected file is safer.",
                     caution=True, is_dark=self._is_dark, buttons=(
                         Button("Save Without Password", True, DESTRUCTIVE),
                         Button("Cancel", False, ALERT_DEFAULT)))
        if choice is True:
            self._write(None)

    def _set_saving(self, saving: bool) -> None:
        self._saving = saving
        for widget in (self.password, self.confirm, self._copy_link, self._plain_link):
            widget.setEnabled(not saving)
        if saving:
            self.note.setText("Saving the backup…")
            self.note.show()
        self._check_password()

    def _write(self, password: Optional[str]) -> None:
        secret = self._secret()
        if self._saving or secret is None:
            return
        path = self._choose_path(self.window(), crypto.get_public_key(secret).hex())
        if not path:
            return
        self._set_saving(True)
        write_file = self._write_file

        def work() -> str:
            write_file(path, backup_text(secret, password=password))
            return path

        def done(_result, error) -> None:
            # The file is written even when the window was closed meanwhile;
            # only the words about it have nowhere to go.
            if not shiboken6.isValid(self):
                return
            self._set_saving(False)
            if error is not None:
                self.note.setText("The backup couldn’t be saved there. Choose "
                                  "another folder and try again.")
                self.note.show()
                return
            self._done(f"✓ Backup saved to {path}")

        run_in_background(work, done, keep_alive=self._keep_alive)

    def copy_private_key(self) -> None:
        secret = self._secret()
        if secret is None:
            return
        copy_secret(bech32.encode_nsec(secret.hex()))
        self._done("✓ Private key copied. Paste it somewhere only you can reach. "
                   "MyEditor clears it from the clipboard in a minute.")

    def _copy_npub(self) -> None:
        secret = self._secret()
        if secret is not None:
            QApplication.clipboard().setText(
                bech32.encode_npub(crypto.get_public_key(secret).hex()))


class BackupAccountWindow(AssistantWindow):
    """Back up an account whose key is kept on this computer, any time."""

    def __init__(self, *, secret: bytes, choose_path=choose_backup_path,
                 write_file=write_backup_file, is_dark: bool = True, parent=None):
        super().__init__("Back Up Account", is_dark=is_dark, parent=parent)
        self._secret = secret
        widget, col = page()
        col.addWidget(text_label("Back Up Your Account", "title"))
        col.addWidget(text_label(
            "Nostr has no password reset. A backup lets you restore this account on "
            "another computer, or here if this one is reset."))
        self.backup_form = BackupForm(secret=lambda: self._secret, choose_path=choose_path,
                                      write_file=write_file, is_dark=is_dark)
        col.addWidget(self.backup_form)
        col.addStretch(1)
        self.add_page(BACKUP, widget)
        self.show_page(BACKUP, [("done", "Done", DEFAULT, self.accept)])
        self.backup_form.refresh()

    def done(self, result: int) -> None:
        self._secret = None
        super().done(result)


# =============================================================================
# Create Account
# =============================================================================

class CreateAccountWindow(AssistantWindow):
    """From nothing to a signed-in Nostr account with a backup."""

    account_ready = Signal(object, str, object)   # Profile, name, SetupReport
    link_activated = Signal(str)

    def __init__(self, *, store, vault, connect_signer: Callable[..., None],
                 is_dark: bool = True, generate=crypto.generate_secret_key,
                 choose_path=choose_backup_path, write_file=write_backup_file, parent=None):
        """``connect_signer(on_profile, parent=window)`` opens the signer
        pairing over this window and calls back with the paired Profile,
        unsaved: this window saves it once it is the new account.
        ``generate``, ``choose_path`` and ``write_file`` are seams for tests."""
        super().__init__("Create Nostr Account", is_dark=is_dark, parent=parent)
        self._store = store
        self._vault = vault
        self._connect_signer = connect_signer
        self._generate = generate
        self._choose_path = choose_path
        self._write_file = write_file
        self._secret: Optional[bytes] = None
        self._pubkey = ""
        self._backed_up = False
        self._report: Optional[SetupReport] = None

        self._build_name()
        self._build_backup()
        self._build_choose()
        self._build_amber_get()
        self._build_amber_import()
        self._build_amber_connect()
        self._build_setup()
        self._build_done()
        self._show_name()

    # -- name ----------------------------------------------------------------

    def _build_name(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Create Your Nostr Account", "title"))
        col.addWidget(text_label(
            "A Nostr account works in every Nostr app, and it belongs to you, "
            "not to MyEditor."))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Your name")
        self._name_edit.setMaxLength(100)
        form.addRow("Name:", self._name_edit)
        col.addSpacing(6)
        col.addLayout(form)
        col.addWidget(text_label("Optional. People see this name. You can change it later.",
                                 "help"))
        col.addStretch(1)
        self.add_page(NAME, widget)

    def _show_name(self) -> None:
        self.show_page(NAME, [("cancel", "Cancel", NORMAL, self.reject),
                              ("continue", "Continue", DEFAULT, self._make_key)])
        self._name_edit.setFocus()

    def _make_key(self) -> None:
        if self._secret is None:
            self._secret = self._generate()
            self._pubkey = crypto.get_public_key(self._secret).hex()
        self._show_backup()

    @property
    def name(self) -> str:
        return self._name_edit.text().strip()

    @property
    def pubkey(self) -> str:
        return self._pubkey

    # -- backup --------------------------------------------------------------

    def _build_backup(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Save a Way Back In", "title"))
        col.addWidget(text_label(
            "Nostr has no password reset. Your private key works like a password "
            "that can never be changed, and it is the only way back into this "
            "account. Save a backup now."))
        self.backup_form = BackupForm(secret=lambda: self._secret,
                                      choose_path=self._choose_path,
                                      write_file=self._write_file, is_dark=self._is_dark)
        self.backup_form.backed_up.connect(self._backup_done)
        col.addWidget(self.backup_form)
        col.addStretch(1)
        self.add_page(BACKUP, widget)

    def _show_backup(self) -> None:
        self.backup_form.refresh()
        self.show_page(BACKUP, [("back", "Go Back", LEADING, self._show_name),
                                ("skip", "Skip for Now", NORMAL, self._skip_backup),
                                ("continue", "Continue", DEFAULT, self._show_choose)])
        self._update_backup_buttons()
        self.backup_form.password.setFocus()

    def _update_backup_buttons(self) -> None:
        if "continue" in self.buttons:
            self.buttons["continue"].setEnabled(self._backed_up)
        if "skip" in self.buttons:
            self.buttons["skip"].setVisible(not self._backed_up)

    def _backup_done(self, _note: str) -> None:
        self._backed_up = True
        self._update_backup_buttons()

    def _skip_backup(self) -> None:
        choice = ask(self, title="Continue without a backup?",
                     message="If this computer is lost or reset, nobody can get this "
                             "account back for you.",
                     caution=True, is_dark=self._is_dark, buttons=(
                         Button("Continue Without Backup", True, DESTRUCTIVE),
                         Button("Save a Backup", False, ALERT_DEFAULT)))
        if choice is True:
            self._show_choose()

    # -- where the key lives ---------------------------------------------------

    def _build_choose(self) -> None:
        widget, col = page(12)
        col.addWidget(text_label("Choose How to Sign In", "title"))
        col.addWidget(text_label("You can change this later."))
        self._choice_local = QRadioButton("Keep the key on this computer")
        self._choice_local.setChecked(True)
        col.addWidget(self._choice_local)
        col.addWidget(self._indented(text_label(
            "MyEditor keeps your private key in a file only your user account can "
            "read, and signs for you. The quickest way to start.", "muted")))
        self._choice_amber = QRadioButton("Use a signer app on your phone")
        col.addWidget(self._choice_amber)
        col.addWidget(self._indented(text_label(
            "Your private key lives in Amber on your Android phone, and you approve "
            "what MyEditor signs there. Safer, and a few more steps.", "muted")))
        self._choose_error = text_label("", "error")
        self._choose_error.hide()
        col.addWidget(self._choose_error)
        col.addStretch(1)
        self.add_page(CHOOSE, widget)

    @staticmethod
    def _indented(child: QWidget) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(26, 0, 0, 0)
        row.addWidget(child)
        return box

    def _show_choose(self) -> None:
        self._choose_error.hide()
        self.show_page(CHOOSE, [("back", "Go Back", LEADING, self._show_backup),
                                ("continue", "Continue", DEFAULT, self._chosen)])

    def _chosen(self) -> None:
        if self._choice_amber.isChecked():
            self._show_amber_get()
        else:
            self._finish_local()

    def _finish_local(self) -> None:
        profile = local_profile(self._pubkey)
        profile.display_name = self.name
        try:
            self._vault.store(self._secret)
            self._store.upsert(profile)
        except OSError:
            self._choose_error.setText(KEY_NOT_SAVED)
            self._choose_error.show()
            return
        self._start_setup(profile, saved_detail="Your key is kept on this computer.")

    # -- Amber -------------------------------------------------------------------

    def _build_amber_get(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Get Amber", "title"))
        col.addWidget(text_label(
            "Amber is a free signer app for Android. It keeps your private key on "
            "your phone and asks you before anything is signed."))
        col.addWidget(link_button("Get Amber from Zapstore",
                                  lambda: self.link_activated.emit(AMBER_URL)),
                      0, Qt.AlignLeft)
        col.addSpacing(6)
        col.addWidget(text_label(
            "Amber runs on Android only. On an iPhone, go back and keep the key on "
            "this computer for now.", "muted"))
        col.addStretch(1)
        self.add_page(AMBER_GET, widget)

    def _show_amber_get(self) -> None:
        self.show_page(AMBER_GET, [("back", "Go Back", LEADING, self._show_choose),
                                   ("continue", "Continue", DEFAULT, self._show_amber_import)])

    def _build_amber_import(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Add Your Key to Amber", "title"))
        col.addWidget(StepList([
            ("add", "In Amber, choose Add account. Not Connect app."),
            ("key", "Choose to use a private key, then scan the code below or paste "
                    "the key you copy here."),
        ]))
        self._qr = QLabel()
        self._qr.setObjectName("qr")
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.setAccessibleName("Private key code for Amber")
        self._qr.hide()
        col.addWidget(self._qr, 0, Qt.AlignHCenter)
        row = QHBoxLayout()
        self._qr_button = link_button("Show Code for Amber", self._toggle_qr)
        row.addWidget(self._qr_button)
        row.addSpacing(16)
        row.addWidget(link_button("Copy Private Key", self._copy_for_amber))
        row.addStretch(1)
        col.addLayout(row)
        self._amber_note = text_label(
            "This code is your private key. Scan it only with your own Amber, on its "
            "Add account screen.", "muted")
        col.addWidget(self._amber_note)
        col.addStretch(1)
        self.add_page(AMBER_IMPORT, widget)

    def _show_amber_import(self) -> None:
        self._qr.hide()
        self._qr_button.setText("Show Code for Amber")
        self.show_page(AMBER_IMPORT, [("back", "Go Back", LEADING, self._show_amber_get),
                                      ("continue", "I’ve Added It", DEFAULT,
                                       self._show_amber_connect)])

    def _toggle_qr(self) -> None:
        if not self._qr.isHidden():
            self._qr.clear()
            self._qr.hide()
            self._qr_button.setText("Show Code for Amber")
            return
        # The key exactly as Amber imports it: a lowercase nsec.
        nsec = bech32.encode_nsec(self._secret.hex())
        self._qr.setPixmap(make_qr_pixmap(nsec, size=200, dark="#000000", light="#FFFFFF"))
        self._qr.show()
        self._qr_button.setText("Hide Code")

    def _copy_for_amber(self) -> None:
        copy_secret(bech32.encode_nsec(self._secret.hex()))
        self._amber_note.setText("Private key copied. Paste it in Amber’s Add account "
                                 "screen. MyEditor clears it from the clipboard in a minute.")

    def _build_amber_connect(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Connect Amber to MyEditor", "title"))
        col.addWidget(StepList([
            ("open", "In Amber, open Connect app (the scan icon)."),
            ("scan", "Scan the code MyEditor shows, and approve the request."),
        ]))
        col.addWidget(text_label(
            "MyEditor then checks that Amber signs as your new account.", "muted"))
        self._connect_error = text_label("", "error")
        self._connect_error.hide()
        col.addWidget(self._connect_error)
        col.addStretch(1)
        self.add_page(AMBER_CONNECT, widget)

    def _show_amber_connect(self) -> None:
        self._qr.clear()
        self._qr.hide()
        self.show_page(AMBER_CONNECT, [
            ("back", "Go Back", LEADING, self._show_amber_import),
            ("connect", "Show Connection Code…", DEFAULT,
             lambda: self._connect_signer(self._on_paired, parent=self)),
        ])

    def _on_paired(self, profile) -> None:
        if profile is None:
            return
        if profile.user_pubkey.lower() != self._pubkey:
            # Amber paired as another account. That pairing is not wanted,
            # and nothing was saved for it: a profile already here for that
            # account stays exactly as it was.
            self._connect_error.setText(
                "Amber is signing as a different account. In Amber, select the "
                "account you just added, then try again.")
            self._connect_error.show()
            return
        self._connect_error.hide()
        profile.display_name = self.name or profile.display_name
        try:
            self._store.upsert(profile)
        except OSError:
            self._connect_error.setText("This account couldn’t be saved on this "
                                        "computer. Make sure the disk has free space, "
                                        "then try again.")
            self._connect_error.show()
            return
        self._start_setup(profile, saved_detail="Amber holds your key.")

    # -- setup -------------------------------------------------------------------

    def _build_setup(self) -> None:
        widget, col = page(14)
        col.addWidget(text_label("Setting Up Your Account", "title"))
        self._setup_intro = text_label("")
        col.addWidget(self._setup_intro)
        self._steps = StepList([
            (STEP_SAVED, "Account saved"),
            (STEP_RELAYS, "Publishing your relay list"),
            (STEP_PROFILE, "Publishing your profile"),
        ])
        col.addWidget(self._steps)
        self._setup_note = text_label("", "muted")
        col.addWidget(self._setup_note)
        col.addStretch(1)
        self.add_page(SETUP, widget)

    def _start_setup(self, profile, *, saved_detail: str) -> None:
        self._profile = profile
        # The key now lives in the vault or in Amber; this window no longer
        # needs a copy of it.
        self._secret = None
        remote = not getattr(profile, "is_local", False)
        self._setup_intro.setText(
            "Approve the requests in Amber when it asks." if remote else
            "MyEditor tells the Nostr network about your new account.")
        self._steps.set_state(STEP_SAVED, "done", saved_detail)
        self._steps.set_state(STEP_RELAYS, "active")
        self._steps.set_state(STEP_PROFILE, "pending")
        self._show_working()
        report = SetupReport(self)
        report.step.connect(self._steps.set_state)
        report.finished.connect(self._on_setup_finished)
        self._report = report
        self.account_ready.emit(profile, self.name, report)

    def _show_working(self) -> None:
        self._setup_note.setText(CLOSE_ANY_TIME)
        self.show_page(SETUP, [("close", "Close", NORMAL, self.accept)])

    def _on_setup_finished(self, ok: bool, message: str) -> None:
        if ok:
            self._show_done()
            return
        first = message or "Your account is saved, but it isn’t published yet."
        self._setup_note.setText(f"{first} {FINISH_LATER}")
        buttons = [("later", "Finish Later", NORMAL, self.accept)]
        if self._report is not None and self._report.retry is not None:
            buttons.append(("retry", "Try Again", DEFAULT, self._retry_setup))
        self.show_page(SETUP, buttons)

    def _retry_setup(self) -> None:
        self._steps.set_state(STEP_RELAYS, "active")
        self._steps.set_state(STEP_PROFILE, "pending")
        self._show_working()
        self._report.retry()

    # -- done ----------------------------------------------------------------------

    def _build_done(self) -> None:
        widget, col = page()
        col.addWidget(text_label("You’re All Set", "title"))
        self._done_text = text_label("")
        col.addWidget(self._done_text)
        col.addStretch(1)
        self.add_page(DONE, widget)

    def _show_done(self) -> None:
        who = self.name or short_npub(self._pubkey)
        self._done_text.setText(
            f"{who} is ready. You can publish notes and articles, and keep private "
            "drafts that follow you to every device.")
        self.show_page(DONE, [("done", "Done", DEFAULT, self.accept)])

    def done(self, result: int) -> None:
        self._secret = None
        super().done(result)


# =============================================================================
# Restore Account
# =============================================================================

class RestoreAccountWindow(AssistantWindow):
    """Back into an account from its backup file or its private key."""

    account_ready = Signal(object, str, object)   # Profile, name (""), SetupReport

    def __init__(self, *, store, vault, is_dark: bool = True, open_file=None, parent=None):
        """``open_file()`` returns the chosen file's text or None; a seam for tests."""
        super().__init__("Restore Nostr Account", is_dark=is_dark, parent=parent)
        self._store = store
        self._vault = vault
        self._open_file = open_file or self._choose_file
        self._found: Optional[FoundKey] = None
        self._keep_alive: list = []
        # Bumped whenever an unlock's answer stops being wanted (the window
        # closed), so a late answer is dropped instead of signing in.
        self._unlock_round = 0
        self._build_open()
        self._build_password()
        self._build_setup()
        self._show_open()

    # -- open ----------------------------------------------------------------------

    def _build_open(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Restore Your Account", "title"))
        col.addWidget(text_label(
            "Open the backup file you saved when you created the account."))
        col.addSpacing(6)
        self._open_error = text_label("", "error")
        self._open_error.hide()
        col.addWidget(self._open_error)
        col.addSpacing(10)
        self._paste_link = link_button("Paste a Private Key Instead", self._reveal_paste)
        col.addWidget(self._paste_link, 0, Qt.AlignLeft)
        self._paste_box = QWidget()
        paste = QVBoxLayout(self._paste_box)
        paste.setContentsMargins(0, 0, 0, 0)
        paste.addWidget(text_label("Private key", "item_title"))
        self._paste_edit = QLineEdit()
        self._paste_edit.setEchoMode(QLineEdit.Password)
        self._paste_edit.setPlaceholderText("nsec1… or ncryptsec1…")
        self._paste_edit.setAccessibleName("Private key")
        paste.addWidget(self._paste_edit)
        paste.addWidget(text_label(
            "Only paste your private key into apps you trust. Anyone who has it can "
            "use your account.", "help"))
        paste.addWidget(link_button("Use a Backup File Instead", self._hide_paste),
                        0, Qt.AlignLeft)
        self._paste_box.hide()
        col.addWidget(self._paste_box)
        col.addStretch(1)
        self.add_page(OPEN, widget)

    def _show_open(self) -> None:
        if not self._paste_box.isHidden():
            buttons = [("cancel", "Cancel", NORMAL, self.reject),
                       ("continue", "Continue", DEFAULT, self._use_paste)]
        else:
            buttons = [("cancel", "Cancel", NORMAL, self.reject),
                       ("choose", "Choose Backup File…", DEFAULT, self._use_file)]
        self.show_page(OPEN, buttons)

    def _reveal_paste(self) -> None:
        self._paste_box.show()
        self._paste_link.hide()
        self._show_open()
        self._paste_edit.setFocus()

    def _hide_paste(self) -> None:
        self._paste_edit.clear()
        self._paste_box.hide()
        self._paste_link.show()
        self._show_open()

    def _choose_file(self) -> Optional[str]:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Backup File", os.path.expanduser("~"),
            "Backup files (*.txt);;All files (*)")
        if not path:
            return None
        if os.path.getsize(path) > 64 * 1024:
            raise BackupError("That file is too large to be a backup.")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _use_file(self) -> None:
        try:
            text = self._open_file()
        except BackupError as exc:
            self._show_open_error(str(exc))
            return
        except OSError:
            self._show_open_error("That file couldn’t be opened. Choose the backup "
                                  "file again, or paste the key instead.")
            return
        if text is None:
            return
        self._use_text(text, allow_hex=False)

    def _use_paste(self) -> None:
        try:
            self._use_text(self._paste_edit.text(), allow_hex=True)
        finally:
            self._paste_edit.clear()

    def _use_text(self, text: str, *, allow_hex: bool) -> None:
        try:
            found = find_key(text, allow_hex=allow_hex,
                             public_keys=[p.user_pubkey for p in self._store])
        except BackupError as exc:
            self._show_open_error(str(exc))
            return
        self._open_error.hide()
        self._found = found
        if found.protected:
            self._show_password()
        else:
            self._restore(found.secret)

    def _show_open_error(self, message: str) -> None:
        self._open_error.setText(message)
        self._open_error.show()

    # -- password ------------------------------------------------------------------

    def _build_password(self) -> None:
        widget, col = page()
        col.addWidget(text_label("Enter the Backup Password", "title"))
        col.addWidget(text_label(
            "The private key in this backup is protected with the password you "
            "chose when you saved it."))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self._backup_password = _password_row(form, "Password:")
        col.addLayout(form)
        self._password_error = text_label("", "error")
        self._password_error.hide()
        col.addWidget(self._password_error)
        self._unlocking = busy_bar()
        self._unlocking.hide()
        col.addWidget(self._unlocking)
        col.addStretch(1)
        self.add_page(PASSWORD, widget)

    def _show_password(self) -> None:
        self._password_error.hide()
        self._unlocking.hide()
        self.show_page(PASSWORD, [("back", "Go Back", LEADING, self._show_open),
                                  ("restore", "Restore", DEFAULT, self._unlock)])
        self._backup_password.setFocus()

    def _unlock(self) -> None:
        found, password = self._found, self._backup_password.text()
        if found is None:
            return
        self._backup_password.clear()
        self._password_error.hide()
        self._unlocking.show()
        # While the password is checked there is nowhere else to go.
        self.buttons["restore"].setEnabled(False)
        self.buttons["back"].setEnabled(False)
        round_ = self._unlock_round

        def done(secret, error):
            if round_ != self._unlock_round or not shiboken6.isValid(self):
                return
            self._unlocking.hide()
            if error is not None:
                self.buttons["restore"].setEnabled(True)
                self.buttons["back"].setEnabled(True)
                if isinstance(error, nip49.Nip49Error):
                    text = ("The password is wrong. Try again." if error.wrong_password
                            else str(error))
                else:
                    text = "This backup couldn’t be opened."
                self._password_error.setText(text)
                self._password_error.show()
                return
            self._restore(secret)

        run_in_background(lambda: found.open_with(password), done,
                          keep_alive=self._keep_alive)

    # -- restore -------------------------------------------------------------------

    def _build_setup(self) -> None:
        widget, col = page(14)
        col.addWidget(text_label("Restoring Your Account", "title"))
        self._who = text_label("", "mono")
        col.addWidget(self._who)
        self._steps = StepList([
            (STEP_SAVED, "Account saved on this computer"),
            (STEP_RELAYS, "Finding your relay list"),
            (STEP_PROFILE, "Finding your profile"),
        ])
        col.addWidget(self._steps)
        self._setup_note = text_label("", "muted")
        col.addWidget(self._setup_note)
        col.addStretch(1)
        self.add_page(SETUP, widget)

    def _restore(self, secret: bytes) -> None:
        pubkey = crypto.get_public_key(secret).hex()
        existing = self._store.get(pubkey)
        profile = local_profile(pubkey, relays=getattr(existing, "bunker_relays", None))
        if existing is not None:
            # The account is already known here: keep what was learned about it.
            for name in ("display_name", "picture", "nip05", "avatar_path",
                         "metadata_cached_at", "setup_pending"):
                setattr(profile, name, getattr(existing, name))
        try:
            self._vault.store(secret)
            self._store.upsert(profile)
        except OSError:
            error = self._password_error if self.page == PASSWORD else self._open_error
            error.setText(KEY_NOT_SAVED)
            error.show()
            if "restore" in self.buttons:
                self.buttons["restore"].setEnabled(True)
                self.buttons["back"].setEnabled(True)
            return
        self._profile = profile
        self._found = None
        self._who.setText(short_npub(pubkey))
        self._steps.set_state(STEP_SAVED, "done")
        self._steps.set_state(STEP_RELAYS, "active")
        self._steps.set_state(STEP_PROFILE, "pending")
        self._setup_note.setText(CLOSE_ANY_TIME)
        self.show_page(SETUP, [("close", "Close", NORMAL, self.accept)])
        report = SetupReport(self)
        report.step.connect(self._steps.set_state)
        report.finished.connect(self._on_restored)
        self.account_ready.emit(profile, "", report)

    def _on_restored(self, ok: bool, message: str) -> None:
        self._setup_note.setText(message)
        self._setup_note.setVisible(bool(message))
        self.show_page(SETUP, [("done", "Done", DEFAULT, self.accept)])

    def done(self, result: int) -> None:
        self._unlock_round += 1
        self._found = None
        super().done(result)
