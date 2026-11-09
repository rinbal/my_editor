# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the guard against Nostr secret keys in what is written
(nostrdesign.org, "private key safeguarding").

What must hold:

  A paste or drop with a secret key in it, into prose while a Nostr
  account is in use, is stopped with a warning; the person may paste the
  public key (the link to the key's owner) instead, or nothing. Without
  an account, and in plain text and code files, a paste is never stopped.

  Nothing is published with a secret key in it. The note or article is
  looked at last, as the event that would be signed: its text (link
  addresses too), title, summary and every tag, whatever was typed into
  the publish dialog. The person may publish it with each key replaced by
  its owner's public key, or not at all; an encrypted key has no public
  key to offer and must be removed. A key in an article's or a draft's
  slug is refused: the slug is published as it is and never rewritten.
  Edit Profile shows a key in a field as that field's problem.

  Underneath, both signers (a key kept here, a signer app) refuse any
  event with a key in its content or tags, so no other way gets one out.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from editor import HtmlEditor  # noqa: E402
from nostr import bech32, refs  # noqa: E402
from nostr.crypto import get_public_key  # noqa: E402
from nostr.events import compute_event_id  # noqa: E402
from nostr.publisher import PublishJob, build_article, build_note, without_secret_keys  # noqa: E402
from nostr.ui import secret_key_check  # noqa: E402
from tests.app_process import run_window_script  # noqa: E402
from tests.outbox_fakes import PK, settle  # noqa: E402
from tests.test_publish_job import _dialog  # noqa: E402

SECRET = "11" * 32
NSEC = bech32.encode_nsec(SECRET)
OWNER = "nostr:" + bech32.encode_npub(get_public_key(bytes.fromhex(SECRET)).hex())


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def text(words: str) -> QMimeData:
    mime = QMimeData()
    mime.setText(words)
    return mime


def test_a_plain_key_has_its_owner_and_an_encrypted_one_has_none():
    assert refs.public_reference_for(NSEC) == OWNER
    assert refs.without_secret_keys(f"my key {NSEC}.") == f"my key {OWNER}."
    assert refs.without_secret_keys(f"[it](nostr:{NSEC})") == f"[it]({OWNER})"


@pytest.mark.parametrize("text", [f"my backup _{NSEC}_ here", f"backup_{NSEC}",
                                  f"my_{NSEC}", f"({NSEC})", f"nostr:{NSEC}"])
def test_a_key_is_found_after_any_character_but_a_letter_or_digit(text):
    assert refs.secret_keys_in(text) == [NSEC]


def test_a_key_inside_a_longer_word_is_not_one():
    assert refs.secret_keys_in(f"x{NSEC}") == [] and refs.secret_keys_in(f"9{NSEC}") == []
    assert refs.without_secret_keys(f"{NSEC} ncryptsec1{'q' * 40}") is None


@pytest.mark.parametrize("answer, shown", [(None, ""), ("other words", "other words"),
                                           (f"key {NSEC}", f"key {NSEC}")])
def test_the_guard_decides_what_a_paste_brings(answer, shown):
    ed = HtmlEditor()
    asked = []

    def guard(words):
        asked.append(words)
        return answer if answer != f"key {NSEC}" else words
    ed.set_paste_guard(guard)
    ed.insertFromMimeData(text(f"key {NSEC}"))
    assert asked == [f"key {NSEC}"]
    assert ed.toPlainText() == shown


SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr import bech32

nsec = bech32.encode_nsec("11" * 32)
asked = []
answers = []
def fake_ask(parent, **kw):
    asked.append({"title": kw.get("title"),
                  "buttons": [b.label for b in kw.get("buttons", ())]})
    return answers.pop(0)
main_window.ask = fake_ask
main_window.inform = lambda parent, **kw: asked.append({"title": kw.get("title"),
                                                        "buttons": ["OK"]})


def paste(ed, words):
    mime = QMimeData()
    mime.setText(words)
    ed.insertFromMimeData(mime)


w = main_window.MainWindow()
w.show()
ed = w.new_tab()
app.processEvents()
r = {}
paste(ed, f"no account {nsec}")
r["without_account"] = [ed.toPlainText(), len(asked)]
ed.selectAll()
ed.textCursor().removeSelectedText()
w.nostr_state._active = True
answers.append(None)                                  # Don't Paste
paste(ed, f"mine {nsec}")
r["refused"] = [ed.toPlainText(), asked[-1]]
public = "nostr:" + bech32.encode_npub(
    __import__("nostr.crypto", fromlist=["x"]).get_public_key(bytes.fromhex("11" * 32)).hex())
answers.append(f"mine {public}")                     # Paste Public Key
paste(ed, f"mine {nsec}")
r["public"] = ed.toPlainText()
txt = os.path.join(os.environ["HOME"], "keys.txt")
with open(txt, "w", encoding="utf-8") as f:
    f.write("")
plain = w.open_path(txt)
before = len(asked)
paste(plain, nsec)
r["plain_file"] = [plain.toPlainText() == nsec, len(asked) == before]
for e in (ed, plain):
    e.document().setModified(False)
w.close()
print("RESULT " + json.dumps({**r, "public_ref": public}))
"""


def test_secret_keys_in_the_window(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    nsec = NSEC
    assert r["without_account"] == [f"no account {nsec}", 0]
    assert r["refused"][0] == ""
    assert r["refused"][1]["title"] == "This is a secret key"
    assert r["refused"][1]["buttons"] == ["Paste Public Key", "Don't Paste"]
    assert r["public"] == f"mine {r['public_ref']}"
    assert r["plain_file"] == [True, True]


# -- publishing: the event itself is looked at last ----------------------------

def test_a_key_is_found_in_the_text_a_link_address_and_every_tag():
    assert refs.secret_keys_in_event(build_note("Nothing to hide.", PK)) == []
    linked = build_note(f"My backup (nostr:{NSEC}) is here.", PK)
    assert refs.secret_keys_in_event(linked) == [NSEC]
    article = build_article("Body.", PK, "post", title=f"Title {NSEC}",
                            extra_tags=[["alt", f"see {NSEC}"]])
    assert refs.secret_keys_in_event(article) == [NSEC]


def test_replacing_keys_builds_the_event_again_with_an_id_that_fits():
    unsigned = build_article(f"[backup](nostr:{NSEC})", PK, "post", title=f"Mine {NSEC}")
    public = without_secret_keys(unsigned)
    assert public["content"] == f"[backup]({OWNER})"
    assert ["title", f"Mine {OWNER}"] in public["tags"]
    assert refs.secret_keys_in_event(public) == []
    assert public["id"] == compute_event_id(public["pubkey"], public["created_at"],
                                            public["kind"], public["tags"], public["content"])
    assert public["created_at"] == unsigned["created_at"] and public["kind"] == 30023
    assert without_secret_keys(build_note("ncryptsec1" + "q" * 40, PK)) is None


def _sign_locally(event):
    from nostr.local_signer import LocalSigner
    signer = LocalSigner(bytes.fromhex(SECRET))
    outcome = {}
    signer.sign_event(event, lambda signed: outcome.update(signed=signed),
                      lambda reason: outcome.update(refused=reason))
    settle()
    return outcome


@pytest.mark.parametrize("event", [
    {"kind": 0, "content": json.dumps({"about": f"My key: {NSEC}"}), "tags": []},
    {"kind": 31234, "content": "c2VjcmV0", "tags": [["d", f"backup-{NSEC}"], ["k", "30023"]]},
    {"kind": 1, "content": "ncryptsec1" + "q" * 60, "tags": []},
])
def test_a_signer_never_signs_an_event_with_a_key_in_it(event):
    """The last line, where every event is signed: a profile, a draft's
    plain slug, an encrypted key published as text."""
    event = {**event, "created_at": 1_800_000_000}
    assert _sign_locally(event) == {"refused": refs.SECRET_KEY_REFUSED}


@pytest.mark.parametrize("about", [f"My key:\n{NSEC}", f"My key:\t{NSEC}", f"\r\n{NSEC}",
                                   f"Caf\u00e9{NSEC}"])
def test_a_key_behind_a_json_escape_is_found(about):
    """A profile's JSON writes a line break before a key as \\n, and with
    ensure_ascii a letter as \\uXXXX: the strings are read, not the text."""
    for ascii_only in (False, True):
        event = {"kind": 0, "created_at": 1_800_000_000, "tags": [],
                 "content": json.dumps({"about": about, "more": [{"deep": about}]},
                                       ensure_ascii=ascii_only)}
        assert refs.secret_keys_in_event(event) == [NSEC]
        assert _sign_locally(event) == {"refused": refs.SECRET_KEY_REFUSED}


@pytest.mark.parametrize("content", [
    "[" * 100_000 + json.dumps(f"My key:\n{NSEC}") + "]" * 100_000,   # deeper than JSON reads
    json.dumps({"about": "x" * (300 * 1024)}),                         # longer than is read
], ids=["too-deep", "too-long"])      # the text itself is no name: Windows caps the variable
def test_json_that_cant_be_read_in_full_is_never_signed(content):
    """No falling back to the raw text, where an escape could hide a key:
    what can't be read can't be cleared, so it is refused, in words."""
    event = {"kind": 0, "created_at": 1_800_000_000, "tags": [], "content": content}
    assert refs.secret_keys_in_event(event) is None
    assert _sign_locally(event) == {"refused": refs.UNREADABLE_REFUSED}


def test_a_key_deep_in_json_that_can_be_read_is_found():
    event = {"kind": 0, "created_at": 1_800_000_000, "tags": [],
             "content": "[" * 900 + json.dumps(f"My key:\n{NSEC}") + "]" * 900}
    assert refs.secret_keys_in_event(event) == [NSEC]
    assert _sign_locally(event) == {"refused": refs.SECRET_KEY_REFUSED}


def test_plain_text_of_any_length_and_wide_json_are_read_quickly():
    import time
    long_article = {"kind": 30023, "created_at": 1, "tags": [["d", "x"]],
                    "content": "Words. " * 60_000}                 # 420 KB, not JSON
    assert refs.secret_keys_in_event(long_article) == []
    wide = {"kind": 0, "created_at": 1, "tags": [],
            "content": "[" * 900 + ",".join(['"a"'] * 25_000) + "]" * 900}
    started = time.perf_counter()
    assert refs.secret_keys_in_event(wide) == []
    assert time.perf_counter() - started < 0.5                    # no walk by recursion


def test_an_encrypted_draft_is_signed():
    from nostr import crypto
    secret = bytes.fromhex(SECRET)
    me = get_public_key(secret)
    sealed = crypto.encrypt_to(f"my notes {NSEC}", secret, me)     # ciphertext only
    outcome = _sign_locally({"kind": 31234, "content": sealed, "created_at": 1_800_000_000,
                             "tags": [["d", "my-post"], ["k", "30023"]]})
    assert "signed" in outcome and outcome["signed"]["content"] == sealed


def test_an_article_whose_address_holds_a_key_is_deleted_by_its_id_alone():
    from nostr.deletion import build_deletion
    me = get_public_key(bytes.fromhex(SECRET)).hex()
    request = build_deletion(me, addresses=[f"30023:{me}:backup-{NSEC}"],
                             event_ids=["ab" * 32], kinds=[30023],
                             created_at=1_800_000_000)
    assert request["tags"] == [["e", "ab" * 32], ["k", "30023"]]
    assert "signed" in _sign_locally(request)


def _delete_draft(identifier, wrap_id):
    from unittest.mock import MagicMock
    from nostr.local_signer import LocalSigner
    from nostr.publisher import DraftDeleteJob
    from tests.outbox_fakes import FakePool, FakeRelayDirectory
    signer = LocalSigner(bytes.fromhex(SECRET))
    session = MagicMock()
    session.get.side_effect = lambda profile, on_ready, on_error: on_ready(signer)
    pool = FakePool()
    job = DraftDeleteJob(relay_pool=pool, relay_directory=FakeRelayDirectory(),
                         session_pool=session,
                         profile=MagicMock(user_pubkey=signer.user_pubkey),
                         identifier=identifier, inner_kind=30023, replaced_wrap_id=wrap_id)
    failed = []
    job.failed.connect(failed.append)
    job.start()
    settle()
    return [event for _urls, event in pool.published], failed


def test_a_draft_whose_slug_holds_a_key_is_deleted_by_its_wrap_id_alone():
    """A tombstone would send the key again as its d tag: the wrap is
    deleted by its id, with a request that names no address."""
    sent, failed = _delete_draft(f"backup-{NSEC}", "cd" * 32)
    assert failed == [] and len(sent) == 1
    assert sent[0]["kind"] == 5 and sent[0]["tags"] == [["e", "cd" * 32], ["k", "31234"]]
    assert refs.secret_keys_in_event(sent[0]) == []
    sent, failed = _delete_draft(f"backup-{NSEC}", "")           # its wrap unknown here
    assert sent == [] and failed and "secret key" in failed[0]
    sent, failed = _delete_draft("backup", "")                   # an ordinary draft: tombstone
    assert failed == [] and [event["kind"] for event in sent] == [31234]


def test_a_key_is_never_sent_to_a_search_relay(tmp_path):
    from nostr.known_people import KnownPeople
    from nostr.search import Nip50SearchClient
    from tests.outbox_fakes import HandPool
    pool = HandPool()
    client = Nip50SearchClient(pool, KnownPeople(tmp_path / "people.json"))
    found = []
    client.results.connect(lambda query, people: found.append(people))
    client.search(f"  {NSEC} ")
    assert pool.subs == [] and found == [[]]
    client.search("alice")
    assert len(pool.subs) == 1 and pool.subs[0].filters[0]["search"] == "alice"


def test_the_people_search_says_why_a_key_is_not_searched(tmp_path, monkeypatch):
    import announce
    from PySide6.QtCore import QRect
    from nostr.known_people import KnownPeople
    from nostr.search import Nip50SearchClient
    from nostr.ui.mention_picker import MentionPicker
    from tests.outbox_fakes import HandPool
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    pool = HandPool()
    people = KnownPeople(tmp_path / "people.json")
    picker = MentionPicker(people, Nip50SearchClient(pool, people), is_dark=False)
    picker.open_at(QRect(0, 0, 1, 1), picker)
    picker._search_edit.setText(NSEC)                         # pasted
    picker._run_search()                                      # the debounce firing
    problem = picker._problem.text()
    assert "secret key" in problem and not picker._problem.isHidden()
    assert picker._search_edit.accessibleDescription() == problem and heard == [problem]
    assert pool.subs == [] and picker._hint.isHidden()           # no "Keep typing" too
    picker._search_edit.setText("ali")
    assert picker._problem.isHidden()
    picker.hide()


def test_a_signer_app_is_never_asked_to_sign_a_key():
    from nostr.bunker import BunkerClient
    from tests.outbox_fakes import FakePool
    client = BunkerClient(FakePool())
    client._is_connected = True
    sent, refused = [], []
    client._send_request = lambda **kw: sent.append(kw)
    client.sign_event({"kind": 0, "content": f"{{\"name\": \"{NSEC}\"}}", "tags": [],
                       "created_at": 1_800_000_000}, lambda signed: None, refused.append)
    # The refusal comes on the next turn of the event loop, like any
    # answer (R6), and nothing is sent meanwhile.
    assert refused == [] and sent == []
    QApplication.processEvents()
    assert refused == [refs.SECRET_KEY_REFUSED] and sent == []
    client.sign_event({"kind": 1, "content": "hello", "tags": [], "created_at": 1_800_000_000},
                      lambda signed: None, refused.append)
    assert len(sent) == 1


@pytest.fixture
def answers(monkeypatch):
    """The alerts of the last check, answered from a list; what they
    asked, in order."""
    queue, asked = [], []

    def ask(parent, **kw):
        asked.append([b.label for b in kw["buttons"]])
        return queue.pop(0)

    monkeypatch.setattr(secret_key_check, "ask", ask)
    monkeypatch.setattr(secret_key_check, "inform",
                        lambda parent, **kw: asked.append(["OK"]))
    return queue, asked


def test_a_key_typed_into_the_note_dialog_is_not_published(tmp_path, answers):
    queue, asked = answers
    dialog = _dialog("note", tmp_path, None)
    dialog._text_edit.setPlainText(f"A harmless note {NSEC}")
    queue.append(False)                                        # Cancel
    dialog._on_publish()
    assert asked == [["Cancel", "Replace with Public Key"]]
    assert dialog._job is None and dialog._publish_btn.isEnabled()
    queue.append(True)                                         # Replace with Public Key
    dialog._on_publish()
    settle()
    published = dialog._relay_pool.published
    assert published and published[-1][1]["content"] == f"A harmless note {OWNER}"


def test_a_key_in_any_field_of_the_article_dialog_is_not_published(tmp_path, answers):
    queue, asked = answers
    dialog = _dialog("article", tmp_path, None)
    dialog._title_edit.setText(f"About {NSEC}")
    dialog._summary_edit.setText("ncryptsec1" + "q" * 40)
    dialog._publish("post", "Body.", 1_800_000_000)
    assert asked == [["OK"]] and dialog._job is None              # encrypted: remove it
    dialog._summary_edit.setText("A summary.")
    queue.append(True)
    dialog._publish("post", f"[backup](nostr:{NSEC})", 1_800_000_000)
    settle()
    event = dialog._relay_pool.published[-1][1]
    assert refs.secret_keys_in_event(event) == []
    assert event["content"] == f"[backup]({OWNER})"
    assert ["title", f"About {OWNER}"] in event["tags"]


def test_a_key_in_the_slug_is_refused_never_rewritten(tmp_path, answers):
    queue, asked = answers
    dialog = _dialog("article", tmp_path, None)
    published_before = len(dialog._relay_pool.published)
    dialog._publish(f"backup-{NSEC}", "Body.", 1_800_000_000)
    assert asked == [["OK"]] and dialog._job is None
    assert len(dialog._relay_pool.published) == published_before
    unsigned = build_article(f"Mine {NSEC}", PK, f"backup-{NSEC}")
    assert ["d", f"backup-{NSEC}"] in without_secret_keys(unsigned)["tags"]   # never changed


@pytest.mark.parametrize("language", ["en", "pseudo"])
@pytest.mark.parametrize("typed", [False, True])
def test_the_slug_message_has_the_room_it_needs(language, typed):
    """Opened with the message, or with it appearing while the slug is
    typed, in English and in the longer test language: every line shows,
    and the slug field keeps its height."""
    import i18n
    from PySide6.QtTest import QTest
    from nostr.ui.stash_kind_dialog import StashKind, StashKindDialog
    i18n.install(language)
    try:
        dialog = StashKindDialog(default=StashKind.ARTICLE, is_dark=False,
                                 suggested_slug="" if typed else f"backup-{NSEC}")
        dialog.show()
        QApplication.processEvents()
        if typed:
            QTest.keyClicks(dialog._slug_edit, f"backup-{NSEC}")
            QApplication.processEvents()
        label, field = dialog._slug_problem, dialog._slug_edit
        assert not label.isHidden()
        assert label.height() >= label.heightForWidth(label.width())
        assert field.height() >= field.minimumSizeHint().height()
        dialog.close()
    finally:
        i18n.install(i18n.ENGLISH)


def test_a_key_in_the_slug_never_leaves_the_computer(tmp_path, monkeypatch):
    """Said under the slug, with its section opened, before anything is
    sent: not even the question whether the article went out before."""
    import announce
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    dialog = _dialog("article", tmp_path, None)
    dialog._job = None
    asked = []
    dialog._published_at_lookup = lambda *args: asked.append(args)
    dialog._title_edit.setText(f"Backup {NSEC}")                # the slug follows it,
    assert dialog._slug_edit.text() == "backup"                 # never with a key
    dialog._slug_edit.setText(f"backup-{NSEC}")
    dialog._on_slug_edited(dialog._slug_edit.text())
    assert not dialog._slug_problem.isHidden() and "link name" in dialog._slug_problem.text()
    assert dialog._details_toggle.isChecked() and dialog._slug_edit.isVisibleTo(dialog)
    assert not dialog._publish_btn.isEnabled()
    dialog._on_publish()
    assert asked == [] and dialog._relay_pool.published == []
    dialog._slug_edit.setText("backup")
    dialog._on_slug_edited("backup")
    assert dialog._slug_problem.isHidden() and dialog._publish_btn.isEnabled()
    dialog._slug_edit.setText(f"backup-{NSEC}")
    dialog._on_slug_edited(dialog._slug_edit.text())
    heard.clear()
    dialog.show()                              # opened with it: said once shown
    assert heard == [dialog._slug_problem.text()]
    dialog.close()


def test_save_as_draft_refuses_a_key_in_the_slug(monkeypatch):
    import announce
    from nostr.ui.stash_kind_dialog import StashKind, StashKindDialog
    heard = []
    monkeypatch.setattr(announce, "announce", lambda source, text: heard.append(text))
    dialog = StashKindDialog(default=StashKind.ARTICLE, suggested_slug=f"backup-{NSEC}")
    assert not dialog._save_btn.isEnabled()
    assert not dialog._slug_problem.isHidden()
    assert "link name" in dialog._slug_problem.text()
    assert dialog._slug_edit.accessibleDescription() == dialog._slug_problem.text()
    assert heard == []                         # not shown yet: no one to hear it
    dialog.show()                              # opened with it: said once, now
    assert heard == [dialog._slug_problem.text()]
    dialog.close()
    dialog._slug_edit.setText("backup")
    dialog._on_slug_edited("backup")
    assert dialog._save_btn.isEnabled() and dialog._slug_problem.isHidden()
