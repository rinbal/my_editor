# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins Edit Profile.

What must hold:

  The profile is read fresh before anything can be edited. A profile that
  could not be read is never edited (it could be replaced); one the
  network clearly does not have can be created.

  Only the fields that were changed are saved; an emptied field is
  removed. Every other field of the profile is kept by the writer, which
  test_profile_editing pins against a real writer.

  A field with a problem says so under itself, and Save waits until
  something changed and nothing is wrong.

  A failed save keeps what was typed and says what happened.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.outbox.lookup import Lookup  # noqa: E402
from nostr.outbox.policy import LookupState  # noqa: E402
from nostr.outbox import writer as outbox_writer  # noqa: E402
from nostr.ui import profile_window as pw  # noqa: E402
from nostr.ui.profile_window import FIELDS, FORM, PROBLEM, READING, SAVING  # noqa: E402
from nostr.bech32 import encode_nsec  # noqa: E402

NSEC = encode_nsec("11" * 32)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def found(**fields):
    return Lookup(LookupState.FOUND, event={"content": json.dumps(fields)})


class Network:
    def __init__(self):
        self.reads = []
        self.saves = []

    def read(self, on_done):
        self.reads.append(on_done)

    def save(self, changes, on_done):
        self.saves.append((changes, on_done))


def window(**kw):
    net = Network()
    win = pw.ProfileWindow(read=net.read, save=net.save, is_dark=False, **kw)
    return win, net


def edit(win, key, text):
    widget = win._edits[key]
    if hasattr(widget, "setPlainText"):
        widget.setPlainText(text)
    else:
        widget.setText(text)


def test_the_profile_is_read_before_anything_can_be_edited():
    win, net = window()
    assert win.page == READING and len(net.reads) == 1
    net.reads[0](found(name="alice", display_name="Alice", about="Hi",
                       lud16="alice@wallet.example"))
    assert win.page == FORM
    assert win._edits["display_name"].text() == "Alice"
    assert win._edits["about"].toPlainText() == "Hi"
    assert not win.buttons["save"].isEnabled()             # nothing changed yet


def test_a_profile_that_could_not_be_read_is_not_edited():
    win, net = window()
    net.reads[0](Lookup(LookupState.UNKNOWN))
    assert win.page == PROBLEM
    # Lessons 36: what did not happen, one sentence, why it is not edited
    # unread in the tooltip, Try Again beside Close.
    assert win._problem_title.text() == "Your Profile Couldn’t Be Read"
    assert win._problem_text.text() == "Check your internet connection and try again."
    assert "replace it" in win._problem_text.toolTip()
    assert "Try Again" in [b.text() for b in win.buttons.values()]
    win.buttons["retry"].click()
    assert win.page == READING and len(net.reads) == 2


def test_a_profile_that_is_not_an_object_is_left_alone():
    win, net = window()
    net.reads[0](Lookup(LookupState.FOUND, event={"content": "[1, 2]"}))
    assert win.page == PROBLEM and "retry" not in win.buttons


def test_no_profile_yet_starts_an_empty_one():
    win, net = window()
    net.reads[0](Lookup(LookupState.ABSENT))
    assert win.page == FORM
    assert "don’t have a profile yet" in win._form_note.text()


def test_only_changed_fields_are_saved_and_an_emptied_one_is_removed():
    win, net = window()
    net.reads[0](found(name="alice", display_name="Alice", website="https://a.example"))
    edit(win, "display_name", "Alice B")
    edit(win, "website", "")
    assert win.changes() == {"display_name": "Alice B", "website": ""}
    win.buttons["save"].click()
    assert win.page == SAVING
    assert net.saves[0][0] == {"display_name": "Alice B", "website": ""}


def test_the_older_display_name_key_is_shown():
    win, net = window()
    net.reads[0](found(displayName="Old Style"))
    assert win._edits["display_name"].text() == "Old Style"


