# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins creating a Nostr account and restoring one from its backup.

What must hold:

  The backup file opens again here and in other apps: it carries the
  public key and the protected key, and the key reads back with its
  password. An unprotected file is a separate, explicit choice. The file
  is readable by its owner only from the first byte, and protecting it
  happens off the UI thread.

  A key is found wherever it sits in a backup or a paste; a public key
  (also as hex, when it is an account MyEditor knows), a damaged key or a
  file with no key is explained in plain words.

  Create Account makes the key only after the first step, shows only the
  public key, asks for a backup before going on (or an explicit skip),
  and keeps the key on this computer or hands it to Amber, as chosen.
  Amber pairing as a different account is refused, and a profile already
  here for that account is left exactly as it was. Return in a backup
  password field saves the file and never closes the window. The steps
  are shown in the order the work runs: relay list, then profile.

  Restore Account reads a backup file or a pasted key, asks for the
  password of a protected one, refuses a wrong password in words, never
  shows a library's message, and signs the account in on this computer.
  A key that can't be saved is explained, and a pasted key never stays in
  its field.

The private key never appears as text in any window.
"""

import datetime
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton  # noqa: E402

from nostr import bech32, crypto, nip49  # noqa: E402
from nostr import account_backup as ab  # noqa: E402
from nostr.key_vault import KeyVault  # noqa: E402
from nostr.profiles import Profile, ProfileStore  # noqa: E402
from nostr.ui import account_windows as aw  # noqa: E402
from nostr.ui import assistant  # noqa: E402
from nostr.ui.account_windows import (  # noqa: E402
    AMBER_CONNECT, AMBER_GET, AMBER_IMPORT, BACKUP, CHOOSE, DONE, NAME, OPEN, PASSWORD,
    SETUP, STEP_PROFILE, STEP_RELAYS, CreateAccountWindow, RestoreAccountWindow,
)

SK = bytes.fromhex("1f" * 32)
PK = crypto.get_public_key(SK).hex()
NSEC = bech32.encode_nsec(SK.hex())


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    answers = []
    monkeypatch.setattr(aw, "ask", lambda parent, **kw: answers.pop(0) if answers else False)
    return answers


def settle():
    QThreadPool.globalInstance().waitForDone(10_000)
    for _ in range(5):
        QApplication.processEvents()


def all_text(window) -> str:
    parts = [w.text() for w in window.findChildren(QLabel)]
    parts += [w.text() for w in window.findChildren(QPushButton)]
    parts += [w.toolTip() for w in window.findChildren(QLabel)]
    return "\n".join(parts)


class Files:
    """The backup seams: where the file goes, and what was written."""

    def __init__(self, tmp_path):
        self.path = str(tmp_path / "backup.txt")
        self.written = []

    def choose(self, parent, pubkey):
        return self.path

    def write(self, path, text):
        self.written.append((path, text))

    def opens_with(self, password):
        _path, text = self.written[-1]
        return ab.find_key(text).open_with(password)


class BrokenVault(KeyVault):
    def store(self, secret_key):
        raise OSError(28, "No space left on device")


# -- the backup file -----------------------------------------------------------------

def test_a_protected_backup_names_the_account_and_reopens_with_its_password():
    text = ab.backup_text(SK, password="correct horse", today=datetime.date(2026, 10, 5))
    assert bech32.encode_npub(PK) in text
    assert NSEC not in text and "ncryptsec1" in text
    assert "Saved 2026-10-05" in text and "\u2014" not in text
    found = ab.find_key(text)
    assert found.protected and found.open_with("correct horse") == SK


def test_an_unprotected_backup_says_so():
    text = ab.backup_text(SK, password=None)
    assert NSEC in text and "not protected" in text
    found = ab.find_key(text)
    assert not found.protected and found.secret == SK


def test_a_key_is_found_wherever_it_sits():
    assert ab.find_key(f"my key, saved by hand:\n\n   {NSEC.upper()}  \n").secret == SK
    assert ab.find_key(SK.hex(), allow_hex=True).secret == SK


def test_hex_is_only_accepted_from_a_paste():
    with pytest.raises(ab.BackupError):
        ab.find_key(SK.hex())


def test_what_is_not_a_private_key_is_explained():
    with pytest.raises(ab.BackupError, match="public key"):
        ab.find_key(bech32.encode_npub(PK))
    with pytest.raises(ab.BackupError, match="No private key"):
        ab.find_key("a shopping list")
    with pytest.raises(ab.BackupError, match="incomplete or mistyped"):
        ab.find_key(NSEC[:-1] + ("q" if NSEC[-1] != "q" else "p"))


def test_a_known_public_key_pasted_as_hex_is_refused_as_one():
    with pytest.raises(ab.BackupError, match="public key"):
        ab.find_key(PK.upper(), allow_hex=True, public_keys=[PK])


def test_a_found_key_never_shows_itself_in_a_repr():
    shown = repr(ab.find_key(NSEC)) + repr(ab.find_key(ab.backup_text(SK, password="pw pw pw")))
    assert NSEC not in shown and SK.hex() not in shown and "ncryptsec1" not in shown


def test_backup_passwords_need_length_and_agreement():
    assert ab.password_problem("short", "short") == "Use at least 8 characters."
    assert "match" in ab.password_problem("long enough", "long enougj")
    assert ab.password_problem("long enough", "long enough") is None


def test_the_suggested_file_name_says_whose_account_it_is():
    name = ab.suggested_file_name(PK)
    assert name.startswith("nostr-account-") and name.endswith(".txt")
    assert name[14:22] == bech32.encode_npub(PK)[5:13]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_backup_file_is_private_from_the_first_byte(tmp_path):
    path = tmp_path / "backup.txt"
    path.write_text("an older file anyone could read")
    os.chmod(path, 0o644)
    aw.write_backup_file(str(path), "the backup")
    assert path.read_text() == "the backup"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    fresh = tmp_path / "fresh.txt"
    aw.write_backup_file(str(fresh), "x")
    assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o600


# -- Create Account ------------------------------------------------------------------

def make_create(tmp_path, *, paired=None, vault=None):
    store = ProfileStore(tmp_path / "profiles.json")
    vault = vault or KeyVault(tmp_path / "keys.json")
    files = Files(tmp_path)
    pairings = []

    def connect_signer(on_profile, parent=None):
        pairings.append(parent)
        on_profile(paired)

    win = CreateAccountWindow(store=store, vault=vault, connect_signer=connect_signer,
                              generate=lambda: SK, choose_path=files.choose,
                              write_file=files.write, is_dark=False)
    win.pairings = pairings
    ready = []
    win.account_ready.connect(lambda p, n, r: ready.append((p, n, r)))
    return win, store, vault, files, ready


def to_choose(win):
    win.buttons["continue"].click()
    win.backup_form.copy_private_key()
    win.buttons["continue"].click()
    assert win.page == CHOOSE


def test_the_key_is_made_only_after_the_first_step(tmp_path):
    win, *_ = make_create(tmp_path)
    assert win.page == NAME and win.pubkey == ""
    win._name_edit.setText("Satoshi")
    win.buttons["continue"].click()
    assert win.page == BACKUP and win.pubkey == PK


def test_only_the_public_key_is_ever_shown(tmp_path):
    win, *_ = make_create(tmp_path)
    win.buttons["continue"].click()
    text = all_text(win)
    assert NSEC not in text and SK.hex() not in text
    assert bech32.encode_npub(PK)[:12] in text


def test_continue_waits_for_a_backup_or_an_explicit_skip(tmp_path, alerts):
    win, *_ = make_create(tmp_path)
    win.buttons["continue"].click()
    assert not win.buttons["continue"].isEnabled()
    alerts.append(False)                      # "Save a Backup"
    win.buttons["skip"].click()
    assert win.page == BACKUP
    alerts.append(True)                       # "Continue Without Backup"
    win.buttons["skip"].click()
    assert win.page == CHOOSE


def test_a_protected_backup_file_needs_a_good_password(tmp_path):
    win, _store, _vault, files, _ready = make_create(tmp_path)
    win.buttons["continue"].click()
    win.backup_form.password.setText("short")
    win.backup_form.confirm.setText("short")
    assert not win.backup_form.save_button.isEnabled()
    win.backup_form.password.setText("correct horse")
    win.backup_form.confirm.setText("correct horse")
    win.backup_form.save_button.click()
    settle()
    assert files.opens_with("correct horse") == SK
    assert win.buttons["continue"].isEnabled()
    assert win.buttons["skip"].isHidden()


def test_protecting_the_backup_happens_off_the_ui_thread(tmp_path):
    win, _store, _vault, files, _ready = make_create(tmp_path)
    win.buttons["continue"].click()
    form = win.backup_form
    form.password.setText("correct horse")
    form.confirm.setText("correct horse")
    form.save_button.click()
    # Back on the UI thread at once: nothing written yet, nothing to click twice.
    assert form.saving and not form.save_button.isEnabled()
    assert "Saving" in form.note.text() and not win.buttons["continue"].isEnabled()
    settle()
    assert not form.saving and files.written and "Backup saved" in form.note.text()


def test_a_backup_that_cant_be_written_is_explained_in_words(tmp_path):
    win, *_ = make_create(tmp_path)

    def refuse(path, text):
        raise PermissionError(13, "Permission denied", path)
    win.backup_form._write_file = refuse
    win.buttons["continue"].click()
    win.backup_form.password.setText("correct horse")
    win.backup_form.confirm.setText("correct horse")
    win.backup_form.save_button.click()
    settle()
    note = win.backup_form.note.text()
    assert "couldn’t be saved" in note and "Errno" not in note
    assert not win.buttons["continue"].isEnabled()


def test_an_unprotected_backup_needs_a_second_yes(tmp_path, alerts):
    win, _store, _vault, files, _ready = make_create(tmp_path)
    win.buttons["continue"].click()
    alerts.append(False)
    win.backup_form.save_plain()
    settle()
    assert files.written == []
    alerts.append(True)
    win.backup_form.save_plain()
    settle()
    assert NSEC in files.written[0][1]


def test_copying_the_key_counts_as_a_backup_and_leaves_the_clipboard_later(
        tmp_path, qt_app, monkeypatch):
    monkeypatch.setattr(assistant, "SECRET_CLIPBOARD_SECONDS", 0)
    win, *_ = make_create(tmp_path)
    win.buttons["continue"].click()
    win.backup_form.copy_private_key()
    assert qt_app.clipboard().text() == NSEC
    assert win.buttons["continue"].isEnabled()
    settle()
    assert qt_app.clipboard().text() == ""


def test_return_in_a_backup_password_saves_and_never_closes_the_window(tmp_path):
    files = Files(tmp_path)
    win = aw.BackupAccountWindow(secret=SK, is_dark=False, choose_path=files.choose,
                                 write_file=files.write)
    win.show()
    form = win.backup_form
    form.password.setText("correct horse")
    QTest.keyClick(form.password, Qt.Key_Return)      # on to the confirmation
    assert win.isVisible() and files.written == []
    form.confirm.setText("correct horse")
    QTest.keyClick(form.confirm, Qt.Key_Return)
    settle()
    assert win.isVisible() and files.opens_with("correct horse") == SK
    win.close()


def test_keeping_the_key_here_stores_it_and_signs_in_locally(tmp_path):
    win, store, vault, _files, ready = make_create(tmp_path)
    win._name_edit.setText("Satoshi")
    to_choose(win)
    win.buttons["continue"].click()           # "Keep the key on this computer"
    assert vault.load(PK) == SK
    profile = store.get(PK)
    assert profile.is_local and profile.display_name == "Satoshi"
    assert win.page == SETUP
    (got, name, report), = ready
    assert got.user_pubkey == PK and name == "Satoshi"
    report.step.emit(STEP_RELAYS, "done", "")
    report.step.emit(STEP_PROFILE, "done", "")
    report.finished.emit(True, "")
    assert win.page == DONE and "Satoshi is ready" in win._done_text.text()


def test_the_steps_are_shown_in_the_order_the_work_runs(tmp_path):
    win, *_ = make_create(tmp_path)
    to_choose(win)
    win.buttons["continue"].click()
    rows = win._steps._rows
    assert rows[STEP_RELAYS]["number"] < rows[STEP_PROFILE]["number"]
    assert win._steps.state(STEP_RELAYS) == "active"
    assert win._steps.state(STEP_PROFILE) == "pending"
    restore = RestoreAccountWindow(store=ProfileStore(tmp_path / "p2.json"),
                                   vault=KeyVault(tmp_path / "k2.json"))
    rows = restore._steps._rows
    assert rows[STEP_RELAYS]["number"] < rows[STEP_PROFILE]["number"]


def test_the_window_can_be_closed_while_the_account_is_set_up(tmp_path):
    win, *_ = make_create(tmp_path)
    to_choose(win)
    win.buttons["continue"].click()
    assert set(win.buttons) == {"close"}
    assert "keeps going" in win._setup_note.text()


def test_a_key_that_cant_be_saved_is_explained(tmp_path):
    win, store, _vault, _files, ready = make_create(tmp_path,
                                                    vault=BrokenVault(tmp_path / "k.json"))
    to_choose(win)
    win.buttons["continue"].click()
    assert win.page == CHOOSE and not win._choose_error.isHidden()
    assert "couldn’t be saved" in win._choose_error.text() and "Errno" not in all_text(win)
    assert ready == [] and store.get(PK) is None


def test_a_failed_setup_can_be_tried_again_or_finished_later(tmp_path):
    win, _store, _vault, _files, ready = make_create(tmp_path)
    to_choose(win)
    win.buttons["continue"].click()
    report = ready[0][2]
    retried = []
    report.retry = lambda: retried.append(True)
    report.finished.emit(False, "")
    assert set(win.buttons) == {"later", "retry"}
    assert "next time you open" in win._setup_note.text()
    win.buttons["retry"].click()
    assert retried == [True] and set(win.buttons) == {"close"}
    assert win._steps.state(STEP_RELAYS) == "active"
    report.retry = None
    report.finished.emit(False, "")
    assert set(win.buttons) == {"later"}


def test_the_amber_path_never_keeps_the_key_here(tmp_path, monkeypatch):
    codes = []
    real = aw.make_qr_pixmap
    monkeypatch.setattr(aw, "make_qr_pixmap",
                        lambda text, **kw: codes.append(text) or real(text, **kw))
    paired = Profile(user_pubkey=PK, bunker_pubkey="b" * 64,
                     bunker_relays=["wss://relay.nsec.app"], local_secret_hex="c" * 64)
    win, store, vault, _files, ready = make_create(tmp_path, paired=paired)
    win._name_edit.setText("Satoshi")
    to_choose(win)
    win._choice_amber.setChecked(True)
    win.buttons["continue"].click()
    assert win.page == AMBER_GET
    win.buttons["continue"].click()
    assert win.page == AMBER_IMPORT
    assert win._qr.isHidden()                 # only on request
    win._qr_button.click()
    assert not win._qr.isHidden()
    assert codes == [NSEC]                    # exactly as Amber imports it, lowercase
    win.buttons["continue"].click()
    assert win.page == AMBER_CONNECT and win._qr.pixmap().isNull()
    win.buttons["connect"].click()
    assert win.pairings == [win]              # the pairing opens over this window
    assert vault.load(PK) is None
    assert not store.get(PK).is_local and store.get(PK).display_name == "Satoshi"
    assert win.page == SETUP and ready


def test_amber_signing_as_someone_else_leaves_that_account_as_it_was(tmp_path):
    other = "ee" * 32
    win, store, _vault, _files, ready = make_create(tmp_path, paired=Profile(
        user_pubkey=other, bunker_pubkey="d" * 64, bunker_relays=["wss://new.example"],
        local_secret_hex="f" * 64))
    # The person already uses that account here, with its own signer pairing.
    store.upsert(Profile(user_pubkey=other, bunker_pubkey="b" * 64,
                         bunker_relays=["wss://old.example"], local_secret_hex="c" * 64,
                         display_name="Already here"))
    to_choose(win)
    win._choice_amber.setChecked(True)
    win.buttons["continue"].click()
    win.buttons["continue"].click()
    win.buttons["continue"].click()
    win.buttons["connect"].click()
    assert win.page == AMBER_CONNECT
    assert "different account" in win._connect_error.text()
    kept = ProfileStore(tmp_path / "profiles.json").get(other)
    assert kept is not None and kept.display_name == "Already here"
    assert kept.bunker_pubkey == "b" * 64 and ready == []
    assert store.get(PK) is None


# -- Restore Account -----------------------------------------------------------------

def make_restore(tmp_path, file_text=None, *, vault=None):
    store = ProfileStore(tmp_path / "profiles.json")
    vault = vault or KeyVault(tmp_path / "keys.json")
    win = RestoreAccountWindow(store=store, vault=vault, is_dark=True,
                               open_file=lambda: file_text)
    ready = []
    win.account_ready.connect(lambda p, n, r: ready.append((p, n, r)))
    return win, store, vault, ready


def test_a_protected_backup_file_restores_with_its_password(tmp_path):
    text = ab.backup_text(SK, password="correct horse")
    win, store, vault, ready = make_restore(tmp_path, text)
    assert win.page == OPEN
    win.buttons["choose"].click()
    assert win.page == PASSWORD
    win._backup_password.setText("wrong guess")
    win.buttons["restore"].click()
    settle()
    assert win.page == PASSWORD and "wrong" in win._password_error.text()
    win._backup_password.setText("correct horse")
    win.buttons["restore"].click()
    settle()
    assert win.page == SETUP
    assert vault.load(PK) == SK and store.get(PK).is_local
    assert ready and ready[0][0].user_pubkey == PK
    assert win._steps.state(STEP_RELAYS) == "active"


def test_while_the_password_is_checked_there_is_nowhere_to_go(tmp_path):
    win, _store, vault, ready = make_restore(tmp_path, ab.backup_text(SK, password="pw pw pw"))
    win.buttons["choose"].click()
    win._backup_password.setText("pw pw pw")
    win.buttons["restore"].click()
    assert not win.buttons["back"].isEnabled() and not win.buttons["restore"].isEnabled()
    win.reject()                              # closed before the answer
    settle()
    assert ready == [] and vault.load(PK) is None


def test_a_library_failure_never_reaches_the_window(tmp_path, monkeypatch):
    def broken(self, password):
        raise RuntimeError("[digital envelope routines] memory limit exceeded")
    monkeypatch.setattr(ab.FoundKey, "open_with", broken)
    win, *_ = make_restore(tmp_path, ab.backup_text(SK, password="pw pw pw"))
    win.buttons["choose"].click()
    win._backup_password.setText("pw pw pw")
    win.buttons["restore"].click()
    settle()
    assert win._password_error.text() == "This backup couldn’t be opened."
    assert win.buttons["back"].isEnabled()


def test_a_backup_too_costly_to_open_is_explained(tmp_path):
    salt, nonce, aad = bytes(16), bytes(24), bytes([nip49.KEY_UNKNOWN])
    payload = (bytes([nip49.VERSION, 21]) + salt + nonce + aad
               + nip49._xchacha_seal(bytes(32), nonce, SK, aad))
    text = bech32.bech32_encode(nip49.HRP, bech32.convertbits(list(payload), 8, 5))
    win, *_ = make_restore(tmp_path, text)
    win.buttons["choose"].click()
    win._backup_password.setText("anything")
    win.buttons["restore"].click()
    settle()
    assert "memory" in win._password_error.text()


def test_a_pasted_key_restores_after_the_warning_is_shown(tmp_path):
    win, store, vault, ready = make_restore(tmp_path)
    win._paste_link.click()
    assert "apps you trust" in all_text(win)
    assert win._paste_edit.echoMode() == QLineEdit.Password
    win._paste_edit.setText(NSEC)
    win.buttons["continue"].click()
    assert vault.load(PK) == SK and win._paste_edit.text() == ""


def test_a_pasted_key_that_cant_be_saved_is_explained_and_cleared(tmp_path):
    win, store, _vault, ready = make_restore(tmp_path, vault=BrokenVault(tmp_path / "k.json"))
    win._paste_link.click()
    win._paste_edit.setText(NSEC)
    win.buttons["continue"].click()
    assert win.page == OPEN and "couldn’t be saved" in win._open_error.text()
    assert win._paste_edit.text() == "" and ready == [] and store.get(PK) is None


def test_a_pasted_field_is_cleared_even_when_reading_it_fails(tmp_path, monkeypatch):
    win, *_ = make_restore(tmp_path)

    def broken(*_args, **_kw):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(aw, "find_key", broken)
    win._paste_link.click()
    win._paste_edit.setText(NSEC)
    with pytest.raises(RuntimeError):
        win._use_paste()
    assert win._paste_edit.text() == ""


def test_a_known_public_key_pasted_as_hex_is_explained(tmp_path):
    win, store, vault, ready = make_restore(tmp_path)
    store.upsert(Profile(user_pubkey=PK, bunker_pubkey="b" * 64, bunker_relays=[],
                         local_secret_hex="c" * 64))
    win._paste_link.click()
    win._paste_edit.setText(PK)
    win.buttons["continue"].click()
    assert "public key" in win._open_error.text() and ready == []


def test_a_file_without_a_key_is_explained(tmp_path):
    win, *_ = make_restore(tmp_path, "nothing to see here")
    win.buttons["choose"].click()
    assert win.page == OPEN and "No private key" in win._open_error.text()


def test_restoring_an_account_already_here_keeps_what_was_learned(tmp_path):
    win, store, _vault, _ready = make_restore(tmp_path)
    store.upsert(Profile(user_pubkey=PK, bunker_pubkey="b" * 64, bunker_relays=[],
                         local_secret_hex="c" * 64, display_name="Satoshi",
                         avatar_path="/cache/a.png", metadata_cached_at=1_700_000_000))
    win._paste_link.click()
    win._paste_edit.setText(NSEC)
    win.buttons["continue"].click()
    kept = store.get(PK)
    assert kept.display_name == "Satoshi" and kept.is_local
    assert kept.avatar_path == "/cache/a.png" and kept.metadata_cached_at == 1_700_000_000


def test_pasting_can_be_undone_back_to_the_backup_file(tmp_path):
    win, *_ = make_restore(tmp_path)
    win._paste_link.click()
    win._paste_edit.setText(NSEC)
    win._hide_paste()
    assert win._paste_box.isHidden() and win._paste_edit.text() == ""
    assert "choose" in win.buttons


def test_an_account_kept_here_can_be_backed_up_any_time(tmp_path):
    files = Files(tmp_path)
    win = aw.BackupAccountWindow(secret=SK, is_dark=False, choose_path=files.choose,
                                 write_file=files.write)
    assert bech32.encode_npub(PK)[:12] in all_text(win) and NSEC not in all_text(win)
    win.backup_form.password.setText("correct horse")
    win.backup_form.confirm.setText("correct horse")
    win.backup_form.save_button.click()
    settle()
    assert files.opens_with("correct horse") == SK
    assert "Backup saved" in win.backup_form.note.text()
