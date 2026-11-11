# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edit on a published article that has a draft (review N1).

What must hold: an article published from MyEditor keeps its draft under
the same identifier. Edit opens that draft when it holds the published
text; when the draft has changes not published yet, the person chooses
(Open Draft is the default, Cancel opens nothing), so the unpublished
words are never hidden behind the published ones. Saving a tab as a
draft under an identifier another draft has asks first, the way Save As
asks before it replaces a file (Cancel is the default), and a tab bound
to that draft just saves it again. A tab recovered after a crash keeps
the draft it was bound to and the article it edited (review N13).

The window is built in a child process with its own home folder
(tests/app_process.py); no relay is asked.
"""

from tests.app_process import run_window_script

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QDialog
app = QApplication(sys.argv[:1])
import main_window
from nostr import crypto, events
from nostr.drafts import DraftWrapMeta
from nostr.profiles import Profile
from nostr.published import PublishedItem
from nostr.ui.stash_kind_dialog import StashChoice, StashKind

asked, answers = [], []


def ask(parent, *, title, message="", buttons, **_kw):
    asked.append({"title": title, "buttons": [(b.label, b.role) for b in buttons]})
    return answers.pop(0)


def confirm_destructive(parent, *, title, message="", action, **_kw):
    asked.append({"title": title, "action": action})
    return answers.pop(0)


main_window.ask = ask
main_window.confirm_destructive = confirm_destructive

w = main_window.MainWindow()
w.show()
app.processEvents()
sk = crypto.generate_secret_key()
pk = events.sign_event({"kind": 1, "content": "", "tags": [], "created_at": 1}, sk)["pubkey"]
alice = Profile(user_pubkey=pk, bunker_pubkey="b" * 64, bunker_relays=[],
                local_secret_hex="c" * 64, display_name="Alice")
w._profile_store.upsert(alice)
w._profile_store.set_default(pk)
store = w._draft_store
store.bind_profile(pk)


def draft(text):
    store.upsert_skeleton(DraftWrapMeta(identifier="my-post", inner_kind=30023,
                                        event_id=os.urandom(32).hex(), pubkey=pk,
                                        created_at=1_800_000_500 + len(asked),
                                        expiration=None, ciphertext="x"))
    store.set_decrypted("my-post", inner={"kind": 30023, "content": text,
                                          "tags": [["d", "my-post"], ["title", "My Post"]]})


article = events.sign_event({
    "kind": 30023, "created_at": 1_800_000_000, "content": "The body.",
    "tags": [["d", "my-post"], ["title", "My Post"], ["published_at", "1700000000"]]}, sk)
item = PublishedItem(event=article, found_on=("wss://r.example",))


def edit():
    before = w.tabs.count()
    w._published_view.open_requested.emit(item)
    app.processEvents()
    ed = w.current_editor()
    binding = getattr(ed, "_draft_binding", None)
    return {"tabs": w.tabs.count() - before, "text": ed.toPlainText(),
            "bound_to_draft": binding is not None and binding.identifier == "my-post"}


def close_current():
    ed = w.current_editor()
    ed.document().setModified(False)
    w.tabs.removeTab(w.tabs.currentIndex())


r = {}
# The draft holds the published text: it is what opens, nothing asked.
draft("The body.")
r["same"] = edit()
r["same_asked"] = len(asked)
close_current()
# The draft has unpublished changes: asked, Open Draft the default.
draft("The body, and my unpublished edits.")
answers.append("draft")
r["changed_draft"] = edit()
r["question"] = asked[-1]
close_current()
answers.append(None)                                  # Cancel
r["cancelled"] = edit()["tabs"]
answers.append("published")
r["published"] = edit()

# Saving that published-version tab as a draft with the dialog's defaults
# targets the draft's identifier: asked before it is replaced.
fired = []
w._fire_draft_publish_job = lambda ed, profile, choice, inner: fired.append(choice.identifier)


class Picker:
    def __init__(self, **_kw):
        self.choice = StashChoice(kind=StashKind.ARTICLE, identifier="my-post",
                                  title="My Post", summary="")

    def exec(self):
        return QDialog.Accepted


main_window.StashKindDialog = Picker
answers.append(False)                                 # Cancel, the default
w._stash_current_tab_as_draft()
r["replace_question"] = asked[-1]
r["fired_after_cancel"] = list(fired)
answers.append(True)                                  # Replace
w._stash_current_tab_as_draft()
r["fired_after_replace"] = list(fired)
# A tab bound to the draft saves it again without a word.
count = len(asked)
close_current()
draft("The body, and my unpublished edits.")
answers.append("draft")
edit()
w._stash_current_tab_as_draft()
r["bound_tab_asked"] = len(asked) - count - 1         # less the Edit question
r["bound_tab_fired"] = fired[-1]

for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_edit_opens_the_draft_or_asks_and_saving_never_replaces_a_draft_unasked(tmp_path):
    r = run_window_script(SCRIPT, tmp_path)
    assert r["same"] == {"tabs": 1, "text": "The body.", "bound_to_draft": True}
    assert r["same_asked"] == 0
    assert r["changed_draft"]["bound_to_draft"]
    assert r["changed_draft"]["text"] == "The body, and my unpublished edits."
    assert r["question"]["title"] == (
        "This article has a draft with changes you haven’t published.")
    assert r["question"]["buttons"] == [["Cancel", "cancel"],
                                        ["Open Published Version", "normal"],
                                        ["Open Draft", "default"]]
    assert r["cancelled"] == 0
    assert r["published"] == {"tabs": 1, "text": "The body.", "bound_to_draft": False}
    assert r["replace_question"] == {
        "title": "A draft named “My Post” already exists. Do you want to replace it?",
        "action": "Replace"}
    assert r["fired_after_cancel"] == [] and r["fired_after_replace"] == ["my-post"]
    assert r["bound_tab_asked"] == 0 and r["bound_tab_fired"] == "my-post"


RECOVERY_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.article_details import ArticleDetails
from recovery import read_backup

w = main_window.MainWindow()
w.show()
app.processEvents()
r = {}

def recovered(setup):
    ed = w.new_tab()
    setup(ed)
    ed.insertPlainText("Words typed before the crash.")
    assert ed._backup.write_now()
    again = w._restore_one_backup(read_backup(ed._backup.path))
    return again

# A tab bound to a draft, and one editing a published article.
draft_tab = recovered(lambda ed: setattr(ed, "_draft_binding", main_window.DraftBinding(
    identifier="my-draft", inner_kind=30023, title="My Draft", profile_pubkey="a" * 64)))
binding = draft_tab._draft_binding
r["draft"] = [binding.identifier, binding.inner_kind, binding.title, binding.profile_pubkey]


def edit_tab(ed):
    ed._published_article = ArticleDetails(identifier="my-post", title="My Post",
                                           published_at=1700000000)
    ed._published_author = "b" * 64


article_tab = recovered(edit_tab)
r["article"] = [article_tab._published_article.identifier,
                article_tab._published_article.published_at, article_tab._published_author]
r["text"] = article_tab.toPlainText()
for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_tab_recovered_after_a_crash_keeps_its_draft_and_its_article(tmp_path):
    # Review N13: the backup kept only the text, so the recovered Edit tab,
    # published, made a second article under a new identifier.
    r = run_window_script(RECOVERY_SCRIPT, tmp_path)
    assert r["draft"] == ["my-draft", 30023, "My Draft", "a" * 64]
    assert r["article"] == ["my-post", 1700000000, "b" * 64]
    assert r["text"] == "Words typed before the crash."


ACCOUNTS_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr import crypto, events
from nostr.drafts import DraftWrapMeta
from nostr.profiles import Profile
from nostr.published import PublishedItem

asked, answers = [], []


def ask(parent, *, title, message="", buttons, **_kw):
    asked.append({"title": title, "message": message,
                  "buttons": [(b.label, b.role) for b in buttons]})
    return answers.pop(0)


main_window.ask = ask
w = main_window.MainWindow()
w.show()
app.processEvents()


def account(name):
    sk = crypto.generate_secret_key()
    pk = events.sign_event({"kind": 1, "content": "", "tags": [], "created_at": 1}, sk)["pubkey"]
    w._profile_store.upsert(Profile(user_pubkey=pk, bunker_pubkey="b" * 64, bunker_relays=[],
                                    local_secret_hex="c" * 64, display_name=name))
    return sk, pk


def article(sk, text):
    return PublishedItem(event=events.sign_event({
        "kind": 30023, "created_at": 1_800_000_000, "content": text,
        "tags": [["d", "my-post"], ["title", "My Post"]]}, sk), found_on=("wss://r.example",))


def edit(item):
    before = w.tabs.count()
    w._published_view.open_requested.emit(item)
    app.processEvents()
    return {"tabs": w.tabs.count() - before, "text": w.current_editor().toPlainText()}


alice_sk, alice = account("Alice")
bob_sk, bob = account("Bob")
r = {}
# Review L3: Alice's tab is not Bob's article of the same identifier.
w._profile_store.set_default(alice)
r["alice"] = edit(article(alice_sk, "Alice's words."))
w._profile_store.set_default(bob)
r["bob"] = edit(article(bob_sk, "Bob's words."))

# Review L4: a draft not decrypted yet has no words to compare or open.
store = w._draft_store
store.bind_profile(bob)
store.upsert_skeleton(DraftWrapMeta(identifier="my-post", inner_kind=30023, event_id="e" * 64,
                                    pubkey=bob, created_at=1_800_000_500, expiration=None,
                                    ciphertext="x"))
for i in range(w.tabs.count() - 1, -1, -1):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None and getattr(e, "_published_author", "") == bob:
        e.document().setModified(False)
        w.tabs.removeTab(i)
answers.append(None)                                  # Cancel, the default
r["not_ready_cancel"] = edit(article(bob_sk, "Bob's words."))["tabs"]
r["not_ready_question"] = asked[-1]
answers.append("published")
r["not_ready_published"] = edit(article(bob_sk, "Bob's words."))
# One that failed to decrypt: waiting changes nothing, a retry does (L12).
for i in range(w.tabs.count() - 1, -1, -1):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None and getattr(e, "_published_author", "") == bob:
        e.document().setModified(False)
        w.tabs.removeTab(i)
store.set_failed("my-post", "declined")
answers.append(None)
edit(article(bob_sk, "Bob's words."))
r["failed_question"] = asked[-1]

for i in range(w.tabs.count()):
    e = w._editor_from_widget(w.tabs.widget(i))
    if e is not None:
        e.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_edit_tells_accounts_apart_and_waits_for_a_draft_not_decrypted_yet(tmp_path):
    r = run_window_script(ACCOUNTS_SCRIPT, tmp_path)
    assert r["alice"] == {"tabs": 1, "text": "Alice's words."}
    assert r["bob"] == {"tabs": 1, "text": "Bob's words."}           # his own, in a tab of its own
    assert r["not_ready_question"] == {
        "title": "This article has a draft that can’t be shown yet.",
        "message": "Its draft is still being decrypted: choose Edit again once its title "
                   "shows in the Drafts panel, or open the version that is published now.",
        "buttons": [["Open Published Version", "normal"], ["Cancel", "default"]]}
    assert r["failed_question"]["message"] == (
        "Its draft couldn’t be decrypted: double-click it in the Drafts panel to try again, "
        "or open the version that is published now.")
    assert r["failed_question"]["buttons"] == r["not_ready_question"]["buttons"]
    assert r["not_ready_cancel"] == 0
    assert r["not_ready_published"] == {"tabs": 1, "text": "Bob's words."}
