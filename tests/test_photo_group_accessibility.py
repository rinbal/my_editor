# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a screen reader meets in a photo group: in the editor, in Quick
Look, and on the exported page, read from the accessibility tree.

In the editor a picture is one character of the text, which VoiceOver
cannot read, and its caption is drawn with it: the caret has one stop at
each picture and says its place, its description and its caption there
("Photo 2 of 5. Loch. The loch at dusk"), in the author's order. Quick
Look is named by the picture's description and leaves the keyboard in
the text.

The page is read from Chrome's own accessibility tree (the DevTools
protocol, over a socket on this computer to a headless Chrome started
here): the group named by its count, each picture a button named by its
description in the author's order, which is the order Tab goes through
them; the full view a modal dialog that takes the focus, keeps it, and
gives it back to the picture it was opened from.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer, QUrl
from PySide6.QtGui import QAccessible, QTextCursor, QTextFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import announce
from tests.test_editor_pictures import BOTHY, _group_editor, _point_of
from tests.photo_corners import PHOTOS
from tests.test_export_photo_groups import CHROME, NAMES, _page, needs_chrome


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------------- #
# The editor                                                                   #
# --------------------------------------------------------------------------- #

def _walk(ed, monkeypatch):
    said = []
    monkeypatch.setattr(announce, "announce", lambda source, text: said.append(text))
    cursor = ed.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    ed.setTextCursor(cursor)
    ed.setFocus()
    end = ed.document().characterCount() - 1
    while ed.textCursor().position() < end:
        before = ed.textCursor().position()
        QTest.keyClick(ed, Qt.Key.Key_Right)
        if ed.textCursor().position() == before:
            break
    return said


def test_the_caret_says_each_picture_of_a_group_in_the_authors_order(monkeypatch):
    ed, _asked = _group_editor(900)
    said = _walk(ed, monkeypatch)
    assert said == ["Photo 1 of 5. Steg. The jetty at dusk", "Photo 2 of 5. Loch",
                    "Photo 3 of 5. Hut", "Photo 4 of 5. Panorama",
                    "Photo 5 of 5. Plan. Floor plan of the bothy"]
    ed.close()


def test_a_picture_without_a_description_is_said_by_its_place(monkeypatch):
    ed, _asked = _group_editor(900, "![A](square.png)\n\n![B](photo.png)\n")
    # (Qt's reader drops a picture written without one: it is taken away here.)
    doc = ed.document()
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            fmt = fragment.charFormat()
            if fmt.isImageFormat() and fmt.toImageFormat().name() == "photo.png":
                cursor = QTextCursor(doc)
                cursor.setPosition(fragment.position())
                cursor.setPosition(fragment.position() + 1, QTextCursor.MoveMode.KeepAnchor)
                fmt.setProperty(QTextFormat.Property.ImageAltText, "")
                cursor.setCharFormat(fmt)
            it += 1
        block = block.next()
    assert _walk(ed, monkeypatch) == ["Photo 1 of 2. A", "Photo 2 of 2"]
    ed.close()


class _Window(QWidget):
    """The part of the main window Quick Look of a picture asks for."""

    is_dark_theme = False

    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        from main_window import MainWindow
        self.look = MainWindow._look_at_picture.__get__(self)

    def current_editor(self):
        return self.editor

    def _picture_file(self, editor, name):
        return str(PHOTOS / name)


