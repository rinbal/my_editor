# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native authoring keeps real documents, target identity and private sources."""
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication

from multilingual_authoring import draft_link, private_link, same_source_projection
from nostr.article_details import ArticleDetails
from nostr.drafts import serialize_inner_event, parse_inner_event
from nostr.ui.language_chooser import LanguageChooser
from tests.app_process import run_window_script

PRIVATE = {"version": 1, "groupId": "group-1", "owner": "ab" * 32,
           "versions": {"de": "source-id", "en": "target-id"},
           "source": {"dTag": "source-id", "language": "de", "title": "Original",
                      "content": "PRIVATE SOURCE SENTINEL", "eventId": "", "address": "", "public": False}}


@pytest.mark.parametrize("query,expected", [("ไทย", "th"), ("ລາວ", "lo"),
                                           ("ខ្មែរ", "km"), ("日本語", "ja"),
                                           ("Hmong", "hmn"), ("Laos", "lo")])
def test_search_reaches_all_languages_including_native_names(query, expected):
    picker = LanguageChooser(current="de", existing=("de", "en"), dark=False)
    picker.search.setText(query)
    assert picker.results.item(0).data(Qt.ItemDataRole.UserRole) == expected
    picker.close()


def test_the_chooser_speaks_the_interface_language_and_shows_the_versions(monkeypatch):
    # me68-red-b-1, -5, -6, -12: German names, every language in name
    # order, regions by name, the versions apart, the current one checked
    # beside its name and never the row Return would open.
    from nostr.ui import language_chooser
    monkeypatch.setattr(language_chooser, "interface_language", lambda: "de")
    picker = LanguageChooser(current="de", existing=("de", "en"), dark=False)
    rows = [picker.results.item(i) for i in range(picker.results.count())]
    assert [row.text() for row in rows[:4]] == [
        "Versions of this article", "Deutsch", "English · Englisch", "Other languages"]
    assert not rows[0].flags() & Qt.ItemFlag.ItemIsEnabled
    assert not rows[1].icon().isNull() and "✓" not in rows[1].text()
    assert rows[1].data(Qt.ItemDataRole.AccessibleDescriptionRole) == "Current language"
    assert picker.results.currentItem() is rows[2]
    picker.search.setText("Italienisch")
    assert picker.results.item(0).data(Qt.ItemDataRole.UserRole) == "it"
    assert language_chooser.language_name("de-AT") == "Deutsch (Österreich)"
    assert language_chooser.language_name("sw") == "Suaheli"
    picker.search.setText("")
    picker.all_button.click()
    names = [picker.results.item(i).text().split(" · ")[-1]
             for i in range(4, picker.results.count())]
    assert len(names) > 7000 and not picker.more_button.isVisible()
    assert names.index("Italienisch") < names.index("Japanisch") < names.index("Niederländisch")
    picker.close()


def test_ime_composition_does_not_select_the_first_result():
    picker = LanguageChooser()
    chosen = []
    picker.chosen.connect(chosen.append)
    QApplication.sendEvent(picker.search, QInputMethodEvent("日本", []))
    picker._choose_first()
    assert chosen == []
    QApplication.sendEvent(picker.search, QInputMethodEvent("", []))
    picker.search.setText("日本語")
    picker._choose_first()
    assert chosen == ["ja"]


def test_a_draft_keeps_no_local_paths_and_leaves_out_a_reference_too_long():
    # me68-red-a-4 and -9: where the versions are saved stays on this
    # computer; a reference that would make the draft too long is kept as
    # its fingerprint, which still tells a changed original.
    link = {**PRIVATE, "documents": {"target-id": "/Users/someone/blog/post.md"}}
    kept = draft_link(link)
    assert "documents" not in kept and kept["source"]["content"] == "PRIVATE SOURCE SENTINEL"
    short = draft_link(link, fits=lambda _record: False)
    assert "content" not in short["source"]
    assert same_source_projection({**PRIVATE["source"]}, short["source"])
    assert not same_source_projection({**PRIVATE["source"], "content": "Changed"}, short["source"])


def test_encrypted_link_is_a_private_root_field_not_an_event_tag():
    inner = {"kind": 30023, "created_at": 1700000000, "pubkey": "ab" * 32, "content": "English", "tags": [["d", "target-id"], ["l", "en", "ISO-639-1"]],
             "standup_multilingual": PRIVATE}
    restored = parse_inner_event(serialize_inner_event(inner))
    assert restored["standup_multilingual"] == PRIVATE
    assert "PRIVATE SOURCE SENTINEL" not in json.dumps(restored["tags"])
    assert "PRIVATE SOURCE SENTINEL" not in json.dumps(ArticleDetails.from_tags(restored["tags"]).published_tags("English"))
    assert private_link({**PRIVATE, "versions": {"invented": "bad"}}) is None