@pytest.mark.parametrize("key, value, problem", [
    ("picture", "http://cdn.example/a.png", "beginning with https"),
    ("banner", "not a link", "beginning with https"),
    ("website", "example.com", "beginning with http or https"),
    ("lud16", "alice", "Lightning address"),
    ("nip05", "alice@", "Nostr address"),
    ("name", "two words", "spaces"),
    ("display_name", "Alice " + NSEC, "secret key"),
    ("about", "My key: " + NSEC, "secret key"),
])
def test_a_field_with_a_problem_says_so_and_blocks_saving(key, value, problem):
    win, net = window()
    net.reads[0](found(name="alice"))
    edit(win, key, value)
    error = win._errors[key]
    assert not error.isHidden() and problem in error.text()
    assert not win.buttons["save"].isEnabled()


def test_a_key_in_a_part_not_shown_is_left_out_and_said_before_saving(monkeypatch):
    """A key already in the profile, in a part the window doesn't show
    (another app's "bio", the older "displayName"): saving without it is
    what takes it out, so Save is offered, and the person is told."""
    import announce
    from nostr.outbox.policy import merge_profile_content
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    win, net = window()
    content = {"name": "alice", "bio": "My key: " + NSEC, "displayName": NSEC,
               "display_name": "Alice"}
    net.reads[0](found(**content))
    note = win._form_note.text()
    assert "“bio”" in note and "“displayName”" in note and "secret key" in note
    assert heard == [note]                                         # said once, on opening
    assert win.buttons["save"].isEnabled()
    win._save_changes()
    changes = net.saves[0][0]
    assert changes == {"bio": "", "displayName": ""}
    merged = merge_profile_content(json.dumps(content), changes)
    assert NSEC not in merged and json.loads(merged)["name"] == "alice"


def test_a_key_added_elsewhere_since_reading_is_said_with_what_to_do():
    """The signer refused: a key reached a part the window doesn't show
    after it read the profile. Trying again here can't help; opening Edit
    Profile again offers to leave that part out."""
    from nostr import refs
    from i18n import _
    win, net = window()
    net.reads[0](found(name="alice"))
    edit(win, "name", "alicia")
    win._save_changes()
    net.saves[0][1](SimpleNamespace(status=outbox_writer.FAILED, reason=_(refs.SECRET_KEY_REFUSED)))
    note = win._form_note.accessibleName()
    assert note == ("Your profile wasn’t saved. Since it was read, a secret key was added to a "
                    "part this window doesn’t show: close this window and open Edit Profile "
                    "again to leave that part out.")
    assert "try again" not in note.lower()
    # No try here can help: no Try Again, no Save; Cancel is the way out.
    assert set(win.buttons) == {"cancel"} and win.buttons["cancel"].isDefault()


def test_a_field_problem_is_read_with_the_field_and_said_once(monkeypatch):
    import announce
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    win, net = window()
    win.show()                                          # heard only from a shown window
    net.reads[0](found(name="alice"))
    edit(win, "about", "My key: " + NSEC)
    edit(win, "about", "My key: " + NSEC + " ")        # typing on: said once
    problem = win._errors["about"].text()
    assert win._edits["about"].accessibleDescription() == problem
    assert heard == [problem]
    edit(win, "about", "Hello")
    assert win._edits["about"].accessibleDescription() == "" and win._errors["about"].isHidden()
    win.close()


@pytest.mark.parametrize("key, value", [
    ("picture", "https://cdn.example/a.png"),
    ("website", "http://old.example"),
    ("lud16", "alice@wallet.example"),
    ("nip05", "example.com"),
    ("nip05", "alice@example.com"),
])
def test_good_values_pass(key, value):
    field = next(f for f in FIELDS if f.key == key)
    assert pw.field_problem(field, value) is None


