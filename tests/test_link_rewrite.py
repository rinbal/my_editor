# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how notes' links follow a file that is renamed or moved.

What must hold: every relative link to what moved points at its new
place, written the way it was (encoded, in brackets, plain); a note
that moved has its own links worked out again; code, web addresses and
broken links are never touched; and the rest of the note stays byte
for byte, its encoding, byte order mark and line endings included.
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import link_rewrite  # noqa: E402
from link_rewrite import find_references, rewrite_after_moves  # noqa: E402


def write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def read(path):
    return path.read_bytes().decode("utf-8")


def move(old, new):
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    return {str(old): str(new)}


def test_a_renamed_picture_keeps_its_links(tmp_path):
    write(tmp_path / "pics" / "a.png", "")
    note = write(tmp_path / "Note.md", "See ![beach](pics/a.png \"Beach\") here.\n")
    moves = move(tmp_path / "pics" / "a.png", tmp_path / "pics" / "b.png")
    assert rewrite_after_moves(str(tmp_path), moves) == [str(note)]
    assert read(note) == "See ![beach](pics/b.png \"Beach\") here.\n"


def test_a_name_with_a_space_is_put_in_brackets(tmp_path):
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Note.md", "![](a.png)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "my beach.png"))
    assert read(note) == "![](<my beach.png>)\n"


def test_a_name_with_parentheses_is_put_in_brackets(tmp_path):
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Note.md", "[pic](a.png)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "Beach (2).png"))
    assert read(note) == "[pic](<Beach (2).png>)\n"