def test_new_linked_uuid_has_one_review_and_keeps_replayed_wire_date(tmp_path, monkeypatch):
    from uuid import uuid4
    from tests.test_article_published_at import _dialog, CapturedJob
    from tests.outbox_fakes import PK, signed
    from nostr.first_publications import FirstPublications
    import nostr.ui.publish_article_dialog as module
    CapturedJob.made = []
    monkeypatch.setattr(module, "PublishJob", CapturedJob)
    identifier = str(uuid4())
    asked = []
    first = FirstPublications(tmp_path / "first.json")
    dialog = _dialog(tmp_path, slug=identifier, lookup=lambda *args: asked.append(args),
                     first_publications=first,
                     language_context={"version": 1, "groupId": "fixture", "expected_event_id": None})
    dialog._on_publish()
    assert asked == [] and len(CapturedJob.made) == 1
    assert not dialog._looking_up
    replayed_date = 1700000000
    job = CapturedJob.made[0]
    job.signed_event = signed(30023, [["d", identifier], ["published_at", str(replayed_date)]], "# Hello\n\nBody.")
    dialog._on_completed([("wss://test.example", True, "")])
    assert dialog.details().published_at == replayed_date
    assert first.get(PK, identifier) == replayed_date


def test_invalid_pair_keeps_single_version_menu_and_opens_original(tmp_path, monkeypatch):
    from uuid import uuid4
    from tests.test_article_published_at import _dialog, CapturedJob
    from tests.outbox_fakes import PK
    import nostr.ui.publish_article_dialog as module
    CapturedJob.made = []
    monkeypatch.setattr(module, "PublishJob", CapturedJob)
    asked = []
    dialog = _dialog(tmp_path, slug=str(uuid4()), lookup=lambda *args: asked.append(args),
                     language_context={"expected_event_id": None, "source": {"language": "de"}},
                     pair_source={"details": ArticleDetails(identifier=str(uuid4())).with_language("de"),
                                  "body": "Original", "media": (), "language": "de", "owner": PK,
                                  "problem": "Finish the original first"})
    dialog.show()
    dialog._set_pair(True)
    assert dialog._publication_options.isEnabled()
    dialog._on_publish()
    assert asked == [] and CapturedJob.made == []
    dialog._set_pair(False)
    assert dialog._publish_btn.isEnabled()
    dialog._set_pair(True)
    opened = []
    dialog.open_original.connect(lambda: opened.append(True))
    dialog._pair_problem.click()
    assert opened == [True] and not dialog.isVisible()


SCRIPT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QTextCursor
app = QApplication(sys.argv[:1])
import main_window, workspace_restore
from nostr.article_details import ArticleDetails
from nostr.profiles import Profile
from tests.outbox_fakes import PK
from nostr.publisher import build_article
import multilingual_authoring
main_window.MainWindow._maybe_auto_check_for_updates = lambda self: None
w = main_window.MainWindow()
w.resize(1450, 850)
w.show()
profile = Profile(user_pubkey=PK, bunker_pubkey="", bunker_relays=[], local_secret_hex="", display_name="Fixture author")
w._profile_store.default = lambda: profile
w.nostr_state._active = True
w._on_nostr_in_use_changed(True)
source = w.new_tab()
source.insertPlainText("Privater Originaltext.\n\nPRIVATE SOURCE SENTINEL")
w._set_article_details(source, ArticleDetails(title="Original").with_language("de"))
app.processEvents()
target = w._multilingual.open_language(source, "th")
app.processEvents()
r = {"blank": target.toPlainText() == "", "source_kept": "PRIVATE SOURCE SENTINEL" in source.toPlainText(),
     "different_id": w._article_details_of(source).identifier != w._article_details_of(target).identifier,
     "reference": target._language_reference_pane.isVisible(), "details_closed": not w.act_article_details.isChecked()}
r["reference_measured"] = target._language_reference_pane.width() >= 260 and target.width() >= 400
# red-b-8, -9: the new tab says which version it is; the command says
# what it does now, with no check mark.
r["tab_named"] = w._document_name(target) == "Original · Thai"
r["hide_reference_title"] = (w.act_language_reference.text() == "Hide Reference"
                             and not w.act_language_reference.isCheckable())