def test_a_saved_profile_closes_the_window_and_says_so():
    win, net = window()
    saved = []
    win.saved.connect(lambda: saved.append(True))
    net.reads[0](found(name="alice"))
    edit(win, "about", "New about")
    win.buttons["save"].click()
    net.saves[0][1](outbox_writer.WriteOutcome(outbox_writer.WRITTEN))
    assert saved == [True] and win.result() == win.DialogCode.Accepted


def test_a_failed_save_keeps_what_was_typed():
    win, net = window()
    net.reads[0](found(name="alice"))
    edit(win, "about", "New about")
    win.buttons["save"].click()
    net.saves[0][1](outbox_writer.WriteOutcome(outbox_writer.FAILED, reason="no"))
    assert win.page == FORM
    assert win._edits["about"].toPlainText() == "New about"
    assert "wasn’t saved" in win._form_note.text()
    assert win.buttons["save"].isEnabled()


@pytest.mark.parametrize("outcome, sentence", [
    (outbox_writer.WriteOutcome(outbox_writer.UNKNOWN_BASE, cause=outbox_writer.CAUSE_RELAYS,
                                reason="The current one couldn’t be read."),
     "Check your internet connection and try again."),
    (outbox_writer.WriteOutcome(outbox_writer.FAILED, cause=outbox_writer.CAUSE_RELAYS,
                                reason="Too few relays accepted it.",
                                refused=(("wss://a.example", "blocked: paid relay"),)),
     "The servers your posts go to turned it down: try again later."),
    (outbox_writer.WriteOutcome(outbox_writer.FAILED, cause=outbox_writer.CAUSE_SIGNER,
                                reason="user rejected"),
     "Your signer app declined the request: allow it there and try again."),
])
def test_a_failed_save_is_said_once_by_its_cause_and_try_again_saves_again(
        monkeypatch, outcome, sentence):
    # Lessons 36: what did not happen, the one sentence of its cause (no
    # longer the signer "or" the servers), what was said in the tooltip,
    # and Save reads Try Again.
    import announce
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    win, net = window()
    win.show()
    net.reads[0](found(name="alice"))
    edit(win, "about", "New about")
    win.buttons["save"].click()
    net.saves[0][1](outcome)
    said = f"Your profile wasn’t saved. {sentence}"
    assert win._form_note.text() == f"<b>Your profile wasn’t saved</b><br>{sentence}"
    assert win._form_note.accessibleName() == said and heard == [said]
    assert win._form_note.toolTip().startswith(outcome.reason)
    assert "wss://" not in win._form_note.text()
    assert win.buttons["save"].text() == "Try Again" and win.buttons["save"].isDefault()
    win.buttons["save"].click()
    assert len(net.saves) == 2 and net.saves[1][0] == net.saves[0][0]   # the same changes
    win.close()


def test_a_signer_account_is_told_to_look_at_the_signer():
    win, net = window()
    net.reads[0](found(name="alice"))
    edit(win, "about", "x")
    win.buttons["save"].click()
    assert "signer app" in win._saving_label.text()
    local, net2 = window(signs_locally=True)
    net2.reads[0](found(name="alice"))
    edit(local, "about", "x")
    local.buttons["save"].click()
    assert "signer" not in local._saving_label.text()


def test_an_answer_after_closing_is_dropped():
    win, net = window()
    win.reject()
    net.reads[0](found(name="alice"))         # nothing happens, nothing raises
    assert win.page == READING


# -- the menu entry --------------------------------------------------------------------------

