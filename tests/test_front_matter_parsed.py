# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every change MyEditor writes to a front matter is read by a YAML parser
(or tomllib) as that change and nothing else, or is not written at all.

Held over front matter collected from the wild and from reviews
(front_matter_corpus/, one shape each, valid or not) and the samples of
test_front_matter.py: for each change, either the detail is kept as written
(read-only, the file untouched) or the parser reads the old front matter
again with only that detail changed, to what was set.
"""

import functools
import pathlib
import re
import tomllib
from datetime import date, datetime, timezone

import pytest
import yaml

import front_matter
from nostr import article_details
from nostr.article_details import (
    from_front_matter, front_matter_with, kept_in_file)
from tests.test_front_matter import HUGO, JEKYLL, TOML

CORPUS = pathlib.Path(__file__).parent / "front_matter_corpus"
SAMPLES = [(path.name, path.read_bytes().decode("utf-8")) for path in sorted(CORPUS.iterdir())] \
    + [("hugo", HUGO), ("jekyll", JEKYLL), ("toml", TOML)]
# Each detail's names, as article_details reads them.
NAMES = {"title": {"title"}, "summary": {"summary", "description"},
         "image": {"image", "cover", "cover_image"}, "tags": {"tags", "hashtags"},
         "slug": {"slug"}, "date": {"date", "published_at"},
         "lang": {"lang", "language"}, "nostr_tags": {"nostr_tags"}}
WHEN = 1791876318
MISSING = object()


def changes(details):
    """(detail, change, what the parser should read for it) of each change tried."""
    import json
    from nostr.content_language import is_language_tag, language_tags
    language_carried = tuple(t for t in details.carried if not is_language_tag(t)) + tuple(tuple(t) for t in language_tags('th'))
    # A translator's credit is the one carried tag a file keeps (red-a-5).
    carried = details.carried + (('p', 'ab' * 32, '', 'translator'),)
    written = json.dumps([list(t) for t in carried if t[0] == 'p'], ensure_ascii=False, separators=(',', ':'))
    return [
        ('lang', {'carried': language_carried}, 'th'),
        ('nostr_tags', {'carried': carried}, written),
        ("tags", {"hashtags": details.hashtags + ("Extra",)}, details.hashtags + ("Extra",)),
        ("tags", {"hashtags": details.hashtags[1:]}, details.hashtags[1:]),
        ("tags", {"hashtags": ()}, ()),
        ("tags", {"hashtags": ("it's, ok", 'say "hi"')}, ("it's, ok", 'say "hi"')),
        ("title", {"title": "Hello world"}, "Hello world"),
        ("title", {"title": 'Tips #1: "Q" it\'s \\ ok'}, 'Tips #1: "Q" it\'s \\ ok'),
        ("title", {"title": ""}, ""),
        ("summary", {"summary": "New summary: with colon"}, "New summary: with colon"),
        ("summary", {"summary": "Line one\nLine two"}, "Line one\nLine two"),
        ("image", {"image": "https://example.com/a b.png"}, "https://example.com/a b.png"),
        ("slug", {"identifier": "new-slug"}, "new-slug"),
        ("date", {"published_at": WHEN}, WHEN),
    ]


# libyaml, through PyYAML: the reader Jekyll's and Hugo's are like (a tab
# after text is a space), and not the one front_matter.parsed uses.
LIBYAML = getattr(yaml, "CSafeLoader", None)


def parser_read(text):
    """The front matter of ``text`` as libyaml or tomllib reads it; None
    when they read none."""
    front, _body = front_matter.split(text)
    if front is None:
        return {}
    written = "".join(line + "\n" for entry in front.entries for line in entry.lines)
    try:
        read = tomllib.loads(written) if front.syntax == front_matter.TOML \
            else yaml.load(written, Loader=LIBYAML) or {}
    except (yaml.YAMLError, ValueError):
        return None
    return read if isinstance(read, dict) else None


def under(detail, read):
    """``(name, value)`` of each place the parser reads ``detail`` in."""
    found = [(key, value) for key, value in read.items()
             if isinstance(key, str) and key.lower() in NAMES[detail]]
    for table in (value for key, value in read.items()
                  if detail == "tags" and key == "taxonomies" and isinstance(value, dict)):
        found += [(("taxonomies", key), value) for key, value in table.items()
                  if key.lower() in NAMES[detail]]
    return found


def holds(detail, value, meant):
    """Whether ``value``, read by a parser, is the detail as ``meant``."""
    if detail == "tags":
        items = value if isinstance(value, list) else \
            value.split(",") if "," in value else value.split()
        tags = [str(item).strip().lstrip("#").strip() for item in items]
        return [tag.lower() for tag in tags if tag] == [tag.lower() for tag in meant]
    if detail == "date":
        moment = datetime.fromtimestamp(meant, tz=timezone.utc)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) == moment if value.tzinfo \
                else value == moment.replace(tzinfo=None)
        if isinstance(value, date):
            return value == moment.date()
        written = str(value).strip()
        if written.isdigit():
            return int(written) == meant
        # A date YAML reads as a text ("2026-10-13 02:25:18 -0500", Jekyll's).
        read = datetime.fromisoformat(re.sub(r"\s+([+-]\d)", r"\1", written))
        return read.astimezone(timezone.utc) == moment if read.tzinfo \
            else read == moment.replace(tzinfo=None)
    return isinstance(value, str) and value.strip() == meant


@pytest.mark.parametrize("name, text", SAMPLES, ids=[name for name, _text in SAMPLES])
def test_a_change_is_written_as_a_parser_reads_it_or_not_at_all(name, text):
    if LIBYAML is None:
        pytest.skip("PyYAML without libyaml")
    front, body = front_matter.split(text)
    details = from_front_matter(front)
    kept = kept_in_file(front)
    before = parser_read(text)
    if before is None:
        assert kept == frozenset(NAMES) | {"coauthors", "zap_split"}
    for detail, change, meant in changes(details):
        out = front_matter.join(front_matter_with(details.with_changes(**change), front), body)
        if detail in kept:
            assert out == text, (detail, change)
            continue
        after = parser_read(out)
        assert after is not None, (detail, change, out)
        others = [key for key in before.keys() | after.keys()
                  if before.get(key, MISSING) != after.get(key, MISSING)
                  and not (isinstance(key, str) and key.lower() in NAMES[detail])
                  and not (detail == "tags" and key == "taxonomies")]
        assert others == [], (detail, change, out)
        found = under(detail, after)
        if not meant:
            # Gone, or left as written when it held nothing ("tags: []").
            assert all(value in (None, "", []) for _name, value in found), (detail, change, out)
        else:
            assert len(found) == 1 and holds(detail, found[0][1], meant), (detail, change, out)
            if detail == "tags" and found[0][0] != ("taxonomies", "tags"):
                outside = {key: value for key, value in after.get("taxonomies", {}).items()} \
                    if isinstance(after.get("taxonomies"), dict) else {}
                assert outside == ({key: value for key, value in before["taxonomies"].items()}
                                   if isinstance(before.get("taxonomies"), dict) else {})


# -- a write a parser reads otherwise is never made -----------------------------

TEXT = "---\ntitle: T\ntags: [a, b]\nlayout: post\n---\nB\n"


def broken_on(monkeypatch, word):
    """front_matter's writers, both the one in the file's style and the
    one in double quotes, made to write a tags entry YAML reads otherwise
    (an unclosed quote) whenever the tags hold ``word``."""
    def broken(written, value):
        if written.key == "tags" and any(word in item for item in value):
            return front_matter.Entry(written.key, (written.lines[0].replace("[", '["', 1),),
                                      written.syntax)
        return written
    rewritten, quoted = front_matter.rewritten, front_matter.quoted
    # What is kept is worked out once per front matter: afresh with these.
    monkeypatch.setattr(article_details, "_kept_in_file", functools.lru_cache(maxsize=32)(
        article_details._kept_in_file.__wrapped__))
    monkeypatch.setattr(front_matter, "rewritten", lambda old, value, **kwargs: broken(
        rewritten(old, value, **kwargs), value))
    monkeypatch.setattr(front_matter, "quoted", lambda old, key, value, *args: broken(
        quoted(old, key, value, *args), value))


def test_a_detail_whose_change_would_be_read_otherwise_is_kept(monkeypatch):
    """A change to it is refused before one is made: the field is read-only."""
    broken_on(monkeypatch, "")
    front, body = front_matter.split(TEXT)
    assert kept_in_file(front) == frozenset({"tags"})
    details = from_front_matter(front)
    assert details.hashtags == ("a", "b")
    assert front_matter.join(front_matter_with(
        details.with_changes(hashtags=("a", "b", "c")), front), body) == TEXT


def test_a_change_no_way_of_writing_holds_is_not_written_and_said(monkeypatch):
    """One value written wrong in every way: the file keeps the detail as
    written, the write names it, and the field stays editable (round 8,
    M1); the other changes are written."""
    broken_on(monkeypatch, "Bad")
    front, body = front_matter.split(TEXT)
    details = from_front_matter(front).with_changes(hashtags=("a", "Bad"), title="New")
    written = front_matter_with(details, front)
    assert front_matter.join(written, body) == TEXT.replace("title: T", "title: New")
    assert written.refused == frozenset({"tags"}) and kept_in_file(written) == frozenset()


@pytest.mark.parametrize("text, change", [
    ("---\ndescription: >-\n  Old one.\nlayout: post\n---\nB\n",
     {"summary": "First line \nsecond line."}),
    ("---\ndescription: >-\n  Old one.\nlayout: post\n---\nB\n",
     {"summary": "First\n  indented second"}),
    ("---\ntitle: Old\nlayout: post\n---\nB\n", {"title": "12:30"}),
    ("---\ntitle: Old\nlayout: post\n---\nB\n", {"title": "<<"}),
    ("---\ntitle: Old\nlayout: post\n---\nB\n", {"title": "="}),
    ("---\ntitle: Old\nlayout: post\n---\nB\n", {"title": "a\tpasted\ttitle"}),
    ("---\nlayout: post\n---\nB\n", {"title": "12:30"}),
    ("---\nlayout: post\n---\nB\n", {"title": "odd\u2028inside\x85x"}),
    ('+++\ntitle = "Old"\ndraft = true\n+++\nB\n', {"title": "a\tb\u2028c"}),
])
def test_a_change_the_files_style_cannot_hold_is_written_in_double_quotes(text, change):
    """Round 8, M1: these were refused at saving, the text typed thrown
    away and the field locked. Written as a double-quoted text, with its
    escapes, they read back as typed."""
    front, body = front_matter.split(text)
    written = front_matter_with(from_front_matter(front).with_changes(**change), front)
    assert written.refused == frozenset()
    out = front_matter.join(written, body)
    after = parser_read(out)
    field, value = next(iter(change.items()))
    assert after[{"title": "title", "summary": "description"}[field]
                 if field == "summary" and "description" in text else field] == value
    assert after["layout" if "layout" in text else "draft"] == parser_read(text)[
        "layout" if "layout" in text else "draft"]
    assert getattr(from_front_matter(front_matter.split(out)[0]), field) == value.strip()


def test_taking_out_the_last_detail_beside_comments_empties_it_instead():
    """Taken out, only a comment would be left, which reads as text: the
    detail is written empty in its place."""
    text = "---\n# my notes\ntags: [idea]\n---\n\nBody.\n"
    front, body = front_matter.split(text)
    written = front_matter_with(from_front_matter(front).with_changes(hashtags=()), front)
    assert written.refused == frozenset()
    assert front_matter.join(written, body) == "---\n# my notes\ntags: []\n---\n\nBody.\n"
REFUSED_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import alerts
import front_matter
import main_window

def broken(written, value):                  # every way of writing it wrong, for one value
    if written.key == "tags" and "Bad" in value:
        return front_matter.Entry(written.key, (written.lines[0].replace("[", '["', 1),),
                                  written.syntax)
    return written
rewritten, quoted = front_matter.rewritten, front_matter.quoted
front_matter.rewritten = lambda old, value, **kwargs: broken(rewritten(old, value, **kwargs), value)
front_matter.quoted = lambda old, key, value, *args: broken(quoted(old, key, value, *args), value)

answer, how = sys.argv[3], sys.argv[4]
asked = []
class Alert:                                 # answers Save, then ``answer``
    def __init__(self, parent, *, title, message="", buttons=(), **kwargs):
        values = [button.value for button in buttons]
        if "save" not in values:             # not the Save, Don't Save, Cancel one
            asked.append({"title": title, "message": message,
                          "buttons": [button.label for button in buttons]})
        self.value = "save" if "save" in values else answer
    def run(self):
        return self.value
alerts.Alert = Alert

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
w._set_article_details(ed, w._article_details_of(ed).with_changes(
    hashtags=("a", "Bad"), title="New"))
r = {}
if how == "save":
    r["saved"] = w._save_to(path)
else:
    w.close_tab(w.tabs.indexOf(w.tabs.currentWidget()))
    r["open"] = w.current_editor() is ed
app.processEvents()
r["asked"] = asked
r["file"] = open(path, encoding="utf-8").read()
if w.current_editor() is ed:
    r["said"] = w.status.currentMessage()
    r["tags read-only"] = panel.hashtags.isReadOnly()
    r["hashtags"] = list(w._article_details_of(ed).hashtags)
    r["modified"] = ed.document().isModified()
    ed.document().setModified(False)
w.close()
print("RESULT " + json.dumps(r))
"""