# red-b-4: the chooser opens inside the screen, under its control.
w._multilingual.choose(); app.processEvents()
chooser = w._multilingual._chooser
r["chooser_on_screen"] = chooser.screen().availableGeometry().contains(chooser.frameGeometry())
chooser.close(); w.activateWindow(); app.processEvents()
target.insertPlainText("ภาษาไทย\n\nงานเขียนของฉัน")
cursor = target.textCursor(); cursor.setPosition(2); target.setTextCursor(cursor)
original_cursor = target.textCursor().position()
w._multilingual.open_language(target, "de")
again = w._multilingual.open_language(source, "th")
app.processEvents()
r["reopen_same_document"] = again is target and target.textCursor().position() == original_cursor
r["text_kept"] = "ภาษาไทย" in target.toPlainText() and target.document().isUndoAvailable()
reference = target._language_reference_pane
splitter = target._view_splitter
sizes = [0] * splitter.count()
sizes[0] = 850; sizes[splitter.indexOf(reference)] = 430
splitter.setSizes(sizes)
w._remember_split(splitter)
before_sizes = splitter.sizes()
w._multilingual.refresh()
r["divider_preserved"] = before_sizes == splitter.sizes()
w._choose_view("preview"); app.processEvents()
w._choose_view("write"); app.processEvents()
r["preview_then_reference_measured"] = reference.width() >= 260 and target.width() >= 400
reference.body.setFocus()
app.processEvents()
r["fixed_target"] = w.current_editor() is target
r["reference_readonly"] = reference.body.isReadOnly() and not w.act_bold.isEnabled()
target.setFocus(); app.processEvents()
w.act_article_details.setChecked(True); app.processEvents()
r["one_supplementary"] = not reference.isVisible()
w.act_article_details.setChecked(False); app.processEvents()
r["reference_restored"] = reference.isVisible()
w._multilingual.toggle_reference(); app.processEvents()
w.resize(700, 850); app.processEvents(); app.processEvents()
w.resize(1450, 850); app.processEvents(); app.processEvents()
r["hidden_stays_hidden"] = not reference.isVisible()
w._multilingual.toggle_reference(); app.processEvents()
w.resize(700, 850); app.processEvents(); app.processEvents()
r["narrow_writing"] = target.isVisible() and not reference.isVisible()
w._multilingual.toggle_reference(); app.processEvents()
r["narrow_reference"] = reference.isVisible() and not target.isVisible() and w.current_editor() is target
# red-b-7: reference-only shows its way back, beside the caption.
r["narrow_back_shown"] = reference.back.isVisible() and reference.back.text() == "Back to Thai"
reference.back.click(); app.processEvents()
r["narrow_return"] = target.isVisible() and target.toPlainText().startswith("ภาษาไทย")
w.resize(1450, 850); app.processEvents(); app.processEvents()
record = workspace_restore.capture_editor_tab(target)
r["workspace_link"] = record.language_link["source"]["content"].endswith("PRIVATE SOURCE SENTINEL")
path = os.path.join(sys.argv[2], "thai.md")
assert w._save_to(path)
w._saved_as(path, "")
idx = w._editor_widget_index(target)
target.document().setModified(False)
w.close_tab(idx); app.processEvents()
restored = w.open_path(path)
app.processEvents()
link = w._multilingual.context(restored)
r["file_link"] = link["groupId"] == record.language_link["groupId"]
with open(path, encoding="utf-8") as saved:
    written = saved.read()
r["file_keeps_nothing_private"] = ("standup_multilingual" not in written and "SENTINEL" not in written
                                   and PK not in written and sys.argv[2] not in written)
body, media = w._publish_payload(restored, "markdown")
event = build_article(content=body, pubkey_hex=profile.user_pubkey, slug=w._article_details_of(restored).identifier,
                      title="Thai", published_at=1700000000,
                      extra_tags=w._multilingual.publication_details(restored).published_tags(body))