def test_edit_profile_opens_a_window_that_reads_the_active_account(monkeypatch):
    import main_window as mw_module
    from types import SimpleNamespace
    from PySide6.QtWidgets import QMainWindow

    reads = []

    class FakeEditing(QObject):
        def __init__(self, *, profile, **_kw):
            super().__init__(_kw.get("parent"))
            self.profile = profile

        def read(self, done):
            reads.append(self.profile)

        def save(self, changes, done):
            pass

    monkeypatch.setattr(mw_module, "ProfileEditing", FakeEditing)
    profile = SimpleNamespace(user_pubkey="ab" * 32, is_local=True)
    host = QMainWindow()
    host._profile_store = SimpleNamespace(default=lambda: profile)
    host._relay_pool = host._session_pool = host._relay_directory = None
    host._media_store = None                      # no media servers: no Choose…
    host._profile_image_loader = lambda _pubkey: None
    host.is_dark_theme = False
    mw_module.MainWindow._on_edit_profile(host)
    windows = host.findChildren(pw.ProfileWindow)
    assert len(windows) == 1 and windows[0].isVisible()
    assert reads == [profile]
    # Leave nothing behind for the tests that run after this one.
    from PySide6.QtCore import QCoreApplication, QEvent
    windows[0].close()
    host.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_the_form_reads_like_every_other_form_in_the_app(qt_app):
    """Labels right-aligned with a colon, as in Create Account, Restore,
    Settings and the membership application; a screen reader gets the
    words without the colon."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFormLayout, QLabel
    from tests.accessibility import unnamed_controls
    win = pw.ProfileWindow(read=lambda _done: None, save=lambda _c, _d: None, is_dark=False)
    form, = win._pages["form"].findChildren(QFormLayout)
    assert form.labelAlignment() & Qt.AlignRight
    labels = [w.text() for w in win._pages["form"].findChildren(QLabel)
              if w.text().endswith(":")]
    assert "Lightning address:" in labels and "Nostr address:" in labels
    assert win._edits["lud16"].accessibleName() == "Lightning address"
    assert unnamed_controls(win) == []
    win.close()


OVERLAP_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import i18n
i18n.install(sys.argv[2])
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from nostr.bech32 import encode_nsec
from nostr.outbox.lookup import Lookup
from nostr.outbox.policy import LookupState
from nostr.ui import profile_window as pw
reads = []
win = pw.ProfileWindow(read=reads.append, save=lambda c, d: None, is_dark=False)
win.show()
reads[0](Lookup(LookupState.FOUND, event={"content": json.dumps({"name": "alice"})}))
app.processEvents()
win._edits["about"].setPlainText("My key: " + encode_nsec("11" * 32))
for _ in range(3):
    app.processEvents()
last, save = win._edits["nip05"], win.buttons["save"]
r = {"last_bottom": last.mapTo(win, QPoint(0, last.height())).y(),
     "save_top": save.mapTo(win, QPoint(0, 0)).y()}
win.close()
print("RESULT " + json.dumps(r))
"""


@pytest.mark.parametrize("language", ["en", "de", "pseudo"])
def test_a_field_problem_never_lays_the_form_over_the_buttons(tmp_path, language):
    # Review of the merge: in the 40 percent longer test language, a secret
    # key in About widened the page and added the problem's lines at once;
    # the window kept its height, and the last field ran under Save. (In a
    # process of its own: the language is chosen before any window is made.)
    from tests.app_process import run_window_script
    r = run_window_script(OVERLAP_SCRIPT, tmp_path, language)
    assert r["last_bottom"] <= r["save_top"], r


# -- the card, the groups and Choose… ------------------------------------------

PIC = "https://media.example/pic.jpg"
WIDE = "https://media.example/wide.png"


def _pixmap(color):
    from PySide6.QtGui import QColor, QPixmap
    pixmap = QPixmap(40, 40)
    pixmap.fill(QColor(color))
    return pixmap


def test_the_card_shows_the_profile_as_the_fields_read():
    win, net = window()
    net.reads[0](found(display_name="Ann Bell", name="ann", nip05="ann@example.com"))
    assert (win.card.name(), win.card.detail()) == ("Ann Bell", "ann@example.com")
    edit(win, "nip05", "")
    assert win.card.detail() == "@ann"           # no Nostr address: the username
    edit(win, "display_name", "")
    assert win.card.name() == "ann"              # no name: the username
    win.close()