def test_quick_look_is_named_by_the_description_and_leaves_the_keyboard_in_the_text():
    ed, _asked = _group_editor(900)
    window = _Window(ed)
    ed.picture_look_requested.connect(lambda name: window.look(ed, name))
    ed.set_picture_look_check(lambda: window._picture_look.isVisible()
                              if getattr(window, "_picture_look", None) else False)
    ed.activateWindow()
    ed.setFocus()
    QTest.mouseDClick(ed.viewport(), Qt.MouseButton.LeftButton, pos=_point_of(ed, "square.png"))
    QApplication.processEvents()
    panel = window._picture_look
    assert panel.isVisible()
    assert QAccessible.queryAccessibleInterface(panel).text(QAccessible.Text.Name) == "Quick Look"
    assert panel.windowTitle() == "Hut"

    def names():
        return {QAccessible.queryAccessibleInterface(w).text(QAccessible.Text.Name)
                for w in panel.findChildren(QWidget) if w.isVisible()}
    for _ in range(3000):                  # it is read in the background
        if "Picture" in names():
            break
        QTest.qWait(20)
    assert "Picture" in names() and "Zoom In" in names()
    # Shown without taking the keyboard: the text keeps it in its window.
    # (The offscreen platform makes every window shown the active one;
    # Cocoa leaves this panel inactive, as the Finder's Quick Look is.)
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert ed.window().focusWidget() is ed
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert not panel.isVisible()
    assert ed.window().focusWidget() is ed                 # back in the text
    ed.close()
    window.close()


# --------------------------------------------------------------------------- #
# The exported page, through Chrome's accessibility tree                       #
# --------------------------------------------------------------------------- #

class _DevTools:
    """A headless Chrome on this computer, driven over its DevTools socket.
    (A bound of a minute on each step keeps a broken browser from holding
    the run; no answer is timed.)"""

    LIMIT_MS = 60_000

    def __init__(self, tmp_path, width):
        from PySide6.QtWebSockets import QWebSocket
        profile = tmp_path / "devtools-profile"
        self.process = subprocess.Popen(
            [CHROME, "--headless", "--disable-gpu", "--no-first-run",
             "--disable-background-networking", "--disable-component-update",
             "--force-prefers-reduced-motion", "--force-device-scale-factor=1",
             f"--window-size={width},2400", f"--user-data-dir={profile}",
             "--remote-debugging-port=0", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = profile / "DevToolsActivePort"
        started = time.monotonic()
        while not port_file.exists() or not port_file.read_text(encoding="ascii").strip():
            assert time.monotonic() - started < self.LIMIT_MS / 1000, "Chrome did not start"
            QTest.qWait(50)
        port = port_file.read_text(encoding="ascii").split()[0]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list") as answer:
            page = next(t for t in json.load(answer) if t["type"] == "page")
        self.socket = QWebSocket()
        self.replies, self.events, self.next_id = {}, [], 0
        self.socket.textMessageReceived.connect(self._received)
        self._wait(lambda: self.socket.state().name == "ConnectedState",
                   lambda: self.socket.open(QUrl(page["webSocketDebuggerUrl"])))

    def _received(self, text):
        message = json.loads(text)
        if "id" in message:
            self.replies[message["id"]] = message
        else:
            self.events.append(message)

    def _wait(self, done, start=lambda: None):
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: done() and loop.quit())
        limit = QTimer()
        limit.setSingleShot(True)
        limit.timeout.connect(loop.quit)
        start()
        if not done():
            timer.start()
            limit.start(self.LIMIT_MS)
            loop.exec()
        assert done(), "no answer from Chrome"

    def call(self, method, **params):
        self.next_id += 1
        number = self.next_id
        self.socket.sendTextMessage(json.dumps({"id": number, "method": method, "params": params}))
        self._wait(lambda: number in self.replies)
        reply = self.replies.pop(number)
        assert "error" not in reply, reply
        return reply.get("result", {})

    def evaluate(self, expression):
        return self.call("Runtime.evaluate", expression=expression, awaitPromise=True,
                         returnByValue=True)["result"].get("value")

    def key(self, key, code, number):
        for kind in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=kind, key=key, code=code,
                      windowsVirtualKeyCode=number, nativeVirtualKeyCode=number)
        self.settle()

    def settle(self):
        self.evaluate("new Promise(done => requestAnimationFrame(() => "
                      "requestAnimationFrame(() => setTimeout(done, 30))))")

    def tree(self):
        nodes = self.call("Accessibility.getFullAXTree")["nodes"]
        return {node["nodeId"]: node for node in nodes}

    def focused(self):
        """What has the focus (the page itself is focused too, around it)."""
        found = [node for node in self.tree().values()
                 if any(p["name"] == "focused" and p["value"].get("value")
                        for p in node.get("properties", []))
                 and _role(node) != "RootWebArea"]
        return (_role(found[-1]), _name(found[-1])) if found else None

    def close(self):
        try:
            self.socket.close()
        finally:
            self.process.kill()
            self.process.wait()


