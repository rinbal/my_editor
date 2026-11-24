# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins a Markdown file's front matter (front_matter.py, the article
details it holds in nostr/article_details.py, and a .md file's way in and
out of the window).

What must hold (decision Q-F, and the checkpoint 4 review's H2, M4, L7):

  The front matter as written is the source of truth. An article's details
  are read from it (``title``, ``summary`` or ``description``, ``image`` or
  ``cover``, ``tags``, ``slug``, ``date``, ``coauthors``, ``zap_split``),
  shown in Article Details, never in the text, never published as text. A
  file saved with no detail changed keeps its front matter byte for byte:
  unknown keys, nested mappings (Hugo's ``cover:``), comments, quoting, key
  order, block values, the byte order mark and its line endings. A changed
  detail rewrites its own entry only, where it was; a date in the form and
  time zone it was written in. Tags keep their case.

  Values are read the way front matter in the wild writes them, block
  values (``>-``, ``|``) included; a value that can't be read fully (a
  mapping) is never shown as a detail and never replaced. Values written
  are quoted when YAML would read them as another type, list items when
  they hold "," or brackets.

  A ``---`` block without a key is no front matter: a document opening
  with a divider keeps its first section.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import front_matter  # noqa: E402
from nostr.article_details import (  # noqa: E402
    ArticleDetails, from_front_matter, front_matter_with, hashtags_from,
    kept_in_file)
from tests.app_process import run_window_script  # noqa: E402

HUGO = ('---\ntitle: "My post"\ndate: 2024-03-01T00:30:00+01:00\ncover:\n'
        '  image: "images/hero.jpg"\n  alt: "A hero"\ntags: ["Go", "Hugo"]\n---\n\nBody text.\n')
JEKYLL = ("---\nlayout: post   # Jekyll's\ntitle: Hi\ndescription: >-\n  A long description\n"
          "  over two lines.\nimage:\n  path: /assets/a.png\n  alt: An image\n---\n\nBody text.\n")
CRLF_BOM = "﻿---\r\ntitle: Hello\r\nlayout: post\r\n---\r\n\r\nBody text.\r\n"
NO_GAP = "---\ntitle: Hello\n---\nBody text right under it.\n"
DIVIDER = "---\n\nThe first section of my essay.\n\n---\n\nThe second section.\n"


def moment(*parts, offset=0) -> int:
    from datetime import timedelta
    return int(datetime(*parts, tzinfo=timezone(timedelta(hours=offset))).timestamp())


def unchanged_save(text: str) -> str:
    front, body = front_matter.split(text)
    return front_matter.join(front_matter_with(from_front_matter(front), front), body)


@pytest.mark.parametrize("text", [HUGO, JEKYLL, CRLF_BOM, NO_GAP])
def test_a_file_saved_with_no_detail_changed_keeps_its_front_matter_byte_for_byte(text):
    assert unchanged_save(text) == text


def test_without_front_matter_the_text_is_all_body():
    assert front_matter.split("# Title\n\nText") == (None, "# Title\n\nText")
    assert front_matter.split("Text\n---\nmore\n---\n")[0] is None   # not at the top


def test_an_empty_block_is_front_matter_and_stays_as_it_was():
    empty = "---\n---\n\nBody text.\n"
    front, body = front_matter.split(empty)
    assert front is not None and body == "Body text.\n"
    assert unchanged_save(empty) == empty
    titled = from_front_matter(front).with_changes(title="T")
    assert front_matter.join(front_matter_with(titled, front), body) == (
        "---\ntitle: T\n---\n\nBody text.\n")


def test_a_block_without_a_key_is_text():
    assert front_matter.split(DIVIDER) == (None, DIVIDER)
    commented, _ = front_matter.split("---\n# a comment\ntitle: A\n---\nText")
    assert from_front_matter(commented).title == "A"


def test_values_are_read_as_written_in_the_wild():
    hugo = from_front_matter(front_matter.split(HUGO)[0])
    assert hugo.title == "My post" and hugo.hashtags == ("Go", "Hugo")     # case kept
    assert hugo.image == "images/hero.jpg"                   # the cover: mapping's image
    assert hugo.published_at == moment(2024, 3, 1, 0, 30, offset=1)
    jekyll = from_front_matter(front_matter.split(JEKYLL)[0])
    assert jekyll.summary == "A long description over two lines."
    assert jekyll.image == "/assets/a.png"                    # image: path/alt mapping
    entries, _ = front_matter.split("---\na: 'it''s'\nb: [x, \"y, z\"]\nc: |\n  one\n  two\n"
                                    "d: plain # comment\ne: \"My \\\"post\\\"\"   # on the site\n"
                                    "f: 'it''s' # too\ng: [Go, \"a # b\"]  # tags\n---\n")
    assert [entry.value for entry in entries.entries] == [
        "it's", ["x", "y, z"], "one\ntwo", "plain", 'My "post"', "it's", ["Go", "a # b"]]


def test_a_changed_detail_rewrites_its_entry_only():
    front, body = front_matter.split(HUGO)
    details = from_front_matter(front).with_changes(title="A new title")
    out = front_matter.join(front_matter_with(details, front), body)
    assert out == HUGO.replace('title: "My post"', 'title: "A new title"')


def test_a_changed_date_keeps_its_form_and_time_zone():
    front, body = front_matter.split(HUGO)
    later = from_front_matter(front).with_changes(
        published_at=moment(2024, 3, 2, 8, 0, offset=1))
    out = front_matter.join(front_matter_with(later, front), body)
    assert "date: 2024-03-02T08:00:00+01:00\n" in out
    for original, expected in (("2026-10-01", "2026-10-02"), ("1700000000", "1700086400"),
                               ("2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z"),
                               ('"2026-10-01"', '"2026-10-02"'),
                               ("2015-11-10 15:41:45 +0100", "2015-11-11 15:41:45 +0100")):
        front, _ = front_matter.split(f"---\ndate: {original}\n---\n")
        details = from_front_matter(front)
        moved = details.with_changes(published_at=details.published_at + 86400)
        assert f"date: {expected}" in front_matter.join(front_matter_with(moved, front), "")


def test_a_date_written_new_is_a_date_to_yaml_not_a_text():
    front, body = front_matter.split("---\ntitle: T\n---\n\nB\n")
    details = from_front_matter(front).with_changes(published_at=1791876318)
    out = front_matter.join(front_matter_with(details, front), body)
    assert "date: 2026-10-13T07:25:18Z\n" in out
    yaml = pytest.importorskip("yaml")
    assert type(yaml.safe_load(out.split("---\n")[1])["date"]).__name__ == "datetime"


@pytest.mark.parametrize("text, change, kept", [
    (HUGO, {"image": "https://cdn.example/c.jpg"}, {"image"}),             # Hugo's cover:
    ("---\ntitle: T\nimage:\n  path: /a.png\n  alt: A\n---\n\nB\n",
     {"image": "https://cdn.example/c.jpg"}, {"image"}),                    # Jekyll Chirpy
    ("---\ntitle: &t Real\nog: *t\n---\n\nB\n", {"title": "New"}, {"title"}),
])
def test_a_detail_whose_entry_cant_be_read_is_left_as_written_never_doubled(text, change, kept):
    front, body = front_matter.split(text)
    assert kept_in_file(front) == kept
    details = from_front_matter(front).with_changes(**change)
    assert front_matter.join(front_matter_with(details, front), body) == text


def test_an_emptied_detail_is_left_out_and_none_written_without_details():
    front, body = front_matter.split(HUGO)
    out = front_matter.join(front_matter_with(
        from_front_matter(front).with_changes(title=""), front), body)
    assert "title:" not in out and "cover:" in out
    assert front_matter_with(ArticleDetails(), None) is None
    assert front_matter.join(None, "Body") == "Body"


@pytest.mark.parametrize("value", ["plain words", "true", "123", "0x1F", "1e3", "2026-10-06",
                                   "a: b", "#hash", " padded ", "Ünïcödé", "\"quoted\"", "",
                                   "- dash", "[bracket]", "trailing:", "a #b"])
def test_what_is_written_reads_back_as_the_same_text(value):
    entries, _ = front_matter.split(front_matter.join(
        front_matter.FrontMatter((front_matter.entry("k", value),)), "x"))
    assert entries.entries[0].value == value


def test_list_items_with_commas_or_brackets_are_quoted():
    items = ["c++", "a]b", "x,y", "plain"]
    entries, _ = front_matter.split(front_matter.join(
        front_matter.FrontMatter((front_matter.entry("tags", items),)), "x"))
    assert entries.entries[0].value == items


def test_the_details_are_read_from_other_apps_keys():
    front, _ = front_matter.split("---\ndescription: A short one\ncover: https://c/x.jpg\n"
                                  "hashtags: [a, B]\npublished_at: 1700000000\n---\n")
    details = from_front_matter(front)
    assert (details.summary, details.image, details.hashtags, details.published_at) == (
        "A short one", "https://c/x.jpg", ("a", "B"), 1700000000)


SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.show()
r = {}
for path in sys.argv[2:]:
    w.open_path(path)
    ed = w.current_editor()
    name = os.path.basename(path)
    r[name + " shown"] = ed.toPlainText()
    r[name + " published"] = w._publish_text(ed, "markdown")
    c = ed.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    c.insertText(" More.")
    w._save_to(path)
    r[name + " saved"] = open(path, "rb").read().decode("utf-8")
    ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_markdown_files_in_the_window(tmp_path):
    paths = []
    for name, text in (("hugo.md", HUGO), ("jekyll.md", JEKYLL), ("divider.md", DIVIDER),
                       ("toml.md", TOML), ("pandoc.md", PANDOC)):
        path = tmp_path / name
        path.write_bytes(text.encode("utf-8"))
        paths.append(str(path))
    r = run_window_script(SCRIPT, tmp_path, *paths)
    assert r["hugo.md shown"] == "Body text."
    assert "cover" not in r["hugo.md published"] and "---" not in r["hugo.md published"]
    assert r["hugo.md saved"] == HUGO.replace("Body text.\n", "Body text. More.\n")
    assert r["jekyll.md saved"] == JEKYLL.replace("Body text.\n", "Body text. More.\n")
    assert "The first section of my essay." in r["divider.md shown"]
    assert r["divider.md saved"] == DIVIDER.replace("second section.\n", "second section. More.\n")
    for name, text in (("toml.md", TOML), ("pandoc.md", PANDOC)):      # review N3
        assert r[name + " shown"] == "Body text."
        assert "+++" not in r[name + " published"] and "title" not in r[name + " published"]
        assert r[name + " saved"] == text.replace("Body text.\n", "Body text. More.\n")



def test_comment_and_blank_lines_after_a_changed_entry_stay():
    text = ("---\ntitle: Hello\n# the tags below are for the site\ntags: [a]\n"
            "summary: S\n\nlayout: post\n---\n\nB\n")
    front, body = front_matter.split(text)
    details = from_front_matter(front).with_changes(title="New", summary="Other")
    assert front_matter.join(front_matter_with(details, front), body) == text.replace(
        "title: Hello", "title: New").replace("summary: S", "summary: Other")
    crlf = "---\r\ntitle: T\r\n# a comment\r\nlayout: post\r\n---\r\n\r\nB\r\n"
    front, body = front_matter.split(crlf)
    details = from_front_matter(front).with_changes(title="U")
    assert front_matter.join(front_matter_with(details, front), body) == crlf.replace(
        "title: T", "title: U")


def test_a_block_value_keeps_its_own_lines():
    text = "---\nsummary: |+\n  Line one\n  Line two\n\nslug: keep-me\n---\n\nB\n"
    front, _ = front_matter.split(text)
    assert from_front_matter(front).summary == "Line one\nLine two"
    assert unchanged_save(text) == text


# -- TOML front matter (Hugo's own archetype writes it), and Pandoc's "..." ----

TOML = ('+++\ntitle = "My TOML post"\ndate = 2024-03-01T08:00:00+01:00\n'
        'tags = ["a", "b"]  # for the site\ndraft = false\n\n[params]\ntoc = true\n+++\n\n'
        'Body text.\n')
PANDOC = "---\ntitle: Pandoc\nauthor: [A, B]\n...\n\nBody text.\n"


def test_toml_front_matter_is_read_and_kept_as_written():
    front, body = front_matter.split(TOML)
    assert body == "Body text.\n" and front.syntax == "toml"
    details = from_front_matter(front)
    assert (details.title, details.hashtags) == ("My TOML post", ("a", "b"))
    assert details.published_at == moment(2024, 3, 1, 8, 0, offset=1)
    assert unchanged_save(TOML) == TOML


def test_a_changed_detail_is_written_into_the_toml_in_place_in_toml():
    front, body = front_matter.split(TOML)
    details = from_front_matter(front)
    changed = details.with_changes(title='A "new" title', hashtags=("a", "b", "Extra"),
                                   published_at=moment(2024, 3, 2, 8, 0, offset=1),
                                   summary="Short")
    out = front_matter.join(front_matter_with(changed, front), body)
    assert out == TOML.replace('title = "My TOML post"', 'title = "A \\"new\\" title"').replace(
        'date = 2024-03-01T08:00:00+01:00', "date = 2024-03-02T08:00:00+01:00").replace(
        'tags = ["a", "b"]  # for the site', 'tags = ["a", "b", "Extra"]  # for the site').replace(
        "draft = false\n", 'draft = false\nsummary = "Short"\n')
    import tomllib
    assert tomllib.loads(out.split("+++\n")[1])["params"] == {"toc": True}   # still a table
    assert "---" not in out


def test_pandoc_front_matter_closed_by_dots_is_front_matter():
    front, body = front_matter.split(PANDOC)
    assert body == "Body text.\n" and from_front_matter(front).title == "Pandoc"
    assert unchanged_save(PANDOC) == PANDOC


KEPT_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QLabel
app = QApplication(sys.argv[:1])
import main_window

path = sys.argv[2]
w = main_window.MainWindow()
w.show()
w.nostr_state._active = True               # Article Details is a Nostr feature
w._update_nostr_commands()
w.open_path(path)
ed = w.current_editor()
w.act_article_details.setChecked(True)
app.processEvents()
panel = w._article_details_panel
r = {"cover read-only": panel.cover.url_edit.isReadOnly(),
     "title read-only": panel.title_edit.isReadOnly(),
     "notes": [l.text() for l in panel.findChildren(QLabel)
               if l.isVisibleTo(panel) and l.text().startswith("Kept")]}
w._set_article_details(ed, w._article_details_of(ed).with_changes(
    image="https://cdn.example/c.jpg"))
w._save_to(path)
r["saved"] = open(path, encoding="utf-8").read()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_cover_the_file_keeps_is_read_only_in_article_details(tmp_path):
    chirpy = "---\ntitle: T\nimage:\n  path: /a.png\n  alt: A\n---\n\nBody text.\n"
    path = tmp_path / "chirpy.md"
    path.write_text(chirpy, encoding="utf-8")
    r = run_window_script(KEPT_SCRIPT, tmp_path, str(path))
    assert r["cover read-only"] is True and r["title read-only"] is False
    assert r["notes"] == ["Kept as written in the file."]
    assert r["saved"] == chirpy


# -- only what changed changes, in the file's own style ----------------------

def test_a_tag_holding_a_comma_stays_one_tag():
    front, body = front_matter.split('---\ntags: [a, "b, c", \'d\']\n---\n\nB\n')
    details = from_front_matter(front)
    assert details.hashtags == ("a", "b, c", "d")
    added = details.with_changes(hashtags=details.hashtags + ("Extra",))
    out = front_matter.join(front_matter_with(added, front), body)
    assert "tags: [a, \"b, c\", 'd', Extra]\n" in out


def test_a_block_list_stays_a_block_list():
    text = '---\naliases:\n  - My Note\ntags:\n  - project/alpha\n  - "#idea"\npublish: true\n---\n\nB\n'
    front, body = front_matter.split(text)
    details = from_front_matter(front)
    added = details.with_changes(hashtags=details.hashtags + ("Extra",))
    assert front_matter.join(front_matter_with(added, front), body) == text.replace(
        '  - "#idea"\n', '  - "#idea"\n  - Extra\n')
    fewer = details.with_changes(hashtags=("idea",))
    assert front_matter.join(front_matter_with(fewer, front), body) == text.replace(
        "  - project/alpha\n", "")


def test_a_rewritten_line_keeps_its_alignment_quotes_and_comment():
    text = ('---\nlayout: post\ntitle:  "Welcome to Jekyll!"   # on the site\n'
            "date:   2015-11-10 15:41:45 +0100\nsummary: 'It''s here'\n---\nB\n")
    front, body = front_matter.split(text)
    details = from_front_matter(front).with_changes(
        title="A new title", summary="Now it's new",
        published_at=from_front_matter(front).published_at + 86400)
    assert front_matter.join(front_matter_with(details, front), body) == (
        '---\nlayout: post\ntitle:  "A new title"   # on the site\n'
        "date:   2015-11-11 15:41:45 +0100\nsummary: 'Now it''s new'\n---\nB\n")


def test_a_block_text_stays_a_block_text():
    text = "---\ndescription: >-\n  A long description\n  over two lines.\nlayout: post\n---\nB\n"
    front, body = front_matter.split(text)
    details = from_front_matter(front).with_changes(summary="A shorter one.")
    assert front_matter.join(front_matter_with(details, front), body) == (
        "---\ndescription: >-\n  A shorter one.\nlayout: post\n---\nB\n")


def test_toml_keeps_its_own_style_too():
    text = "+++\ntitle   = 'Literal'  # c\ntags = ['a', 'b']\n+++\nB\n"
    front, body = front_matter.split(text)
    details = from_front_matter(front)
    changed = details.with_changes(title="New", hashtags=("a", "b", "c"))
    assert front_matter.join(front_matter_with(changed, front), body) == (
        "+++\ntitle   = 'New'  # c\ntags = ['a', 'b', 'c']\n+++\nB\n")


# -- keys left empty to fill: Obsidian templates, Hugo and Jekyll archetypes ----

TEMPLATES = {
    "obsidian": "---\ntitle:\ndate:\ntags:\n---\n\nBody text.\n",
    "templater": "---\ntitle: \ndescription:\ndate: \n---\nBody text.\n",
    "hugo": '---\ntitle:\ndate:\ndraft: true\ntags: []\n---\n\nBody text.\n',
    "jekyll": "---\nlayout: post\ntitle:\ndate:\ncategories:\n---\nBody text.\n",
}


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_a_detail_set_on_an_empty_key_is_valid_yaml_and_reads_back(name):
    text = TEMPLATES[name]
    front, body = front_matter.split(text)
    assert unchanged_save(text) == text
    details = from_front_matter(front).with_changes(
        title="Hello world", summary="A summary", published_at=1791876318,
        hashtags=("one", "two"))
    out = front_matter.join(front_matter_with(details, front), body)
    assert "title: Hello world\n" in out and "date: 2026-10-13T07:25:18Z\n" in out
    again = from_front_matter(front_matter.split(out)[0])
    assert (again.title, again.summary, again.published_at, again.hashtags) == (
        "Hello world", "A summary", 1791876318, ("one", "two"))
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(out.split("---\n")[1])["title"] == "Hello world"


@pytest.mark.parametrize("written", ["~", "null", "Null", "NULL", ""])
def test_a_yaml_null_is_no_value(written):
    front, body = front_matter.split(
        f"---\ntitle: {written}\nsummary: {written}   # to fill\ntags: {written}\n---\n\nB\n")
    details = from_front_matter(front)
    assert (details.title, details.summary, details.hashtags) == ("", "", ())
    tags = details.with_changes(identifier="post").draft_tags("B")
    assert not [tag for tag in tags if tag[0] in ("title", "summary", "t")]


def test_a_quoted_null_is_the_word():
    front, _ = front_matter.split('---\ntitle: "null"\n---\n')
    assert from_front_matter(front).title == "null"


def test_a_value_set_on_a_key_with_only_a_comment_keeps_the_comment():
    front, body = front_matter.split("---\nsummary:    # to fill\ntitle: ~\n---\nB\n")
    details = from_front_matter(front).with_changes(summary="Filled", title="Set")
    assert front_matter.join(front_matter_with(details, front), body) == (
        "---\nsummary:    Filled # to fill\ntitle: Set\n---\nB\n")


# -- more shapes from the wild (review L4) -------------------------------------

def changed(text, **change):
    front, body = front_matter.split(text)
    details = from_front_matter(front).with_changes(**change)
    return front_matter.join(front_matter_with(details, front), body)



def test_a_hashtag_set_in_another_case_keeps_the_files_spelling():
    """Round 11, H2: the file's way was checked against the hashtags as set,
    not as spelled in the file, failed, and they were written in quotes and
    in lower case."""
    text = "---\ntitle: T\ntags: [Python, Rust]\n---\n\nB\n"
    assert changed(text, hashtags=("python", "rust")) == text
    assert changed(text, hashtags=("python", "rust", "go")) == text.replace("Rust]", "Rust, go]")

def test_a_toml_multi_line_list_stays_multi_line():
    text = '+++\ntitle = "T"\ntags = [\n  "go",\n  "hugo",\n]\n+++\n\nB\n'
    assert changed(text, hashtags=("go", "hugo", "extra")) == text.replace(
        '  "hugo",\n', '  "hugo",\n  "extra",\n')


def test_a_tags_comment_is_never_a_tag():
    front, _ = front_matter.split('---\ntags:\n  - post\n  - "#idea"   # with a hash\n---\n')
    assert from_front_matter(front).hashtags == ("post", "idea")


def test_jekyll_space_separated_tags_are_read_as_jekyll_reads_them():
    front, _ = front_matter.split("---\ntags: ruby jekyll\n---\n")
    assert from_front_matter(front).hashtags == ("ruby", "jekyll")
    front, _ = front_matter.split('---\ntags: "a, b"\n---\n')
    assert from_front_matter(front).hashtags == ("a", "b")


def test_hugo_capitalised_keys_are_read_and_rewritten_in_place():
    text = '---\nTitle: "Caps Keys"\nDescription: "Hugo keys"\nTags: ["a"]\n---\n\nB\n'
    front, _ = front_matter.split(text)
    details = from_front_matter(front)
    assert (details.title, details.summary, details.hashtags) == ("Caps Keys", "Hugo keys", ("a",))
    assert changed(text, title="New") == text.replace('Title: "Caps Keys"', 'Title: "New"')


def test_zola_taxonomy_tags_are_read_and_written_in_their_table():
    text = ('+++\ntitle = "Zola post"\ndate = 2024-03-01\n\n[taxonomies]\n'
            'tags = ["rust", "ssg"]\n\n[extra]\ncover = "x.png"\n+++\n\nB\n')
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("rust", "ssg")
    assert changed(text, hashtags=("rust", "ssg", "web")) == text.replace(
        'tags = ["rust", "ssg"]', 'tags = ["rust", "ssg", "web"]')


def test_a_date_that_is_no_date_is_kept_as_written():
    text = "---\ntitle: Eleventy\ndate: Last Modified\n---\n\nB\n"
    front, _ = front_matter.split(text)
    assert from_front_matter(front).published_at is None and "date" in kept_in_file(front)
    assert changed(text, published_at=1791876318) == text


def test_tags_written_as_a_string_stay_a_string():
    assert changed("---\ntags: ruby jekyll\n---\nB\n", hashtags=("ruby", "jekyll", "web")) == \
        "---\ntags: ruby jekyll web\n---\nB\n"
    assert changed('---\ntags: "a, b"\n---\nB\n', hashtags=("a", "b", "c")) == \
        '---\ntags: "a, b, c"\n---\nB\n'
    # A tag the string can't hold whole: a list then.
    assert changed("---\ntags: ruby jekyll\n---\nB\n", hashtags=("ruby", "two words")) == \
        "---\ntags: [ruby, two words]\n---\nB\n"


# -- TOML arrays over several lines, as people write them (review N1) ----------

MULTI_LINE_TOML = {
    "trailing commas": '+++\ntags = [\n  "a",\n  "b",\n]\n+++\nB\n',
    "no last comma": '+++\ntags = [\n  "a",\n  "b"\n]\n+++\nB\n',
    "comment on an item": '+++\ntags = [\n  "a",  # first\n  "b",\n]\n+++\nB\n',
    "single quotes": "+++\ntags = [\n  'a',\n  'b',\n]\n+++\nB\n",
    "two on a line": '+++\ntags = [\n  "a", "b",\n  "c",\n]\n+++\nB\n',
    "leading commas": '+++\ntags = [\n  "a"\n  , "b"\n]\n+++\nB\n',
}


@pytest.mark.parametrize("shape", sorted(MULTI_LINE_TOML))
def test_a_tag_added_to_a_toml_array_over_lines_is_valid_toml(shape):
    import tomllib
    text = MULTI_LINE_TOML[shape]
    front, _ = front_matter.split(text)
    tags = from_front_matter(front).hashtags
    out = changed(text, hashtags=tags + ("Extra",))
    assert tomllib.loads(out.split("+++\n")[1])["tags"] == list(tags) + ["Extra"]
    assert from_front_matter(front_matter.split(out)[0]).hashtags == tags + ("Extra",)
    fewer = changed(text, hashtags=tags[1:])
    assert tomllib.loads(fewer.split("+++\n")[1])["tags"] == list(tags[1:])


def test_a_toml_array_over_lines_keeps_its_lines_and_comments():
    out = changed(MULTI_LINE_TOML["no last comma"], hashtags=("a", "b", "Extra"))
    assert out == '+++\ntags = [\n  "a",\n  "b",\n  "Extra"\n]\n+++\nB\n'
    out = changed(MULTI_LINE_TOML["comment on an item"], hashtags=("a", "b", "Extra"))
    assert out == '+++\ntags = [\n  "a",  # first\n  "b",\n  "Extra",\n]\n+++\nB\n'


@pytest.mark.parametrize("written", ["", " ~", " idea"])
def test_tags_left_to_fill_or_one_word_become_a_list(written):
    """Review N2: Hugo and Eleventy read "tags: idea draft" as one tag."""
    yaml = pytest.importorskip("yaml")
    text = f"---\ntitle: x\ntags:{written}\n---\n\nB\n"
    out = changed(text, hashtags=("idea", "draft"))
    assert yaml.safe_load(out.split("---\n")[1])["tags"] == ["idea", "draft"]


def test_a_yaml_flow_list_over_lines_is_read_and_rewritten_whole():
    """Review N3: "tags: [a,\\n  b]" was read as the tag "[a"."""
    yaml = pytest.importorskip("yaml")
    text = "---\ntitle: x\ntags: [a,\n  b]   # the tags\nlayout: post\n---\n\nB\n"
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("a", "b")
    assert unchanged_save(text) == text
    out = changed(text, hashtags=("a", "b", "new"))
    assert out == "---\ntitle: x\ntags: [a, b, new]   # the tags\nlayout: post\n---\n\nB\n"
    assert yaml.safe_load(out.split("---\n")[1])["tags"] == ["a", "b", "new"]


@pytest.mark.parametrize("header", ["[taxonomies]  # the terms", "[ taxonomies ]"])
def test_zola_taxonomies_header_with_a_comment_or_spaces(header):
    import tomllib
    text = f'+++\ntitle = "Z"\n{header}\ntags = ["rust", "web"]\n+++\n\nB\n'
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("rust", "web")
    out = changed(text, hashtags=("rust", "web", "zola"))
    assert out == text.replace('["rust", "web"]', '["rust", "web", "zola"]')
    assert tomllib.loads(out.split("+++\n")[1])["taxonomies"]["tags"] == ["rust", "web", "zola"]


# -- comments in and after YAML flow lists (round 5, M1 and L4) -----------------

@pytest.mark.parametrize("text, tags", [
    ("---\ntags: [a,  # first\n  b]\n---\nB\n", ("a", "b")),
    ("---\ntags: [a,  # see [1]\n  b]   # the tags\n---\nB\n", ("a", "b")),
    ('---\ntags: ["a # not a comment",\n  b]\n---\nB\n', ("a # not a comment", "b")),
    ("---\ntags: [a, b]  # see [1]\n---\nB\n", ("a", "b")),
])
def test_a_comment_in_or_after_a_flow_list_is_no_tag(text, tags):
    yaml = pytest.importorskip("yaml")
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == tags
    assert unchanged_save(text) == text
    out = changed(text, hashtags=tags + ("new",))
    assert yaml.safe_load(out.split("---\n")[1])["tags"] == list(tags) + ["new"]
    assert from_front_matter(front_matter.split(out)[0]).hashtags == tags + ("new",)


def test_the_comments_of_a_flow_list_over_lines_are_kept_after_it():
    out = changed("---\ntags: [a,  # first\n  b]   # the tags\n---\nB\n",
                  hashtags=("a", "b", "new"))
    assert out == "---\ntags: [a, b, new]   # the tags  # first\n---\nB\n"
    out = changed("---\ntags: [a, b]  # see [1]\n---\nB\n", hashtags=("a", "b", "new"))
    assert out == "---\ntags: [a, b, new]  # see [1]\n---\nB\n"


# -- an apostrophe inside a word is no quote (round 6, H1 and L1) -------------

@pytest.mark.parametrize("typed, tags", [
    ("nostr, don't panic, books", ("nostr", "don't panic", "books")),
    ("women's history, art, music", ("women's history", "art", "music")),
    ("rock'n'roll, jazz", ("rock'n'roll", "jazz")),
    ('a, "b, c", d', ("a", "b, c", "d")),
])
def test_an_apostrophe_in_a_typed_hashtag_is_part_of_it(typed, tags):
    assert hashtags_from(typed) == tags
    # Typed or pasted into the hashtags field (tag_field.TagField), the same.
    from nostr.ui.tag_field import TagField
    field = TagField()
    field.entry.setText(typed)
    assert field.tags() == tags


@pytest.mark.parametrize("text, tags", [
    ("---\ntags: o'reilly, books\n---\nB\n", ("o'reilly", "books")),
    ("---\ntags: [women's history, art]\n---\nB\n", ("women's history", "art")),
    ("---\ntags: [don't,\n  panic]\n---\nB\n", ("don't", "panic")),
    ("---\ntags: ['it''s', b]\n---\nB\n", ("it's", "b")),
])
def test_an_apostrophe_in_a_files_hashtag_is_part_of_it(text, tags):
    yaml = pytest.importorskip("yaml")
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == tags
    out = changed(text, hashtags=tags + ("Extra",))
    parsed = yaml.safe_load(out.split("---\n")[1])["tags"]
    parsed = parsed if isinstance(parsed, list) else [t.strip() for t in parsed.split(",")]
    assert parsed == list(tags) + ["Extra"]


@pytest.mark.parametrize("text", [
    "---\ntags: [a, b]\n  # a note on the tags\nlayout: post\n---\nB\n",
    "---\ntags: [a,\n  b]\n  # a note on the tags\nlayout: post\n---\nB\n",
])
def test_a_comment_line_after_a_flow_list_stays_after_it(text):
    """Round 6, L2: such a list was kept read-only, its tags unread."""
    yaml = pytest.importorskip("yaml")
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("a", "b")
    out = changed(text, hashtags=("a", "b", "new"))
    assert out == "---\ntags: [a, b, new]\n  # a note on the tags\nlayout: post\n---\nB\n"
    assert yaml.safe_load(out.split("---\n")[1])["tags"] == ["a", "b", "new"]


@pytest.mark.parametrize("text, tags", [
    ("---\ntags: [a, [b, c], d]\n---\nB\n", ("a", "d")),
    ("---\ntags: [a, {b: c}]\n---\nB\n", ("a",)),
])
def test_a_list_nested_in_the_tags_keeps_them_as_written(text, tags):
    """Round 6, L3: "[b" and "c]" were read, and would be published, as
    tags. The tags around them are tags (round 8)."""
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == tags and "tags" in kept_in_file(front)
    assert changed(text, hashtags=("x",)) == text


@pytest.mark.parametrize("text, expected", [
    ('---\n"tags": [a, b]\n---\nB\n', '---\n"tags": [a, b, Extra]\n---\nB\n'),
    ("---\n'tags': [a, b]\n---\nB\n", "---\n'tags': [a, b, Extra]\n---\nB\n"),
    ('+++\n"tags" = ["a", "b"]\n+++\nB\n', '+++\n"tags" = ["a", "b", "Extra"]\n+++\nB\n'),
])
def test_a_quoted_key_is_its_key(text, expected):
    """Round 6, L4: a second "tags" was written beside a quoted one."""
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("a", "b")
    assert changed(text, hashtags=("a", "b", "Extra")) == expected


# -- an escaped quote inside a quoted hashtag (round 7, H1) --------------------

@pytest.mark.parametrize("text, tags", [
    ("---\ntags: ['it''s, ok', b]\n---\nB\n", ("it's, ok", "b")),
    ('---\ntags: ["say \\"hi\\", there", b]\n---\nB\n', ('say "hi", there', "b")),
    ('---\ntags: ["a\\\\", b]\n---\nB\n', ("a\\", "b")),
])
def test_an_escaped_quote_stays_inside_its_hashtag(text, tags):
    yaml = pytest.importorskip("yaml")
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == tags
    for wanted in (tags[1:], tags + ("Extra",)):
        out = changed(text, hashtags=wanted)
        assert yaml.safe_load(out.split("---\n")[1])["tags"] == list(wanted)


@pytest.mark.parametrize("tags", [
    ('a "b", c', "d"), ('a\\", b', "c"), ("it's, ok", "x"), ('say "hi"',),
])
def test_a_hashtag_with_quotes_and_commas_comes_back_from_the_field(tags):
    from nostr.ui.tag_field import TagField
    field = TagField()
    field.set_tags(tags)
    assert field.tags() == tags


# -- what is read is what a YAML or TOML parser reads (round 7) ----------------

ALL_DETAILS = frozenset({"title", "summary", "image", "tags", "slug", "date",
                         "coauthors", "zap_split", "lang", "nostr_tags"})


@pytest.mark.parametrize("text, kept, shown", [
    # A mapping in the list, as YAML reads "a: b" there (L2): no hashtag.
    ("---\ntags: [a: b, c]\n---\nB\n", {"tags"}, {"hashtags": ("c",)}),
    # A key written the explicit way, and a dotted TOML key (L1).
    # ("? tags" is no key to this reader: the title's entry goes on there.)
    ("---\ntitle: T\n? tags\n: [a]\n---\nB\n", {"title", "tags"},
     {"title": "T", "hashtags": ("a",)}),
    ('+++\ntitle = "T"\n"tags".x = "a"\n+++\nB\n', {"tags"}, {"title": "T", "hashtags": ()}),
    # An anchor, and an escape this reader does not know: as YAML reads them.
    ("---\ntitle: &t Hello\n---\nB\n", {"title"}, {"title": "Hello"}),
    ('---\nsummary: "\\x41"\n---\nB\n', {"summary"}, {"summary": "A"}),
])
def test_a_detail_read_otherwise_by_a_parser_is_shown_as_it_reads_it_and_kept(
        text, kept, shown):
    """Kept as written means MyEditor does not change it there: it is still
    shown, and published, as a parser reads it (round 8)."""
    front, _ = front_matter.split(text)
    assert kept_in_file(front) == frozenset(kept)
    read = from_front_matter(front)
    for field, value in shown.items():
        assert getattr(read, field) == value
    for detail in kept:
        change = {"title": {"title": "New"}, "summary": {"summary": "New"},
                  "tags": {"hashtags": ("new",)}}.get(detail)
        assert change is None or changed(text, **change) == text


@pytest.mark.parametrize("line, field, shown", [
    ("title: yes", "title", "yes"), ("title: No", "title", "No"), ("title: True", "title", "True"),
    ("title: 12:30", "title", "12:30"), ("title: 1.10", "title", "1.10"), ("title: .5", "title", ".5"),
    ("title: 0x1F", "title", "0x1F"), ("title: 010", "title", "010"),
    ("title: 1_000", "title", "1_000"), ("title: .inf", "title", ".inf"),
    ("title: 1984", "title", "1984"), ("slug: yes", "identifier", "yes"),
    ("slug: 1.10", "identifier", "1.10"),
    ("tags: [python, 3.10]", "hashtags", ("python", "3.10")),
    ("tags: [on, off]", "hashtags", ("on", "off")), ("tags: [yes, no]", "hashtags", ("yes", "no")),
    ("tags: [null, a]", "hashtags", ("null", "a")), ("tags: 1.10", "hashtags", ("1.10",)),
])
def test_a_value_yaml_reads_as_another_type_is_shown_as_written(line, field, shown):
    """Round 8, H2: these read as empty, read-only. Shown and published as
    the file writes them, and a change is written quoted, as text."""
    yaml = pytest.importorskip("yaml")
    text = f"---\n{line}\nlayout: post\n---\nB\n"
    front, _ = front_matter.split(text)
    assert getattr(from_front_matter(front), field) == shown
    assert kept_in_file(front) == frozenset()
    new = ("python", "3.11") if field == "hashtags" else "1985"
    out = changed(text, **{field: new})
    read = yaml.safe_load(out.split("---\n")[1])
    key = {"title": "title", "identifier": "slug", "hashtags": "tags"}[field]
    assert read[key] == (list(new) if field == "hashtags" else new) and read["layout"] == "post"


def test_a_toml_number_is_shown_as_written():
    text = '+++\ntitle = 2024\ntags = ["python", 3.10]\n+++\nB\n'
    front, _ = front_matter.split(text)
    read = from_front_matter(front)
    assert (read.title, read.hashtags) == ("2024", ("python", "3.10"))
    assert kept_in_file(front) == frozenset()
    assert changed(text, title="1985") == text.replace("title = 2024", 'title = "1985"')


def test_a_key_written_twice_is_read_as_a_parser_reads_it():
    """PyYAML keeps the last of two "title" keys; so does this reader.
    Emptied, both go (round 8, L2): the first never comes back."""
    text = "---\ntitle: First\ntitle: Last\nlayout: post\n---\nB\n"
    front, _ = front_matter.split(text)
    assert from_front_matter(front).title == "Last" and kept_in_file(front) == frozenset()
    assert changed(text, title="") == "---\nlayout: post\n---\nB\n"


@pytest.mark.parametrize("style", [">", ">-", ">+"])
@pytest.mark.parametrize("summary", ["Line one\nLine two", "One\n\nTwo after a gap", "Just one"])
def test_a_folded_summary_keeps_its_line_breaks(style, summary):
    """In a ">" block YAML reads a single line break as a space: one in the
    summary is written as an empty line, as YAML reads it back."""
    yaml = pytest.importorskip("yaml")
    text = f"---\ndescription: {style}\n  A long description\n  over two lines.\nlayout: post\n---\nB\n"
    out = changed(text, summary=summary)
    assert out.startswith(f"---\ndescription: {style}\n")
    read = yaml.safe_load(out.split("---\n")[1])["description"]
    assert read.strip("\n") == summary
    assert from_front_matter(front_matter.split(out)[0]).summary == summary


@pytest.mark.parametrize("text, expected", [
    ('+++\ntitle = "T"\n  tags = ["a"]\n+++\nB\n', '+++\ntitle = "T"\n  tags = ["a", "Extra"]\n+++\nB\n'),
    ('+++\ntitle = "T"\n  "tags" = ["a"]\n+++\nB\n',
     '+++\ntitle = "T"\n  "tags" = ["a", "Extra"]\n+++\nB\n'),
    ('+++\ntitle = "T"\n\n[taxonomies]\n  tags = ["a"]\n+++\nB\n',
     '+++\ntitle = "T"\n\n[taxonomies]\n  tags = ["a", "Extra"]\n+++\nB\n'),
    ('+++\ntitle = "T"\n  [taxonomies]\n  tags = ["a"]\n+++\nB\n',
     '+++\ntitle = "T"\n  [taxonomies]\n  tags = ["a", "Extra"]\n+++\nB\n'),
])
def test_an_indented_toml_key_is_its_key(text, expected):
    """Round 7, L1: TOML lets a key be indented; it was not seen."""
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("a",) and kept_in_file(front) == frozenset()
    assert changed(text, hashtags=("a", "Extra")) == expected


def test_an_item_holding_an_equals_sign_stays_in_its_toml_list():
    text = '+++\ntitle = "T"\ntags = [\n  "a = b",\n  "c",\n]\n+++\nB\n'
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == ("a = b", "c")
    assert changed(text, hashtags=("a = b", "c", "d")) == text.replace('  "c",\n', '  "c",\n  "d",\n')


# -- a tab, and a front matter no parser reads (round 8, H1) --------------------

TABBED = ("---\ntitle: My post\nslug: my-first-post\ndate: 2024-03-01\ntags: [a,\t# the first\n  b]\n"
          "image: /c.png\nsummary: S\ndraft: false\t\nlayout:\tpost\t# Jekyll's\n---\nB\n")


def test_a_tab_after_text_reads_as_a_space_as_jekyll_and_hugo_read_it():
    """PyYAML's own reader refuses these tabs; libyaml, Jekyll and Hugo
    take them as spaces. Every detail was read as empty."""
    front, _ = front_matter.split(TABBED)
    details = from_front_matter(front)
    assert (details.title, details.identifier, details.hashtags, details.image,
            details.summary) == ("My post", "my-first-post", ("a", "b"), "/c.png", "S")
    assert details.published_at == moment(2024, 3, 1)
    assert kept_in_file(front) == frozenset()
    assert changed(TABBED, title="New") == TABBED.replace("title: My post", "title: New")


UNREAD = "---\ntitle: T\nslug: my-post\ntags: [a, b]\nnotes: [unclosed\n---\nB\n"


def test_a_front_matter_no_parser_reads_shows_its_details_kept_as_written():
    """Read as MyEditor reads it, every detail read-only: never as empty,
    so the article keeps its title and its address."""
    front, _ = front_matter.split(UNREAD)
    details = from_front_matter(front)
    assert (details.title, details.identifier, details.hashtags) == ("T", "my-post", ("a", "b"))
    assert kept_in_file(front) == ALL_DETAILS
    assert changed(UNREAD, title="New", hashtags=("c",)) == UNREAD


# -- keys with spaces, and the last detail taken out (round 8, H3) -------------

@pytest.mark.parametrize("text, change, expected", [
    ("---\ncreated at: 2024-01-01\ntags: [idea]\n---\n\nBody.\n", {"hashtags": ()},
     "---\ncreated at: 2024-01-01\n---\n\nBody.\n"),
    ("---\nRelated Notes: x\ntags: [idea]\n---\n\nBody.\n", {"hashtags": ()},
     "---\nRelated Notes: x\n---\n\nBody.\n"),
    ("---\ncreated at: 2024-01-01\ntitle: T\n---\n\nBody.\n", {"title": ""},
     "---\ncreated at: 2024-01-01\n---\n\nBody.\n"),
    ("---\ntitle: T\nDate Created: 2024-01-01\nsummary: S\n---\nB\n", {"title": "U"},
     "---\ntitle: U\nDate Created: 2024-01-01\nsummary: S\n---\nB\n"),
])
def test_a_key_with_spaces_is_a_key_and_stays(text, change, expected):
    """Obsidian's properties may hold spaces: such a key went unseen, and
    taking out the last detail took the whole front matter with it."""
    yaml = pytest.importorskip("yaml")
    out = changed(text, **change)
    assert out == expected
    front, _ = front_matter.split(text)
    assert from_front_matter(front).title == ("T" if "title: T" in text else "")
    assert yaml.safe_load(out.split("---\n")[1]) == {
        key: value for key, value in yaml.safe_load(text.split("---\n")[1]).items()
        if key not in ("tags", "title")} | ({"title": "U"} if change.get("title") else {})


def test_a_detail_is_never_taken_out_with_the_front_matter_around_it():
    """Only comments would be left: the block would read as text."""
    text = "---\n# my notes\ntags: [idea]\n---\n\nBody.\n"
    front, body = front_matter.split(text)
    written = front_matter_with(from_front_matter(front).with_changes(hashtags=()), front)
    again, rest = front_matter.split(front_matter.join(written, body))
    assert rest == body and again is not None


@pytest.mark.parametrize("odd", [" ", "\x85", "\x0c", " ", "\x1c"])
def test_a_line_break_yaml_does_not_end_a_line_on_stays_as_written(odd):
    """Round 8, L3: a save with no change wrote these as line breaks. (A
    form feed or U+001C is no character YAML reads in a file at all: that
    front matter stays as written whatever is set.)"""
    text = f'---\ntitle: "Hello{odd}World"\nlayout: post\n---\nB\n'
    assert unchanged_save(text) == text
    crlf = text.replace("\n", "\r\n")
    assert unchanged_save(crlf) == crlf
    added = text.replace("layout: post\n", "layout: post\nsummary: S\n")
    assert changed(text, summary="S") == (text if odd in "\x0c\x1c" else added)


@pytest.mark.parametrize("text, change, expected", [
    ("---\nsummary: S\ndescription: D\nlayout: post\n---\nB\n", {"summary": ""},
     "---\nlayout: post\n---\nB\n"),
    ("---\nimage: /a.png\ncover: /b.png\nlayout: post\n---\nB\n", {"image": ""},
     "---\nlayout: post\n---\nB\n"),
    ('+++\ntags = ["a"]\ndraft = true\n\n[taxonomies]\ntags = ["b"]\n+++\nB\n', {"hashtags": ()},
     '+++\ndraft = true\n\n[taxonomies]\n+++\nB\n'),
])
def test_a_cleared_detail_is_gone_from_every_key_it_is_read_from(text, change, expected):
    """Round 8, L2: the other key came back as the detail on reopening."""
    out = changed(text, **change)
    assert out == expected
    again = from_front_matter(front_matter.split(out)[0])
    assert (again.summary, again.image, again.hashtags) == ("", "", ())


KEPT_CHANGED_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.article_details import ArticleDetails

path = sys.argv[2]
w = main_window.MainWindow()
w.show()
w.nostr_state._active = True               # Article Details is a Nostr feature
w._update_nostr_commands()
w.open_path(path)
ed = w.current_editor()
w.act_article_details.setChecked(True)
app.processEvents()
panel = w._article_details_panel
# What the publish window hands back: a title typed over the kept one.
w._set_article_details(ed, ArticleDetails(title="A real title", summary="New"))
app.processEvents()
r = {"title": w._article_details_of(ed).title, "shown": panel.title_edit.text(),
     "read-only": panel.title_edit.isReadOnly()}
w._save_to(path)
r["saved"] = open(path, encoding="utf-8").read()
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_kept_detail_changed_in_another_window_stays_as_in_the_file(tmp_path):
    """Round 8, M2: the publish window's title went into the tab, was
    dropped at saving without a word, and showed under "Kept as written"."""
    text = "---\ntitle: &t Hello\nsummary: S\n---\n\nBody text.\n"
    path = tmp_path / "anchored.md"
    path.write_text(text, encoding="utf-8")
    r = run_window_script(KEPT_CHANGED_SCRIPT, tmp_path, str(path))
    assert (r["title"], r["shown"], r["read-only"]) == ("Hello", "Hello", True)
    assert r["saved"] == text.replace("summary: S", "summary: New")


ZOLA_DOTTED = '+++\ntitle = "Z"\ntaxonomies.tags = ["a"]\n+++\n\nBody.\n'
ZOLA_INLINE = '+++\ntitle = "Z"\ntaxonomies = { tags = ["a"] }\n+++\n\nBody.\n'


def test_zolas_tags_written_as_a_dotted_key_are_read_and_changed():
    """Round 8, L6."""
    front, _ = front_matter.split(ZOLA_DOTTED)
    assert from_front_matter(front).hashtags == ("a",) and kept_in_file(front) == frozenset()
    assert changed(ZOLA_DOTTED, hashtags=("a", "b")) == ZOLA_DOTTED.replace(
        '["a"]', '["a", "b"]')
    assert changed(ZOLA_DOTTED, hashtags=()) == ZOLA_DOTTED.replace(
        'taxonomies.tags = ["a"]\n', "")


def test_zolas_tags_in_an_inline_table_are_shown_and_kept_as_written():
    front, _ = front_matter.split(ZOLA_INLINE)
    assert from_front_matter(front).hashtags == ("a",) and kept_in_file(front) == {"tags"}
    assert changed(ZOLA_INLINE, hashtags=("b",)) == ZOLA_INLINE


@pytest.mark.parametrize("tags", [("a", '"', "c"), ('"x, y', "z"), ("a", "'b, c'")])
def test_tags_written_as_a_string_read_back_as_the_tags_meant(tags):
    """Found by fuzzing (round 8): a tag a comma string can't hold whole
    was checked against the string's own reading, not the tags meant."""
    text = "---\ntags: a, b\nlayout: post\n---\nB\n"
    out = changed(text, hashtags=tags)
    assert from_front_matter(front_matter.split(out)[0]).hashtags == tags


@pytest.mark.parametrize("text", [
    '+++\ntitle = "T"\ntags = [\n  1,\n  "b",  # a tag\n  3.10,\n  1e3, 0x1F,\n]\n+++\n\nBody.\n',
    '+++\ntitle = "T"\n\n[taxonomies]\ntags = [\n  1,\n  "b",\n  3.10, 1e3,\n  0x1F\n]\n+++\n\nBody.\n',
    '+++\ntitle = "T"\ntaxonomies.tags = [\n  1, "b", 3.10,\n  1e3, 0x1F]\n+++\n\nBody.\n',
])
def test_a_toml_list_over_lines_holding_numbers_reads_them_as_written(text):
    """Round 8, H2: shown, but kept as written, as its numbers had no
    reading of their own over lines. Round 9, H2: then read as Python
    prints them ("3.1" for 3.10), shown, published and written so."""
    tags = ("1", "b", "3.10", "1e3", "0x1F")
    front, _ = front_matter.split(text)
    assert from_front_matter(front).hashtags == tags and kept_in_file(front) == frozenset()
    out = changed(text, hashtags=tags + ("c",))
    assert from_front_matter(front_matter.split(out)[0]).hashtags == tags + ("c",)


# -- line endings as each line has them -----------------------------------------

@pytest.mark.parametrize("text", [
    "---\ntitle: x\r\ntags: [a]\n---\n\nB\n",
    "---\r\ntitle: x\ntags: [a]\r\n---\r\n\n\r\nB\n",
    '+++\ntitle = "x"\r\n[taxonomies]\ntags = ["a"]\r\n+++\n\nB\n',
    "﻿---\r\ntitle: x\n---\nB",
    "---\ntitle: x\n---",
    "---\n\n---\nB\n",
])
def test_a_save_with_no_change_keeps_every_line_ending_as_written(text):
    """A front matter mixing CRLF and LF was saved with one of them on every
    line: a file changed that no one asked to change."""
    assert unchanged_save(text) == text


def test_a_changed_front_matter_keeps_the_endings_of_the_lines_it_keeps():
    text = "---\r\ntitle: x\nlayout: post\r\n---\n\nB\n"
    assert changed(text, title="New") == text.replace("title: x", "title: New")
    # A new line takes the ending of the line it follows.
    assert changed(text, summary="S") == text.replace("layout: post\r\n",
                                                      "layout: post\r\nsummary: S\r\n")
    assert changed(text, title="") == text.replace("title: x\n", "")


def test_the_window_saves_a_front_matter_with_its_own_line_endings(tmp_path):
    """The file is read and saved with its line endings made one; its
    front matter keeps each line's own, its text the file's own."""
    paths = []
    for name, text in (("mixed.md", "---\r\ntitle: x\nlayout: post\r\n---\n\nBody text.\n"),
                       ("lone.md", "---\ntitle: a\rb\nlayout: post\n---\n\nBody text.\n"),
                       ("crlf.md", "---\r\ntitle: x\r\n---\r\n\r\nBody text.\r\n")):
        path = tmp_path / name
        path.write_bytes(text.encode("utf-8"))
        paths.append((str(path), text))
    r = run_window_script(SCRIPT, tmp_path, *[path for path, _text in paths])
    for path, text in paths:
        assert r[os.path.basename(path) + " saved"] == text.replace(
            "Body text.", "Body text. More."), path


# A file that is only a front matter, its closing line without a line
# ending, as VS Code saves one by default.
ENDS_THE_FILE = {"lf.md": "---\ntitle: Only\ntags: [a]\n---",
                 "crlf.md": "---\r\ntitle: Only\r\ntags: [a]\r\n---",
                 "cr.md": "---\rtitle: Only\r---",
                 "toml.md": '+++\ntitle = "Only"\n+++',
                 "dots.md": "---\ntitle: Only\n..."}


@pytest.mark.parametrize("text", ENDS_THE_FILE.values())
def test_a_text_under_a_front_matter_ending_the_file_starts_on_a_line_of_its_own(text):
    """Round 11, H1: the text went on the closing line ("---My text"), and
    the file had no front matter any more. With no text, it stays as it was."""
    front, body = front_matter.split(text)
    assert body == ""
    titled = front_matter_with(from_front_matter(front).with_changes(title="New"), front)
    for written, expected in ((front, text), (titled, text.replace("Only", "New"))):
        assert front_matter.join(written, "") == expected
        assert front_matter.join(written, "My text.\n") == expected + front.newline + "My text.\n"


TYPED_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.show()
r = {}
for path in sys.argv[2:]:
    ed = w.open_path(path)
    if os.path.basename(path).startswith("t_"):
        w._set_article_details(ed, w._article_details_of(ed).with_changes(title="New"))
    c = ed.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    c.insertText("My first paragraph.")
    w._save_to(path)
    r[os.path.basename(path)] = open(path, "rb").read().decode("utf-8")
    ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_the_window_types_under_a_front_matter_ending_the_file(tmp_path):
    paths = []
    for name, text in ENDS_THE_FILE.items():
        for prefix in ("", "t_"):
            path = tmp_path / (prefix + name)
            path.write_bytes(text.encode("utf-8"))
            paths.append(str(path))
    r = run_window_script(TYPED_SCRIPT, tmp_path, *paths)
    for name, text in ENDS_THE_FILE.items():
        newline = front_matter.split(text)[0].newline
        typed = newline + "My first paragraph." + newline
        assert r[name] == text + typed, name
        assert r["t_" + name] == text.replace("Only", "New") + typed, name


# -- a lone CR ends a line, as YAML reads it (round 9, H1) ----------------------

CR_ONLY = "---\rtitle: CR Title\rtags: [x]\r---\r\rBody.\r"


@pytest.mark.parametrize("text", [CR_ONLY, "---\r\ntitle: CR Title\rtags: [x]\n---\r\rBody.\r"])
def test_a_front_matter_whose_lines_end_in_a_lone_cr_is_found(text):
    """Classic Mac line endings: the front matter was not found, its text
    published as the article, and a save wrote a second front matter."""
    front, body = front_matter.split(text)
    assert front is not None and body == "Body.\r"
    details = from_front_matter(front)
    assert (details.title, details.hashtags) == ("CR Title", ("x",))
    assert unchanged_save(text) == text
    assert changed(text, title="New") == text.replace("CR Title", "New")


def test_the_window_finds_a_front_matter_with_lone_cr_line_endings(tmp_path):
    path = tmp_path / "cr_only.md"
    path.write_bytes(CR_ONLY.encode("utf-8"))
    r = run_window_script(SCRIPT, tmp_path, str(path))
    assert r["cr_only.md shown"] == "Body."
    # Its text keeps its lone CR too (round 10, M1): no file of two styles.
    assert r["cr_only.md saved"] == CR_ONLY.replace("Body.", "Body. More.")


@pytest.mark.parametrize("text, expected", [
    ("---\ntags:\n  - python   # main\n  - 3.10     # the version\nlayout: post\n---\nB\n",
     "---\ntags:\n  - python   # main\n  - 3.10     # the version\n  - added\nlayout: post\n---\nB\n"),
    ("---\ntags:\n  - python\n  - yes\n---\nB\n", "---\ntags:\n  - python\n  - yes\n  - added\n---\nB\n"),
    ("---\ntags: [python, 3.10]\n---\nB\n", "---\ntags: [python, 3.10, added]\n---\nB\n"),
])
def test_a_list_holding_a_number_keeps_its_style_when_a_tag_is_added(text, expected):
    """Round 9, M1: written again as a quoted flow list, its comments gone."""
    front, _ = front_matter.split(text)
    assert changed(text, hashtags=from_front_matter(front).hashtags + ("added",)) == expected


def test_a_number_typed_into_a_list_is_written_as_text():
    text = "---\ntags:\n  - python\n  - 3.10\n---\nB\n"
    assert changed(text, hashtags=("python", "3.10", "3.11")) == text.replace(
        "  - 3.10\n", '  - 3.10\n  - "3.11"\n')


RESTORED_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from recovery import read_backup

w = main_window.MainWindow()
w.show()
r = {}
for path in sys.argv[2:]:
    ed = w.open_path(path)
    c = ed.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    c.insertText(" More.")
    ed._backup.write_now()
    record = read_backup(ed._backup.path)
    ed.document().setModified(False)
    w.close_tab(w.tabs.currentIndex())
    app.processEvents()
    restored = w._restore_one_backup(record)
    w.tabs.setCurrentIndex(w.tabs.count() - 1)
    w._save_to(path)
    r[os.path.basename(path)] = open(path, "rb").read().decode("utf-8")
    restored.document().setModified(False)
    w.close_tab(w.tabs.currentIndex())
    app.processEvents()
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_restored_tab_saves_with_the_line_endings_its_file_had(tmp_path):
    """Round 9, M2: a tab restored from its backup saved its text with this
    system's line ending under a front matter keeping the file's own."""
    texts = {"crlf.md": "---\r\ntitle: A\r\ntags: [x]\r\n---\r\n\r\nBody one.\r\nBody two.\r\n",
             "bom_crlf.md": "﻿---\r\ntitle: A\r\n---\r\n\r\nBody.\r\n",
             "toml_crlf.md": '+++\r\ntitle = "A"\r\n+++\r\n\r\nBody.\r\n'}
    paths = []
    for name, text in texts.items():
        (tmp_path / name).write_bytes(text.encode("utf-8"))
        paths.append(str(tmp_path / name))
    r = run_window_script(RESTORED_SCRIPT, tmp_path, *paths)
    for name, text in texts.items():
        saved = r[name]
        assert "More." in saved and saved.startswith(text.split("Body")[0]), name
        assert saved.count("\n") == saved.count("\r\n"), (name, saved)   # every one CRLF


def test_a_texts_line_ending_is_counted_over_its_text_alone(tmp_path):
    """Round 9, L1: a CRLF front matter longer than its LF text made the
    text CRLF on saving."""
    text = "---\r\ntitle: A\r\ntags: [x]\r\nslug: s\r\n---\r\n\r\nBody one.\n\nBody text.\n"
    path = tmp_path / "lf_text.md"
    path.write_bytes(text.encode("utf-8"))
    r = run_window_script(SCRIPT, tmp_path, str(path))
    assert r["lf_text.md saved"] == text.replace("Body text.", "Body text. More.")


@pytest.mark.parametrize("text, field, shown", [
    ("---\ntitle: [Part, One]\n---\nB\n", "title", "Part, One"),
    ("---\ntitle:\n  - Part\n  - 2.10\n---\nB\n", "title", "Part, 2.10"),
    ("---\ntitle:\n  en: Hello\n  de: Hallo\n---\nB\n", "title", "Hello"),
    ("---\nsummary: [One, Two]\n---\nB\n", "summary", "One, Two"),
    ("---\ntitle: [Part, One]\nnotes: [unclosed\n---\nB\n", "title", "Part, One"),
])
def test_a_text_written_as_a_list_or_a_mapping_is_shown_kept_as_written(text, field, shown):
    """Round 9, L2: shown empty (and a list one could be typed over)."""
    front, _ = front_matter.split(text)
    assert getattr(from_front_matter(front), field) == shown
    assert {"title": "title", "summary": "summary"}[field] in kept_in_file(front)
    assert changed(text, **{field: "New"}) == text


LAZY_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import front_matter
import main_window

read = []
real = front_matter._parsed_text
def counted(*args):
    read.append(1)
    return real(*args)
front_matter._parsed_text = counted

path = sys.argv[2]
w = main_window.MainWindow()
w.show()
ed = w.open_path(path)
app.processEvents()
c = ed.textCursor()
c.movePosition(QTextCursor.MoveOperation.End)
c.insertText(" More.")
w._save_to(path)
ed._backup.write_now()
r = {"read at open and save": len(read), "saved": open(path, encoding="utf-8").read()}
r["title"] = w._article_details_of(ed).title
r["read for the details"] = len(read) > 0
ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""


def test_opening_and_saving_a_file_reads_no_article_details(tmp_path):
    """Round 9, L3: every open read the front matter with a YAML parser and
    tried changes on it, for everyone, Nostr or not: 18 s for 1 MB. The
    details are read when something asks for them."""
    text = "---\ntitle: Lazy\ntags: [a]\n---\n\nBody text.\n"
    path = tmp_path / "lazy.md"
    path.write_text(text, encoding="utf-8")
    r = run_window_script(LAZY_SCRIPT, tmp_path, str(path))
    assert r["read at open and save"] == 0
    assert r["saved"] == text.replace("Body text.", "Body text. More.")
    assert r["title"] == "Lazy" and r["read for the details"] is True


def test_what_is_kept_is_worked_out_once_per_front_matter(monkeypatch):
    import nostr.article_details as module
    front, _ = front_matter.split("---\ntitle: Once\ntags: [a]\n---\nB\n")
    tried = []
    real = module._written
    monkeypatch.setattr(module, "_written", lambda *args: tried.append(1) or real(*args))
    kept_in_file(front)
    first = len(tried)
    kept_in_file(front_matter.split("---\ntitle: Once\ntags: [a]\n---\nB\n")[0])
    assert first > 0 and len(tried) == first


def test_a_huge_front_matter_is_not_tried_change_by_change(monkeypatch):
    """Each trial reads the whole front matter again: past a size, a change
    is checked only when it is made."""
    import nostr.article_details as module
    lines = "".join(f"key{n}: value {n}\n" for n in range(4000))
    front, _ = front_matter.split(f"---\ntitle: Big\n{lines}---\nB\n")
    tried = []
    real = module._written
    monkeypatch.setattr(module, "_written", lambda *args: tried.append(1) or real(*args))
    assert kept_in_file(front) == frozenset() and tried == []
    assert from_front_matter(front).title == "Big"


ALL_KEPT_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QLabel
app = QApplication(sys.argv[:1])
import main_window

w = main_window.MainWindow()
w.resize(700, 640)
w.show()
w.nostr_state._active = True               # Article Details is a Nostr feature
w._update_nostr_commands()
w.open_path(sys.argv[2])
w.act_article_details.setChecked(True)
app.processEvents()
panel = w._article_details_panel
r = {"notes": [label.text() for label in panel.findChildren(QLabel)
               if label.isVisibleTo(panel) and "kept as written" in label.text().lower()],
     "title": panel.title_edit.text(), "title from": panel.title_edit.cursorPosition(),
     "cover hint": [label.text() for label in panel.cover.findChildren(QLabel)
                    if label.isVisibleTo(panel) and "media servers" in label.text()]}
w.close()
print("RESULT " + json.dumps(r))
"""


def test_a_front_matter_no_parser_reads_says_so_once_in_article_details(tmp_path):
    """Round 9, L4: the note stood under each of six fields, and a long
    read-only title showed its end."""
    title = "A Long Title That Does Not Fit In The Field Of The Smallest Window"
    path = tmp_path / "unread.md"
    path.write_text(f"---\ntitle: {title}\nimage: /a.png\nnotes: [unclosed\n---\n\nB\n",
                    encoding="utf-8")
    r = run_window_script(ALL_KEPT_SCRIPT, tmp_path, str(path))
    assert r["notes"] == ["Every detail is kept as written in the file: MyEditor can’t read "
                          "its front matter in full."]
    assert r["title"] == title and r["title from"] == 0 and r["cover hint"] == []


def test_a_lone_cr_ends_no_line_of_toml():
    """Round 10, L3: TOML ends a line at LF or CRLF only, and tomllib reads
    no front matter whose lines end in a lone CR. Such a front matter is
    one no parser reads: its details shown as this reader reads them, kept
    as written, and never a second front matter written above it."""
    cr_toml = '+++\rtitle = "T"\rtags = ["x", 3.10]\r+++\r\rBody.\r'
    front, body = front_matter.split(cr_toml)
    assert front is not None and body == "Body.\r"
    assert from_front_matter(front).title == "T" and kept_in_file(front) == ALL_DETAILS
    assert unchanged_save(cr_toml) == cr_toml and changed(cr_toml, title="New") == cr_toml
    # A lone CR inside a TOML text: tomllib reads no front matter there, so
    # every detail stays as written, the title shown as this reader reads it.
    text = '+++\ntitle = "T"\nsummary = """one\rtwo"""\n+++\n\nBody.\n'
    front, _ = front_matter.split(text)
    assert from_front_matter(front).title == "T" and kept_in_file(front) == ALL_DETAILS
    assert unchanged_save(text) == text and changed(text, title="New") == text


def test_a_toml_list_over_lines_holding_numbers_keeps_its_lines_and_comments():
    """Round 10, L2: written on one line, every item quoted, its comments
    gone, when a hashtag changed."""
    text = ('+++\ntitle = "T"\ntags = [\n  "python", # a\n  3.10,     # b\n  1e3,\n  0x1F,\n'
            '  true,\n]\n+++\n\nBody.\n')
    front, _ = front_matter.split(text)
    tags = from_front_matter(front).hashtags
    assert changed(text, hashtags=tags + ("added",)) == text.replace(
        "  true,\n]", '  true,\n  "added",\n]')
    assert changed(text, hashtags=tags[1:]) == text.replace('  "python", # a\n', "")