def test_the_cards_pictures_are_loaded_for_the_addresses_given_and_follow_a_change():
    asked = []
    win, net = window(load_image=lambda kind, url, done: asked.append((kind, url, done)))
    net.reads[0](found(name="ann", picture=PIC, banner=WIDE))
    win._image_timer.timeout.emit()
    assert [(kind, url) for kind, url, _done in asked] == [("picture", PIC), ("banner", WIDE)]
    red, blue = _pixmap("#FF0000"), _pixmap("#0000FF")
    asked[0][2](red)
    asked[1][2](blue)
    assert win.card._picture is red and win.card._banner is blue
    # An address typed over: the answer for the old one no longer lands.
    edit(win, "picture", "https://media.example/new.jpg")
    win._image_timer.timeout.emit()
    asked[0][2](blue)
    assert win.card._picture is not blue
    edit(win, "banner", "")
    win._image_timer.timeout.emit()
    assert win.card._banner is None
    win.close()


def test_an_image_answer_after_closing_is_dropped():
    asked = []
    win, net = window(load_image=lambda kind, url, done: asked.append(done))
    net.reads[0](found(name="ann", picture=PIC))
    win._image_timer.timeout.emit()
    win.reject()                                 # closed, as Cancel or the close box does
    asked[0](_pixmap("#FF0000"))                 # nothing happens, nothing breaks
    assert win.card._picture is None


def test_a_long_address_shows_where_it_lives():
    win, net = window()
    net.reads[0](found(name="ann", picture=PIC + "?" + "x" * 200))
    assert win._edits["picture"].cursorPosition() == 0
    win.close()


def test_the_fields_come_in_groups_titled_in_order():
    from PySide6.QtWidgets import QLabel
    win, _net = window()
    titles = [label.text() for label in win._pages["form"].findChildren(QLabel)
              if label.objectName() == "item_title"]
    assert titles == ["Pictures", "Links and Payments"]   # the first, under the card
    win.close()


def test_choose_is_offered_with_the_media_library_and_fills_in_the_public_address(
        monkeypatch):
    from nostr.ui import cover_field
    win, _net = window()
    assert win._choose == {}                     # no media servers: the address only
    win.close()

    picked = SimpleNamespace(url=PIC)

    class Picker:
        def __init__(self, **_kwargs):
            self._filter_combo = SimpleNamespace(setCurrentIndex=lambda *_a: None)
            self.file_picked = SimpleNamespace(connect=lambda slot: setattr(self, "slot", slot))

        def setWindowTitle(self, title):
            self.title = title

        def exec(self):
            self.slot(picked)

    monkeypatch.setattr(cover_field, "MediaLibraryDialog", Picker)
    monkeypatch.setattr(pw, "resolve_pick", lambda media, **_kw: SimpleNamespace(
        ok=True, url=media.url, reason=""))
    services = cover_field.MediaServices(store=SimpleNamespace())
    win, net = window(media=services)
    net.reads[0](found(name="ann"))
    assert set(win._choose) == {"picture", "banner"}
    assert win._choose["banner"].accessibleName() == "Choose Banner"
    win._choose["picture"].click()
    assert win._edits["picture"].text() == PIC and "picture" in win.changes()
    win.close()


def test_a_private_picture_that_stays_private_says_why_under_its_field(monkeypatch):
    from nostr.ui import cover_field
    monkeypatch.setattr(pw, "choose_picture",
                        lambda services, *, on_picked, **_kw: on_picked(SimpleNamespace()))
    monkeypatch.setattr(pw, "resolve_pick", lambda media, **_kw: SimpleNamespace(
        ok=False, url="", reason="This picture is kept privately."))
    win, net = window(media=cover_field.MediaServices(store=SimpleNamespace()))
    net.reads[0](found(name="ann"))
    win._choose["banner"].click()
    assert win._edits["banner"].text() == ""
    assert win._errors["banner"].text() == "This picture is kept privately."
    win.close()
