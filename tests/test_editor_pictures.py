# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pictures in the text are shown no wider than the text, as Pages and
Notes show them, without changing the picture, the document or its undo
history."""

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QUrl
from PySide6.QtGui import QColor, QImage, QTextDocument, QTextImageFormat
from PySide6.QtWidgets import QApplication

from editor import HtmlEditor


@pytest.fixture
def editor():
    QApplication.instance() or QApplication([])
    ed = HtmlEditor()
    ed.resize(700, 500)
    ed.show()
    QApplication.processEvents()
    yield ed
    ed.document().setModified(False)
    ed.close()
    ed.deleteLater()


def _png(width, height):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("teal"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return QByteArray(buf.data())


def _insert(ed, name, payload):
    ed.add_picture_resource(name, payload)
    fmt = QTextImageFormat()
    fmt.setName(name)
    cursor = ed.textCursor()
    cursor.insertImage(fmt)
    ed.setTextCursor(cursor)
    QApplication.processEvents()


def _shown_width(ed, name):
    found = ed.document().resource(QTextDocument.ImageResource, QUrl(name))
    return found.width() / found.devicePixelRatio(), found


def test_a_photo_wider_than_the_text_is_shown_at_the_texts_width(editor):
    _insert(editor, "asset:photo", _png(3000, 2000))
    shown, image = _shown_width(editor, "asset:photo")
    assert shown <= editor._room_for_pictures() + 0.5
    assert image.width() == 3000      # every pixel kept
    assert editor.document().size().width() <= editor.viewport().width() + 1


def test_a_small_picture_keeps_its_own_size(editor):
    _insert(editor, "asset:icon", _png(120, 80))
    shown, image = _shown_width(editor, "asset:icon")
    assert shown == 120
    assert image.devicePixelRatio() == 1.0


def test_fitting_neither_marks_the_document_edited_nor_adds_an_undo_step(editor):
    _insert(editor, "asset:photo", _png(2400, 1600))
    editor.document().setModified(False)
    steps = editor.document().availableUndoSteps()
    editor.resize(400, 500)
    QApplication.processEvents()
    QApplication.processEvents()
    assert not editor.document().isModified()
    assert editor.document().availableUndoSteps() == steps


def test_the_picture_follows_the_width_of_the_window(editor):
    _insert(editor, "asset:photo", _png(3000, 2000))
    editor.resize(420, 500)
    QApplication.processEvents()
    QApplication.processEvents()
    narrow, _ = _shown_width(editor, "asset:photo")
    editor.resize(1200, 500)
    QApplication.processEvents()
    QApplication.processEvents()
    wide, _ = _shown_width(editor, "asset:photo")
    assert narrow <= editor._room_for_pictures()
    assert narrow < wide <= 3000


def test_paper_mode_fits_the_picture_to_the_page(editor):
    editor.set_prose(True)
    _insert(editor, "asset:photo", _png(3000, 2000))
    editor.resize(1600, 600)
    QApplication.processEvents()
    editor.set_paper_mode(True)
    QApplication.processEvents()
    QApplication.processEvents()
    shown, _ = _shown_width(editor, "asset:photo")
    assert shown <= editor._room_for_pictures() + 0.5
    assert shown < 1400


def test_a_picture_read_beside_the_file_is_fitted_too(editor):
    big = QImage(2600, 1000, QImage.Format.Format_RGB32)
    big.fill(QColor("orange"))
    editor.set_local_image_resolver(lambda name: big)
    editor.setHtml('<p>x<img src="pictures/big.png"></p>')
    QApplication.processEvents()
    QApplication.processEvents()
    assert editor.document().size().width() <= editor.viewport().width() + 1


# -- photo groups in the editor ---------------------------------------------------------
# Pictures that follow each other show as one group (photo_group), every
# picture whole (the coloured corners of the fixtures), at every width,
# laid out again when the width or a picture's size changes, with nothing
# in the document or its undo history changed.

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

import photo_group  # noqa: E402
import rich_text  # noqa: E402
from markdown_writer import _image_as_read, document_to_markdown, read_markdown  # noqa: E402
from tests.photo_corners import SHAPES, image_of, shows_whole  # noqa: E402

BOTHY = ("# Bothy weekend\n\nWe walked in from the station.\n\n![Steg](portrait.png)\n\n"
         "*The jetty at dusk*\n\n![Loch](landscape.png)\n\n![Hut](square.png)\n\n"
         "![Panorama](panorama.png)\n\n![Plan](plan.png#wide)\n\n*Floor plan of the bothy*\n\n"
         "The next morning it rained.\n")


def _group_editor(width, markdown=BOTHY, *, missing=()):
    ed = HtmlEditor()
    asked = []

    def local(name):
        asked.append(name)
        return None if name in missing else image_of(name)
    ed.set_local_image_resolver(local)
    ed.resize(width, 2000)
    read_markdown(ed.document(), markdown)
    ed.document().clearUndoRedoStacks()
    ed.document().setModified(False)
    ed.show()
    QApplication.processEvents()
    return ed, asked


def _boxes(ed):
    return [(name, box) for _p, name, box in photo_group.picture_boxes(ed.document())]


@pytest.mark.parametrize("width", [1200, 760, 420])
def test_a_group_in_the_editor_shows_every_picture_whole(width):
    ed, _asked = _group_editor(width)
    shot = ed.viewport().grab().toImage()
    boxes = _boxes(ed)
    assert [name for name, _b in boxes] == ["portrait.png", "landscape.png", "square.png",
                                            "panorama.png", "plan.png#wide"]
    for name, box in boxes:
        assert shows_whole(shot, box), name
        w, h = SHAPES[photo_group.without_wide(name)]
        assert box.width() / box.height() == pytest.approx(w / h, rel=0.01)
        assert box.right() <= ed.viewport().width()
    ed.close()


def test_group_cells_are_never_taller_than_the_cap():
    ed, _asked = _group_editor(1600)
    for name, box in _boxes(ed):
        cap = 900 if photo_group.is_wide(name) else 560
        assert box.height() <= cap + 0.5
    ed.close()


def test_a_group_follows_the_width_with_nothing_to_undo():
    ed, _asked = _group_editor(1100)
    before = [box.width() for _n, box in _boxes(ed)]
    ed.resize(600, 2000)
    QApplication.processEvents()
    after = [box.width() for _n, box in _boxes(ed)]
    assert all(b < a for a, b in zip(before, after))
    assert not ed.document().isUndoAvailable() and not ed.document().isModified()
    ed.close()


def test_a_wide_picture_is_loaded_without_its_wide():
    _ed, asked = _group_editor(900)
    assert "plan.png" in asked and not any(name.endswith("#wide") for name in asked)
    _ed.close()


def test_a_picture_arriving_late_takes_its_own_shape_in_the_group():
    ed, _asked = _group_editor(900, missing={"portrait.png"})
    first = dict(_boxes(ed))["portrait.png"]
    assert first.width() / first.height() == pytest.approx(1.5, rel=0.02)   # 3:2 meanwhile
    ed.add_picture_resource("portrait.png", image_of("portrait.png"))
    QApplication.processEvents()
    now = dict(_boxes(ed))["portrait.png"]
    assert now.width() / now.height() == pytest.approx(809 / 1800, rel=0.01)
    ed.close()


def test_copying_a_group_copies_its_paragraphs_and_a_paste_shows_the_group_again():
    ed, _asked = _group_editor(900)
    ed.selectAll()
    mime = ed.createMimeDataFromSelection()
    import paste
    assert bytes(mime.data(paste.MARKDOWN_MIME)).decode("utf-8") == BOTHY
    assert "<table" not in mime.html() and "<table" not in bytes(
        mime.data(paste.LITERAL_HTML_MIME)).decode("utf-8")
    # The words in the order they read: each picture, then its caption.
    assert mime.text().split("\n")[2:5] == ["\ufffc", "The jetty at dusk", "\ufffc"]
    other, _ = _group_editor(900, "")
    other.insertFromMimeData(mime)
    assert photo_group.has_groups(other.document())
    assert document_to_markdown(other.document(), _image_as_read) == BOTHY
    ed.close()
    other.close()


def test_the_table_commands_leave_a_group_alone():
    ed, _asked = _group_editor(900)
    caption = ed.document().find("The jetty at dusk")
    assert caption.currentTable() is not None
    assert rich_text.table_at(caption) is None
    assert rich_text.tables_of(ed.document()) == []
    ed.close()


def _point_of(ed, name):
    box = dict(_boxes(ed))[name]
    return QPointF(box.center()).toPoint()


def test_a_double_click_on_a_group_picture_shows_it_in_quick_look():
    ed, _asked = _group_editor(900)
    asked = []
    ed.picture_look_requested.connect(asked.append)
    QTest.mouseDClick(ed.viewport(), Qt.MouseButton.LeftButton, pos=_point_of(ed, "square.png"))
    assert asked == ["square.png"]
    assert photo_group.group_picture_at_cursor(ed.textCursor()) == "square.png"
    ed.close()


def test_space_on_a_selected_group_picture_shows_it_and_puts_it_away():
    ed, _asked = _group_editor(900)
    shown = [False]
    asked = []
    ed.set_picture_look_check(lambda: shown[0])
    ed.picture_look_requested.connect(asked.append)
    QTest.mouseDClick(ed.viewport(), Qt.MouseButton.LeftButton, pos=_point_of(ed, "landscape.png"))
    shown[0] = True
    QTest.keyClick(ed, Qt.Key.Key_Space)
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    assert asked == ["landscape.png", "", ""]
    assert document_to_markdown(ed.document(), _image_as_read) == BOTHY     # nothing typed
    ed.close()


def test_space_in_a_caption_is_a_space():
    ed, _asked = _group_editor(900)
    asked = []
    ed.picture_look_requested.connect(asked.append)
    cursor = ed.document().find("The jetty")
    cursor.clearSelection()
    ed.setTextCursor(cursor)
    QTest.keyClick(ed, Qt.Key.Key_Space)
    assert asked == []
    assert "*The jetty  at dusk*" in document_to_markdown(ed.document(), _image_as_read)
    ed.close()