r["private_publish_safe"] = "PRIVATE SOURCE SENTINEL" not in json.dumps(event) and "standup_multilingual" not in json.dumps(event)
draft, *_ = w._submission_of(restored, profile, [])
r["private_submission_safe"] = "PRIVATE SOURCE SENTINEL" not in json.dumps(draft.content) and not any(tag[0] == "standup:multilingual" for tag in draft.extra_tags)
w._open_publish_article(restored)
app.processEvents()
sheet = w._publish_sheet
r["scope"] = "stays unpublished" in sheet._scope.text()
r["named_publish"] = "Thai" in sheet._publish_btn.text()
sheet._set_pair(True); app.processEvents()
r["pair_named_scope"] = "German" in sheet._publish_btn.text() and "Thai" in sheet._publish_btn.text() and sheet._source_card.isVisible()
# red-b-11: both cards say their language; the original's shows words, not Markdown.
r["pair_cards_balanced"] = (sheet._source_card.byline.text().startswith("German")
                            and sheet._card.byline.text().startswith("Thai")
                            and "#" not in sheet._source_card.summary.text())
# red-b-2: the original went out and the second did not: said as such.
sheet._pair_version_out({"tags": [["d", w._article_details_of(source).identifier]]})
sheet._say_not_published("", "relay.example: blocked")
r["pair_half_failure"] = ("Thai wasn’t published" in sheet._status.text()
                          and "German is public. Try again to publish Thai." in sheet._status.text()
                          and sheet._status.toolTip() == "relay.example: blocked"
                          and sheet._scope.text().startswith("Thai will be public."))
sheet._set_pair(False); app.processEvents()
r["optional_hidden"] = not sheet._summary_edit.isVisible() and not sheet._hashtags.isVisible()
sheet.reject()
# Pair source success must survive even when no target signature follows.
from tests.outbox_fakes import signed, FakeRelayDirectory, settle
from tests.test_source_provenance import SourcePool
from nostr.content_language import language_tags
from recovery import read_backup
source_details = w._article_details_of(source)
source_body = w._publish_payload(source, "markdown", keep_local=True)[0]
accepted_source = signed(30023, [["d", source_details.identifier], ["title", source_details.title],
                                ["published_at", "1700000000"], *language_tags("de")], source_body)
w._language_version_published(restored, accepted_source)
recovery_record = read_backup(restored._backup.path)
r["accepted_source_recovery"] = recovery_record["language_link"]["source"]["eventId"] == accepted_source["id"]
# Simulate a restored target after signer refusal: no target event exists.
from nostr.drafts import serialize_inner_event, parse_inner_event
restored._language_link = parse_inner_event(serialize_inner_event({"kind": 30023, "created_at": 1700000000, "pubkey": PK, "content": body, "tags": [["d", w._article_details_of(restored).identifier]], "standup_multilingual": recovery_record["language_link"]}))["standup_multilingual"]
w._multilingual._confirmed_public.clear()
old_pool, old_directory = w._relay_pool, w._relay_directory
pool = SourcePool(); pool.stored = {accepted_source["id"]: accepted_source}
w._relay_pool = pool
w._relay_directory = FakeRelayDirectory({PK: ["wss://test.example"]})
ready = []
w._multilingual.prepare_source(restored, ready.append)
settle(50)
r["restored_pin_publicly_verified"] = ready == [True] and w._multilingual.publication_details(restored).carried[-1][1] == accepted_source["id"]
w._relay_pool, w._relay_directory = old_pool, old_directory
# Explicit text review advances the pin within the established group.
from nostr.multilingual import translation_tags, translation_metadata
from PySide6.QtWidgets import QPushButton
root_address = "30023:" + PK + ":" + source_details.identifier
target_details = w._article_details_of(restored)
target_event = signed(30023, [["d", target_details.identifier], ["title", "Thai title"],
                            ["published_at", "1700000000"], *language_tags("th"),
                            *translation_tags({"root": root_address, "source": root_address,
                                              "source_event": accepted_source["id"]})], body)
w._multilingual.published(restored, target_event)
new_source = signed(30023, accepted_source["tags"], source_body + "\n\nA reviewed new paragraph.", created_at=accepted_source["created_at"] + 1)
restored._language_source_check = {"available": True, "event": accepted_source, "current": new_source,
                                   "changed": True, "confirmed_root": root_address}
w._multilingual.review_source(restored); app.processEvents()
review = w._multilingual._review_sheet
next(button for button in review.findChildren(QPushButton) if button.text() == "Use this reference").click()
app.processEvents()
reviewed = translation_metadata(w._multilingual.publication_details(restored).carried)
r["review_keeps_root_advances_pin"] = reviewed["root"] == root_address and reviewed["source_event"] == new_source["id"]
# A later source migration never changes an already public target's group.
restored._language_source_check = {"available": True, "event": new_source, "current": new_source,
                                   "changed": True, "confirmed_root": "30023:" + PK + ":another-root", "group_conflict": True}