def _name(node):
    return node.get("name", {}).get("value", "")


def _role(node):
    return node.get("role", {}).get("value", "")


def _below(tree, node):
    """The nodes under ``node``, in the tree's order (what VoiceOver reads)."""
    out = []
    for child in node.get("childIds", []):
        found = tree.get(child)
        if found is not None:
            if not found.get("ignored"):
                out.append(found)
            out.extend(_below(tree, found))
    return out


@pytest.fixture
def page(tmp_path):
    path = tmp_path / "bothy.html"
    path.write_text(_page(), encoding="utf-8")
    browser = _DevTools(tmp_path, 1440)
    try:
        browser.call("Page.enable")
        browser.call("Accessibility.enable")
        browser.call("Page.navigate", url=path.as_uri())
        browser._wait(lambda: any(e["method"] == "Page.loadEventFired" for e in browser.events))
        browser.evaluate("new Promise(done => { const look = () => "
                         "document.documentElement.dataset.photoViewer === '' ? done(true) "
                         ": setTimeout(look, 20); look() })")
        browser.settle()
        yield browser
    finally:
        browser.close()


DESCRIPTIONS = ["Steg", "Loch", "Bay", "Shore", "Pier $& jetty", "Hut", "Panorama", "Plan"]


@needs_chrome
def test_the_page_names_the_group_and_its_pictures_in_the_authors_order(page):
    tree = page.tree()
    groups = [n for n in tree.values() if _role(n) == "group" and _name(n) == "8 photos"]
    assert len(groups) == 1
    buttons = [_name(n) for n in _below(tree, groups[0]) if _role(n) == "button"]
    assert buttons == [f"Enlarge: {alt}" for alt in DESCRIPTIONS]
    assert len(DESCRIPTIONS) == len(NAMES)


@needs_chrome
def test_tab_goes_through_the_pictures_in_the_authors_order(page):
    seen = []
    for _ in DESCRIPTIONS:
        page.key("Tab", "Tab", 9)
        seen.append(page.focused())
    assert seen == [("button", f"Enlarge: {alt}") for alt in DESCRIPTIONS]


@needs_chrome
def test_the_full_view_is_a_dialog_that_keeps_the_focus_and_gives_it_back(page):
    page.key("Tab", "Tab", 9)
    page.key("Tab", "Tab", 9)                   # the second picture
    assert page.focused() == ("button", "Enlarge: Loch")
    page.key("Enter", "Enter", 13)
    tree = page.tree()
    dialogs = [n for n in tree.values() if _role(n) == "dialog"]
    assert [_name(n) for n in dialogs] == ["Full View"]
    assert any(p["name"] == "modal" and p["value"].get("value")
               for p in dialogs[0].get("properties", []))
    assert page.focused() == ("button", "Close")
    stops = []
    for _ in range(5):
        page.key("Tab", "Tab", 9)
        stops.append(page.focused())
    assert stops == [("button", "Open Original in a New Tab"), ("button", "Previous Photo"),
                     ("button", "Next Photo"), ("button", "Close"),
                     ("button", "Open Original in a New Tab")]
    page.key("Escape", "Escape", 27)
    # It goes once its picture is back in place (at once with Reduce Motion).
    page.evaluate("new Promise(done => { const look = () => document.querySelector("
                  "'[role=dialog]').hidden ? done(true) : setTimeout(look, 20); look() })")
    page.settle()
    assert not [n for n in page.tree().values() if _role(n) == "dialog" and not n.get("ignored")]
    assert page.focused() == ("button", "Enlarge: Loch")