def test_a_bracketed_link_stays_in_brackets(tmp_path):
    write(tmp_path / "my pic.png", "")
    note = write(tmp_path / "Note.md", "![x](<my pic.png>)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "my pic.png", tmp_path / "plain.png"))
    assert read(note) == "![x](<plain.png>)\n"


def test_a_percent_encoded_link_stays_encoded(tmp_path):
    write(tmp_path / "a b.png", "")
    note = write(tmp_path / "Note.md", "![x](a%20b.png)\n")
    moves = move(tmp_path / "a b.png", tmp_path / "Fotos" / "Strand am Meer (1).png")
    rewrite_after_moves(str(tmp_path), moves)
    assert read(note) == "![x](Fotos/Strand%20am%20Meer%20%281%29.png)\n"


def test_umlauts_are_found_and_written_as_they_are(tmp_path):
    write(tmp_path / "Bücher" / "Übersicht.png", "")
    note = write(tmp_path / "Notiz.md", "![](Bücher/Übersicht.png) and ![](B%C3%BCcher/%C3%9Cbersicht.png)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "Bücher", tmp_path / "Bände"))
    assert read(note) == "![](Bände/Übersicht.png) and ![](B%C3%A4nde/%C3%9Cbersicht.png)\n"


def test_fragment_query_and_title_are_kept(tmp_path):
    write(tmp_path / "Plan.md", "")
    note = write(tmp_path / "Index.md",
                 "[a](Plan.md#goals) [b](Plan.md?raw=1 'The plan') [c](<Plan.md#two words>)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "Plan.md", tmp_path / "Work" / "Plan.md"))
    assert read(note) == ("[a](Work/Plan.md#goals) [b](Work/Plan.md?raw=1 'The plan') "
                          "[c](<Work/Plan.md#two words>)\n")


def test_reference_definitions_follow(tmp_path):
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Note.md", "![x][pic]\n\n[pic]: a.png \"Title\"\n[other]: <a.png>\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "img" / "a b.png"))
    assert read(note) == "![x][pic]\n\n[pic]: <img/a b.png> \"Title\"\n[other]: <img/a b.png>\n"


def test_html_pictures_and_anchors_follow(tmp_path):
    write(tmp_path / "a.png", "")
    write(tmp_path / "Doc.pdf", "")
    note = write(tmp_path / "Note.md",
                 '<img src="a.png" width="200"> <a href=\'Doc.pdf\'>doc</a> <img src=a.png>\n')
    moves = move(tmp_path / "a.png", tmp_path / "pics" / "a b.png")
    moves.update(move(tmp_path / "Doc.pdf", tmp_path / "files" / "Doc.pdf"))
    rewrite_after_moves(str(tmp_path), moves)
    assert read(note) == ('<img src="pics/a b.png" width="200"> <a href=\'files/Doc.pdf\'>doc</a> '
                          '<img src="pics/a b.png">\n')


def test_links_in_code_are_left_alone(tmp_path):
    write(tmp_path / "a.png", "")
    text = ("Real ![](a.png) and `![](a.png)` and ``code ` ![](a.png)``\n\n"
            "```markdown\n![](a.png)\n```\n\n"
            "~~~~\n![](a.png)\n~~~~\n\n"
            "```{r}\nknitr::include_graphics(\"a.png\")  # ![](a.png)\n```\n")
    note = write(tmp_path / "Note.Rmd", text)
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert read(note) == text.replace("Real ![](a.png)", "Real ![](b.png)")


def test_an_unclosed_fence_keeps_the_rest_of_the_note_as_code(tmp_path):
    write(tmp_path / "a.png", "")
    text = "![](a.png)\n\n```\n![](a.png)\n"
    note = write(tmp_path / "Note.md", text)
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert read(note) == "![](b.png)\n\n```\n![](a.png)\n"


def test_web_mail_and_absolute_links_are_never_touched(tmp_path):
    write(tmp_path / "a.png", "")
    text = ("[w](https://example.com/a.png) [m](mailto:me@example.com) [f](#a.png) "
            f"[abs]({tmp_path}/a.png) <https://example.com/a.png>\n")
    note = write(tmp_path / "Note.md", text)
    assert rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png")) == []
    assert read(note) == text


def test_a_note_in_a_subfolder_with_parent_links(tmp_path):
    write(tmp_path / "pics" / "a.png", "")
    note = write(tmp_path / "Trips" / "2026" / "Rome.md", "![](../../pics/a.png)\n")
    moves = move(tmp_path / "pics", tmp_path / "Media" / "Pictures")
    rewrite_after_moves(str(tmp_path), moves)
    assert read(note) == "![](../../Media/Pictures/a.png)\n"


def test_a_moved_note_works_out_its_own_links_again(tmp_path):
    write(tmp_path / "pics" / "a.png", "")
    write(tmp_path / "Other.md", "")
    text = "![](pics/a.png) [o](Other.md) [web](https://x.org) [gone](missing.png)\n"
    write(tmp_path / "Note.md", text)
    moves = move(tmp_path / "Note.md", tmp_path / "Archive" / "Old" / "Note.md")
    changed = rewrite_after_moves(str(tmp_path), moves)
    note = tmp_path / "Archive" / "Old" / "Note.md"
    assert changed == [str(note)]
    assert read(note) == ("![](../../pics/a.png) [o](../../Other.md) [web](https://x.org) "
                          "[gone](missing.png)\n")


def test_a_note_moved_with_its_pictures_keeps_its_links(tmp_path):
    write(tmp_path / "Trip_media" / "beach.png", "")
    note = write(tmp_path / "Trip.md", "![](Trip_media/beach.png)\n")
    moves = move(tmp_path / "Trip.md", tmp_path / "Done" / "Trip.md")
    moves.update(move(tmp_path / "Trip_media", tmp_path / "Done" / "Trip_media"))
    assert rewrite_after_moves(str(tmp_path), moves) == []
    assert read(tmp_path / "Done" / "Trip.md") == "![](Trip_media/beach.png)\n"
    assert not note.exists()


def test_moving_a_folder_that_contains_linked_pictures(tmp_path):
    write(tmp_path / "Album" / "Summer" / "sea.jpg", "")
    outside = write(tmp_path / "Index.md", "![](Album/Summer/sea.jpg) ![](./Album/Summer/sea.jpg)\n")
    inside = write(tmp_path / "Album" / "About.md", "![](Summer/sea.jpg) [i](../Index.md)\n")
    moves = move(tmp_path / "Album", tmp_path / "Archive" / "Album 2026")
    changed = rewrite_after_moves(str(tmp_path), moves)
    moved_inside = tmp_path / "Archive" / "Album 2026" / "About.md"
    assert sorted(changed) == sorted([str(outside), str(moved_inside)])
    assert read(outside) == ("![](<Archive/Album 2026/Summer/sea.jpg>) "
                             "![](<./Archive/Album 2026/Summer/sea.jpg>)\n")
    assert read(moved_inside) == "![](Summer/sea.jpg) [i](../../Index.md)\n"
    assert not inside.exists()


def test_encoding_bom_and_line_endings_are_kept(tmp_path):
    write(tmp_path / "a.png", "")
    crlf = tmp_path / "Windows.md"
    crlf.write_bytes(b"\xef\xbb\xbfTitle\r\n\r\n![](a.png)\r\nEnd\r\n")
    utf16 = tmp_path / "Wide.md"
    utf16.write_bytes(b"\xff\xfe" + "![](a.png)\n".encode("utf-16-le"))
    mixed = tmp_path / "Mixed.md"
    mixed.write_bytes(b"one\r\n![](a.png)\ntwo\rthree\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert crlf.read_bytes() == b"\xef\xbb\xbfTitle\r\n\r\n![](b.png)\r\nEnd\r\n"
    assert utf16.read_bytes() == b"\xff\xfe" + "![](b.png)\n".encode("utf-16-le")
    assert mixed.read_bytes() == b"one\r\n![](b.png)\ntwo\rthree\n"


def test_notes_that_are_not_text_or_are_locked_are_left_alone(tmp_path):
    import locked_files
    write(tmp_path / "a.png", "")
    binary = tmp_path / "Binary.md"
    binary.write_bytes(b"\x00\xff\xfe![](a.png)")
    locked = tmp_path / "Locked.md"
    locked.write_bytes(locked_files.MAGIC + b"![](a.png)")
    before = binary.read_bytes(), locked.read_bytes()
    assert rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png")) == []
    assert (binary.read_bytes(), locked.read_bytes()) == before


def test_a_note_in_latin_1_is_updated_byte_for_byte(tmp_path):
    write(tmp_path / "a.png", "")
    latin = tmp_path / "Latin.md"
    latin.write_bytes("Café ![](a.png)\n".encode("latin-1"))
    result = link_rewrite.rewrite_links(str(tmp_path),
                                        move(tmp_path / "a.png", tmp_path / "pics" / "b.png"))
    assert result.changed == [str(latin)]
    assert latin.read_bytes() == "Café ![](pics/b.png)\n".encode("latin-1")


def test_a_note_in_latin_1_is_named_when_a_new_name_needs_more_than_ascii(tmp_path):
    write(tmp_path / "a.png", "")
    latin = tmp_path / "Latin.md"
    latin.write_bytes("Café ![](a.png)\n".encode("latin-1"))
    before = latin.read_bytes()
    result = link_rewrite.rewrite_links(str(tmp_path),
                                        move(tmp_path / "a.png", tmp_path / "Bände.png"))
    assert result.changed == []
    assert result.not_updated == [str(latin)]
    assert latin.read_bytes() == before


def test_a_link_in_a_latin_1_note_to_a_name_with_accents_is_found(tmp_path):
    write(tmp_path / "café.png", "")
    latin = tmp_path / "Latin.md"
    latin.write_bytes("![](café.png)\n".encode("latin-1"))
    result = link_rewrite.rewrite_links(str(tmp_path),
                                        move(tmp_path / "café.png", tmp_path / "cafe.png"))
    assert result.changed == [str(latin)]
    assert latin.read_bytes() == b"![](cafe.png)\n"


def test_skipped_notes_are_not_written(tmp_path):
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Open.md", "![](a.png)\n")
    other = write(tmp_path / "Other.md", "![](a.png)\n")
    changed = rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"),
                                  skip_notes={str(note)})
    assert changed == [str(other)]
    assert read(note) == "![](a.png)\n"


def test_a_plain_link_with_spaces_is_followed_as_written(tmp_path):
    write(tmp_path / "my pic.png", "")
    note = write(tmp_path / "Note.md", "![](my pic.png)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "my pic.png", tmp_path / "our pic.png"))
    assert read(note) == "![](our pic.png)\n"


def test_escaped_parentheses_stay_escaped(tmp_path):
    write(tmp_path / "a(1).png", "")
    note = write(tmp_path / "Note.md", "![](a\\(1\\).png)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a(1).png", tmp_path / "b(2).png"))
    assert read(note) == "![](b\\(2\\).png)\n"


def test_hidden_folders_are_not_searched(tmp_path):
    write(tmp_path / "a.png", "")
    hidden = write(tmp_path / ".git" / "Note.md", "![](../a.png)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert read(hidden) == "![](../a.png)\n"


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="case-insensitive disks")
def test_a_change_of_case_only_is_followed(tmp_path):
    write(tmp_path / "photo.png", "")
    note = write(tmp_path / "Note.md", "![](photo.png)\n")
    os.rename(tmp_path / "photo.png", tmp_path / "Photo.png")
    rewrite_after_moves(str(tmp_path), {str(tmp_path / "photo.png"): str(tmp_path / "Photo.png")})
    assert read(note) == "![](Photo.png)\n"


def test_references_are_found_for_files_and_folders(tmp_path):
    write(tmp_path / "pics" / "a b.png", "")
    one = write(tmp_path / "One.md", "![](pics/a%20b.png)\n")
    two = write(tmp_path / "Sub" / "Two.md", "<img src=\"../pics/a b.png\">\n")
    write(tmp_path / "Three.md", "`![](pics/a b.png)` [web](https://x.org/pics)\n")
    found = find_references(str(tmp_path), [str(tmp_path / "pics" / "a b.png"),
                                            str(tmp_path / "pics"), str(tmp_path / "Nothing")])
    assert sorted(found[str(tmp_path / "pics" / "a b.png")]) == sorted([str(one), str(two)])
    assert sorted(found[str(tmp_path / "pics")]) == sorted([str(one), str(two)])
    assert found[str(tmp_path / "Nothing")] == []


def test_a_note_moved_out_of_the_library_still_has_its_links_updated(tmp_path):
    library = tmp_path / "Library"
    write(library / "a.png", "")
    write(library / "Note.md", "![](a.png)\n")
    moves = move(library / "Note.md", tmp_path / "Elsewhere" / "Note.md")
    rewrite_after_moves(str(library), moves)
    assert read(tmp_path / "Elsewhere" / "Note.md") == "![](../Library/a.png)\n"


def test_is_within_compares_whole_names():
    assert link_rewrite.is_within("/a/b/c", "/a/b")
    assert link_rewrite.is_within("/a/b", "/a/b")
    assert not link_rewrite.is_within("/a/bc", "/a/b")


# -- notes that are not files of their own -------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need rights on Windows")
def test_a_linked_note_is_never_written_through(tmp_path):
    library, outside = tmp_path / "Library", tmp_path / "Outside"
    journal = write(outside / "journal.md", "My picture: ![](pic.png)\n")
    write(outside / "pic.png", "outside")
    write(library / "pic.png", "library")
    os.symlink(journal, library / "journal-link.md")
    changed = rewrite_after_moves(str(library),
                                  move(library / "pic.png", library / "renamed.png"))
    assert changed == []
    assert read(journal) == "My picture: ![](pic.png)\n"
    assert os.path.islink(library / "journal-link.md")
    assert list(link_rewrite.notes_under(str(library))) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need rights on Windows")
def test_a_moved_link_to_a_note_is_not_written_through(tmp_path):
    library, outside = tmp_path / "Library", tmp_path / "Outside"
    journal = write(outside / "journal.md", "![](pic.png)\n")
    write(library / "pic.png", "")
    os.symlink(journal, library / "link.md")
    os.symlink(outside, library / "linked folder")
    moves = move(library / "link.md", library / "Sub" / "link.md")
    moves.update(move(library / "linked folder", library / "Sub" / "linked folder"))
    assert rewrite_after_moves(str(library), moves) == []
    assert read(journal) == "![](pic.png)\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes")
def test_a_pipe_named_like_a_note_is_never_read(tmp_path):
    write(tmp_path / "a.png", "")
    os.mkfifo(tmp_path / "pipe.md")
    assert list(link_rewrite.notes_under(str(tmp_path))) == []
    assert link_rewrite.rewrite_note(str(tmp_path / "pipe.md"), {}) is False
    assert link_rewrite.link_targets(str(tmp_path / "pipe.md")) == []


def test_a_note_that_changed_meanwhile_is_not_written_over(tmp_path, monkeypatch):
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Note.md", "![](a.png)\n")
    real_read = link_rewrite._read_note

    def read_then_the_editor_saves(path):
        found = real_read(path)
        # The editor saves the note as it was written, with its own line
        # endings (bytes: text mode would write Windows' on Windows).
        write(note, "![](a.png) and a new sentence\n")
        return found

    monkeypatch.setattr(link_rewrite, "_read_note", read_then_the_editor_saves)
    result = link_rewrite.rewrite_links(str(tmp_path),
                                        move(tmp_path / "a.png", tmp_path / "b.png"))
    assert result.changed == []
    assert result.not_updated == [str(note)]
    assert read(note) == "![](a.png) and a new sentence\n"


# -- what is not a link, read as CommonMark reads it ----------------------------


def test_comments_front_matter_and_indented_code_are_left_alone(tmp_path):
    write(tmp_path / "a.png", "")
    text = ("---\ncover: a.png\nlink: '[a](a.png)'\n---\n"
            "<!-- [old](a.png) -->\n\n"
            "<!--\n![](a.png)\n\n![](a.png)\n-->\n\n"
            "Text <!-- ![](a.png) --> and ![](a.png)\n\n"
            "    ![](a.png) indented code\n\n"
            "\\[not a link](a.png)\n")
    note = write(tmp_path / "Note.md", text)
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert read(note) == text.replace("--> and ![](a.png)", "--> and ![](b.png)")


def test_a_fence_may_be_indented_three_spaces_and_four_is_code(tmp_path):
    write(tmp_path / "a.png", "")
    text = ("   ```\n![](a.png)\n   ```\n\n"
            "    ```\n![](a.png)\n\n"
            "- item\n\n  ```\n  ![](a.png)\n  ```\n\n"
            "- item\n\n      ![](a.png)\n\n"
            "> ```\n> ![](a.png)\n> ```\n")
    note = write(tmp_path / "Note.md", text)
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    # Only the line after a fence indented four spaces (an indented code
    # block of one line) is a paragraph with a link in it.
    assert read(note) == text.replace("    ```\n![](a.png)", "    ```\n![](b.png)")


def test_html_blocks_keep_their_tags_and_hold_no_markdown_links(tmp_path):
    write(tmp_path / "a.png", "")
    text = ('<div>\n<img src="a.png">\n[text](a.png)\n</div>\n\n'
            '<pre>\n<img src="a.png">\n</pre>\n\n'
            '<img alt=" src=a.png" src="a.png">\n')
    note = write(tmp_path / "Note.md", text)
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert read(note) == ('<div>\n<img src="b.png">\n[text](a.png)\n</div>\n\n'
                          '<pre>\n<img src="a.png">\n</pre>\n\n'
                          '<img alt=" src=a.png" src="b.png">\n')


def test_math_in_r_markdown_is_left_alone_and_dollars_elsewhere_are_text(tmp_path):
    write(tmp_path / "a.png", "")
    text = "$[a](a.png)$ costs $5 and [b](a.png) $10\n\n$$\n[c](a.png)\n$$\n"
    rmd = write(tmp_path / "Note.Rmd", text)
    md = write(tmp_path / "Note.md", text)
    rewrite_after_moves(str(tmp_path), move(tmp_path / "a.png", tmp_path / "b.png"))
    assert read(rmd) == "$[a](a.png)$ costs $5 and [b](b.png) $10\n\n$$\n[c](a.png)\n$$\n"
    assert read(md) == text.replace("a.png", "b.png")


def test_a_picture_may_hold_a_link_and_a_link_may_not(tmp_path):
    write(tmp_path / "a.png", "")
    write(tmp_path / "c.md", "")
    text = "![x [y](c.md)](a.png) [p [q](c.md)](a.png)\n"
    note = write(tmp_path / "Note.md", text)
    moves = move(tmp_path / "a.png", tmp_path / "b.png")
    moves.update(move(tmp_path / "c.md", tmp_path / "d.md"))
    rewrite_after_moves(str(tmp_path), moves)
    assert read(note) == "![x [y](d.md)](b.png) [p [q](d.md)](a.png)\n"


def test_a_note_of_any_shape_is_read_in_time_proportional_to_its_length():
    import time
    size = 200_000
    shapes = {
        "backtick runs": "x " + " ".join("`" * k for k in range(1, 640)),
        "unclosed comments": "<!-- " * (size // 5),
        "unclosed titles": '[a](b "' * (size // 7),
        "open brackets": "[" * size,
        "unclosed destinations": "[a](x" * (size // 5),
        "loose destinations": "[a](b c " * (size // 8) + ")",
        "unclosed tags": "<img a " * (size // 7),
        "unclosed angles": "[a](<b " * (size // 7),
        "fences": "```\n" * (size // 4),
        "nested lists": "".join(" " * (i % 40) + "- x\n" for i in range(size // 25)),
        "dollars": "$a " * (size // 3),
        "parentheses": "[a](" + "(" * size,
    }
    for name, text in shapes.items():
        for math in (False, True):
            started = time.perf_counter()
            link_rewrite._links(text, math)
            # Linear time takes a few ms; quadratic time takes many seconds
            # for 200,000 characters. 1 s leaves room for a busy CI runner.
            assert time.perf_counter() - started < 1.0, name


# -- names that would change what a link means ---------------------------------


@pytest.mark.parametrize("name", [
    "C# tips.md",
    # Windows holds no "?" in a name: the library refuses it there and says
    # why (test_folder_library), so no link ever points at one.
    pytest.param("Why?.md", marks=pytest.mark.skipif(sys.platform == "win32",
                                                     reason="Windows names hold no question mark")),
    "100% sure.md", "a%41.md"])
def test_a_name_that_reads_as_part_of_an_address_is_encoded_and_followed_again(tmp_path,
                                                                                name):
    write(tmp_path / "tips.md", "")
    index = write(tmp_path / "index.md", "[tips](tips.md) [same](<tips.md>)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "tips.md", tmp_path / name))
    assert os.path.basename(link_rewrite.link_targets(str(index))[0]) == name
    rewrite_after_moves(str(tmp_path), move(tmp_path / name, tmp_path / ("x" + name)))
    assert os.path.basename(link_rewrite.link_targets(str(index))[0]) == "x" + name
    rewrite_after_moves(str(tmp_path), move(tmp_path / ("x" + name), tmp_path / "plain.md"))
    # (A link that needed brackets for a space keeps them, as any does.)
    assert read(index) in ("[tips](plain.md) [same](<plain.md>)\n",
                           "[tips](<plain.md>) [same](<plain.md>)\n")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows names hold no colon")
def test_a_name_with_a_colon_is_a_file_when_it_is_there(tmp_path):
    write(tmp_path / "Todo:old.md", "")
    index = write(tmp_path / "index.md",
                  "[t](Todo:old.md) [w](https://x.org) [n](Note:none.md)\n")
    rewrite_after_moves(str(tmp_path), move(tmp_path / "Todo:old.md", tmp_path / "Done:old.md"))
    assert read(index) == "[t](Done%3Aold.md) [w](https://x.org) [n](Note:none.md)\n"
    rewrite_after_moves(str(tmp_path), move(tmp_path / "Done:old.md", tmp_path / "done.md"))
    assert read(index) == "[t](done.md) [w](https://x.org) [n](Note:none.md)\n"


# -- one writer at a time ----------------------------------------------------------


def test_a_save_landing_while_a_note_is_updated_waits_its_turn_and_is_kept(tmp_path,
                                                                          monkeypatch):
    import threading
    import atomic_file
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Note.md", "![](a.png)\n\nold text\n")
    moves = move(tmp_path / "a.png", tmp_path / "b.png")
    real_look = link_rewrite._regular_file_bytes
    looks, savers = [], []

    def look(path, limit):
        found = real_look(path, limit)
        looks.append(path)
        if path == str(note) and len(looks) == 2:
            # The editor saves now: after the update looked at the note,
            # before it replaced it.
            saver = threading.Thread(target=atomic_file.save_document, args=(
                str(note), b"![](a.png)\n\nsaved by the person\n"))
            savers.append(saver)
            saver.start()
            saver.join(0.3)
        return found

    monkeypatch.setattr(link_rewrite, "_regular_file_bytes", look)
    rewrite_after_moves(str(tmp_path), moves)
    savers[0].join()
    assert read(note) == "![](a.png)\n\nsaved by the person\n"


def test_an_open_note_with_unsaved_changes_is_asked_for_when_it_is_written(tmp_path):
    write(tmp_path / "a.png", "")
    note = write(tmp_path / "Note.md", "![](a.png)\n")
    other = write(tmp_path / "Other.md", "![](a.png)\n")
    asked = []

    def unsaved():
        asked.append(True)
        return {str(note)}

    moves = move(tmp_path / "a.png", tmp_path / "b.png")
    result = link_rewrite.rewrite_links(str(tmp_path), moves, unsaved)
    assert asked
    assert result.changed == [str(other)]
    assert [(n.note, n.was, n.moves) for n in result.left_open] == [(str(note), str(note), moves)]
    assert read(note) == "![](a.png)\n"