kept = w._multilingual.publication_details(restored)
w._multilingual.review_source(restored)
unchanged = translation_metadata(w._multilingual.publication_details(restored).carried)
r["published_root_immutable"] = unchanged["root"] == root_address and unchanged["source_event"] == new_source["id"]
# Opening a public reference checks quietly; a send joins that same read.
import nostr.source_provenance as source_provenance
checker = source_provenance.check_source_snapshot
pending = []
source_provenance.check_source_snapshot = lambda pool, directory, snapshot, done, parent=None: pending.append(done)
w.tabs.setCurrentIndex(w._editor_widget_index(restored))
restored._language_last_source_token = None
w.status.showMessage("Unrelated work in progress")
w._multilingual.refresh(); app.processEvents()
r["quiet_read_has_no_status_noise"] = len(pending) == 1 and w.status.currentMessage() == "Unrelated work in progress"
joined = []
w._multilingual.prepare_source(restored, joined.append)
latest_source = signed(30023, new_source["tags"], new_source["content"] + "\n\nA later public change.", created_at=new_source["created_at"] + 1)
pending[0]({"available": True, "event": new_source, "current": latest_source, "changed": True, "confirmed_root": root_address})
app.processEvents()
r["foreground_joins_quiet_read"] = len(pending) == 1 and joined == [True]
r["quiet_change_hint_preserves_pin"] = restored._language_reference_pane.change_hint.isVisible() and restored._language_link["source"]["eventId"] == new_source["id"]
source_provenance.check_source_snapshot = checker
# A public reference excludes unpublished changes in the original buffer.
source.insertPlainText(" Unpublished suffix.")
w._multilingual.refresh(); app.processEvents()
r["unpublished_source_note"] = restored._language_reference_pane.unpublished.isVisible() and "Unpublished suffix" not in restored._language_reference_pane.body.toPlainText()
# An unrelated document may have the same imported d; grouping is scoped.
unrelated = w.new_tab()
w._set_article_details(unrelated, ArticleDetails(identifier=w._article_details_of(source).identifier).with_language("de"))
unrelated._language_link = {"version": 1, "groupId": "unrelated", "owner": profile.user_pubkey,
                            "versions": {"de": w._article_details_of(source).identifier}}
w.tabs.tabBar().moveTab(w._editor_widget_index(unrelated), 0)
r["source_group_scoped"] = w._multilingual.source_editor(restored) is source
w._set_article_details(unrelated, ArticleDetails(identifier=w._article_details_of(restored).identifier).with_language("th"))
unrelated._language_link["versions"] = {"th": w._article_details_of(restored).identifier}
r["existing_group_scoped"] = w._multilingual.open_language(source, "th") is restored
# A new document does not inherit an opaque source file's kept restrictions.
import tab_details
opaque = w.new_tab()
opaque.insertPlainText("Kept source text")
tab_details.hold(opaque, ArticleDetails(identifier="kept-source").with_language("de").keeping({"slug", "lang", "nostr_tags"}))
new_target = w._multilingual.open_language(opaque, "ja")
r["kept_source_new_identity"] = w._article_details_of(new_target).identifier != "kept-source" and bool(w._article_details_of(new_target).identifier)
r["kept_source_target_language"] = w._article_details_of(new_target).language == "ja" and not w._article_details_of(new_target).kept
# Optional metadata is part of source review; a body-only equality is unsafe.
opaque_link = dict(new_target._language_link)
opaque_details = w._article_details_of(opaque)
tab_details.hold(opaque, opaque_details.with_changes(summary="A new summary"))
r["metadata_change_needs_review"] = bool(w._multilingual.pair_source(new_target)["problem"])
# Choosing a target does not guess an unlabeled source from the UI language.
unlabeled = w.new_tab()
unlabeled.insertPlainText("Dies ist ein deutscher Text ohne Sprachangabe.")
english = w._multilingual.open_language(unlabeled, "en")
r["unlabeled_creates_blank_target"] = english is not unlabeled and english.toPlainText() == "" and w._article_details_of(english).language == "en" and not w._article_details_of(unlabeled).language
w._multilingual.set_language(unlabeled, "de")
r["label_edit_keeps_existing_document"] = w._multilingual.open_language(english, "de") is unlabeled
# A title derived from a heading is the same reviewed publication projection.
untitled = w.new_tab()
w._load_markdown(untitled, "# A derived title\n\nThe original text.")
w._set_article_details(untitled, ArticleDetails().with_language("de"))
untitled_target = w._multilingual.open_language(untitled, "en")
untitled_target.insertPlainText("The English translation.")
untitled_source = untitled_target._language_link["source"]
untitled_event = signed(30023, [["d", untitled_source["dTag"]], ["title", untitled_source["title"]], *language_tags("de")], untitled_source["content"])
w._multilingual.source_published(untitled_target, untitled_event)
r["derived_title_pair_recovery"] = read_backup(untitled_target._backup.path)["language_link"]["source"]["eventId"] == untitled_event["id"]
# The original after a translation went out names it, never itself (red-a-3).
source_address = "30023:" + PK + ":" + w._article_details_of(source).identifier
source._language_group_check = {"root": source_address, "events": [target_event],
                                "translations": [], "conflict": False}