UNSAVED_TITLE = "“Hashtags” can’t be saved in the file"


def refused_in_window(tmp_path, answer, how):
    from tests.app_process import run_window_script
    path = tmp_path / "post.md"
    path.write_text(TEXT, encoding="utf-8")
    return run_window_script(REFUSED_SCRIPT, tmp_path, str(path), answer, how)


def test_saving_what_cant_be_written_asks_and_keeps_editing(tmp_path):
    """Round 8, M1: the text typed was thrown away, the field locked, and
    only the status bar said so. Keep Editing: nothing is written."""
    r = refused_in_window(tmp_path, "keep", "save")
    assert [a["title"] for a in r["asked"]] == [UNSAVED_TITLE]
    assert r["asked"][0]["buttons"] == ["Save Without It", "Keep Editing"]
    assert "post.md" in r["asked"][0]["message"]
    assert r["saved"] is False and r["file"] == TEXT and r["modified"] is True
    assert r["hashtags"] == ["a", "Bad"] and r["tags read-only"] is False


def test_saving_without_what_cant_be_written_keeps_it_in_the_tab(tmp_path):
    r = refused_in_window(tmp_path, "without", "save")
    assert r["saved"] is True and r["file"] == TEXT.replace("title: T", "title: New")
    assert r["hashtags"] == ["a", "Bad"] and r["tags read-only"] is False
    assert r["modified"] is False
    assert "Hashtags" in r["said"]


def test_closing_with_save_asks_before_the_document_closes(tmp_path):
    r = refused_in_window(tmp_path, "keep", "close")
    assert [a["title"] for a in r["asked"]] == [UNSAVED_TITLE]
    assert r["open"] is True and r["file"] == TEXT