as_root = translation_metadata(w._multilingual.publication_details(source).carried)
r["root_never_its_own_original"] = (as_root["root"] == "" and as_root["translations"]
                                    == ["30023:" + PK + ":" + target_details.identifier])
# A file's original that is public already is found when linking (red-a-2).
public_file = w.new_tab()
public_file.insertPlainText("Published long ago")
w._set_article_details(public_file, ArticleDetails(identifier="published-long-ago", title="Old").with_language("de"))
r["not_known_is_not_absent"] = "expected_event_id" not in (w._multilingual.context(public_file) or {"x": 1}) \
    and w._multilingual._needs_public_lookup(public_file)
out_there = signed(30023, [["d", "published-long-ago"], ["title", "Old"], *language_tags("de")], "Published long ago")
pool = SourcePool(); pool.stored = {out_there["id"]: out_there}
w._relay_pool, w._relay_directory = pool, FakeRelayDirectory({PK: ["wss://test.example"]})
linked = w._multilingual.open_language(public_file, "en")
settle(60)
w._relay_pool, w._relay_directory = old_pool, old_directory
r["public_original_found"] = (linked._language_link["source"]["eventId"] == out_there["id"]
                              and w._multilingual.context(public_file)["expected_event_id"] == out_there["id"])
# The latest action asked for while the reference is read is the one that follows (red-a-11).
pending = []
source_provenance.check_source_snapshot = lambda pool, directory, snapshot, done, parent=None: pending.append(done)
restored._language_last_source_token = None
restored._language_preparing = False
first_action, second_action = [], []
w._multilingual.prepare_source(restored, first_action.append)
w._multilingual.prepare_source(restored, second_action.append)
pending[0]({"available": True, "event": new_source, "current": latest_source, "changed": True, "confirmed_root": root_address})
app.processEvents()
r["latest_action_follows"] = first_action == [] and second_action == [True]
source_provenance.check_source_snapshot = checker
# Switching accounts during a public read never opens an old send under a new account.
import nostr.source_provenance as source_provenance
from PySide6.QtCore import QTimer
checker = source_provenance.check_source_snapshot
source_provenance.check_source_snapshot = lambda pool, directory, snapshot, done, parent=None: QTimer.singleShot(0, lambda: done({"available": True, "event": untitled_event, "current": untitled_event, "changed": False, "confirmed_root": "30023:" + PK + ":" + untitled_source["dTag"]}))
continued = []
w._multilingual.prepare_source(untitled_target, continued.append)
other_profile = Profile(user_pubkey="cd"*32, bunker_pubkey="", bunker_relays=[], local_secret_hex="")
w._profile_store.default = lambda: other_profile
app.processEvents(); app.processEvents()
r["async_account_guard"] = continued == []
w._profile_store.default = lambda: profile
source_provenance.check_source_snapshot = checker
w.nostr_state._active = False
w._on_nostr_in_use_changed(False); app.processEvents()
r["local_controls_restore"] = w.view_switch.isVisible() and not w._multilingual.controls.isVisible()
w.nostr_state._active = True
w._on_nostr_in_use_changed(True); app.processEvents()
# Write, Preview and Split stay; the language controls join them on an article.
r["nostr_icons_restore"] = w.view_switch.isVisible() and w._multilingual.controls.isVisible()
code = w.new_tab()
w._editor_kind = (lambda original: lambda ed: "code" if ed is code else original(ed))(w._editor_kind)
w._multilingual.refresh(); app.processEvents()
r["no_language_icon_off_articles"] = not w._multilingual.languages.isVisible()
for editor in w._multilingual.editors():
    editor.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
'''


def test_native_first_time_flow_preserves_work_and_private_source(tmp_path):
    result = run_window_script(SCRIPT, tmp_path, str(tmp_path))
    assert all(result.values()), result
