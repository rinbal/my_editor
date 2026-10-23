# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The media seams the application actually connects on startup.

Every object in the media stack is injected, which is what makes the
rest of the suite able to test it. The cost is that a seam can be left
unplugged without a single test noticing: the mechanism keeps its
coverage, the app quietly loses the behaviour, and the symptom only
shows up on a machine that is offline.

That is exactly what happened to the blob cache. ``MediaStore`` seeds
the cache it is given and ``tests/test_blossom_store.py`` pins that, but
nothing checked that the running app gives it one. Removing the keyword
argument from ``MainWindow`` left the whole suite green while an
uploaded image went back to being downloaded from a server to be shown.

``MainWindow`` cannot be constructed here to check it the direct way. Its
``__init__`` reads the real settings file, builds a relay pool, and for
a profile from a previous session starts a metadata fetch, so a test
that built one would touch the user's configuration and the network,
which AD-8 forbids. ``tests/test_media_document.py`` works around the
same problem by lifting unbound methods off the class. Its constructor
has no methods to lift, so the wiring is read out of the source instead:
narrower than running it, and it does catch a dropped argument and a
reordered construction, which is the whole failure mode.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main_window import MainWindow


@pytest.fixture(scope="module")
def init_body():
    """The statements of ``MainWindow.__init__``, in order."""
    source = textwrap.dedent(inspect.getsource(MainWindow.__init__))
    return ast.parse(source).body[0].body


def statement_index(body, matches) -> int:
    """Position of the first top-level statement containing a match."""
    for index, statement in enumerate(body):
        if any(matches(node) for node in ast.walk(statement)):
            return index
    return -1


def constructor(body, name: str) -> ast.Call:
    """The one ``name(...)`` call in ``body``."""
    calls = [
        node for statement in body for node in ast.walk(statement)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == name
    ]
    assert len(calls) == 1, f"expected exactly one {name}(...), got {len(calls)}"
    return calls[0]


def attribute_argument(call: ast.Call, keyword: str) -> str:
    """The ``self.<attr>`` a keyword argument names, or an empty string."""
    for kwarg in call.keywords:
        if kwarg.arg != keyword:
            continue
        value = kwarg.value
        if (isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"):
            return value.attr
    return ""


def assigns(attr: str):
    def matches(node) -> bool:
        return (isinstance(node, ast.Attribute)
                and node.attr == attr
                and isinstance(node.ctx, ast.Store))
    return matches


def calls(name: str):
    def matches(node) -> bool:
        return isinstance(node, ast.Call) and getattr(node.func, "id", "") == name
    return matches


def test_the_media_store_is_given_a_blob_cache(init_body):
    """AD-2: an uploaded image is displayable with the network gone.

    The store writes the bytes into this cache before it sends them, so
    a failed upload, a retry and an offline session all still have
    something to show. Without the argument the store keeps uploading
    and silently caches nothing.
    """
    assert attribute_argument(constructor(init_body, "MediaStore"),
                              "blob_cache") == "_media_image_loader"


def test_the_cache_the_store_seeds_is_the_one_the_editor_reads_from(init_body):
    """One cache, or the seeding writes where nothing ever reads.

    ``AssetManager`` resolves a document's images through the blob store
    it is handed. Seeding a different object would be a write with no
    reader and the offline guarantee would still be broken.
    """
    store = constructor(init_body, "MediaStore")
    assets = constructor(init_body, "AssetManager")
    assert attribute_argument(store, "blob_cache") == "_media_image_loader"
    assert attribute_argument(assets, "blob_store") == "_media_image_loader"


def test_the_cache_is_built_before_the_store_that_seeds_it(init_body):
    """Construction order is load bearing, so it is pinned.

    The cache used to be created after the store. Passing it in means it
    has to exist first, and moving it back is an ``AttributeError`` at
    startup rather than anything a running test would report.
    """
    cache_at = statement_index(init_body, assigns("_media_image_loader"))
    store_at = statement_index(init_body, calls("MediaStore"))
    assert cache_at >= 0 and store_at >= 0
    assert cache_at < store_at


# --------------------------------------------------------------------- #
# The private-media seams                                                #
# --------------------------------------------------------------------- #

def test_the_copy_maker_writes_to_the_ledger_the_app_reads(init_body):
    """One ledger, or a public copy is listed where nothing looks.

    The ledger is the only record of what this app has published and
    therefore the only way to revoke any of it. A copy maker committing
    to one object while the interface reads another would show the user
    an empty list of public copies while their pictures were on a
    server.
    """
    maker = constructor(init_body, "PublicCopyMaker")
    view = constructor(init_body, "MediaVisibility")
    assert attribute_argument(maker, "ledger") == "_public_ledger"
    assert attribute_argument(view, "ledger") == "_public_ledger"


def test_the_copy_maker_uploads_through_the_ordinary_store(init_body):
    """A public copy is an upload like any other.

    Going through ``MediaStore`` is what gives it the same auth, the
    same hash verification and the same mirroring as everything else
    this app sends. A private upload path of its own would be a second
    place for all of that to drift.
    """
    maker = constructor(init_body, "PublicCopyMaker")
    assert attribute_argument(maker, "uploader") == "_media_store"


def test_the_copy_maker_fetches_through_the_guarded_downloader(init_body):
    """The fetch reuses the loader's policy checks rather than repeating them.

    That one object owns the media-policy test, the redirect rules, the
    hop limit, the final-URL revalidation after redirects and the size
    cap. A second downloader would be a second place for any of those to
    be forgotten.
    """
    maker = constructor(init_body, "PublicCopyMaker")
    assert attribute_argument(maker, "fetcher") == "_media_image_loader"


def test_the_visibility_view_reads_the_library_that_holds_the_keys(init_body):
    view = constructor(init_body, "MediaVisibility")
    assert attribute_argument(view, "library") == "_private_library"


def test_the_ledger_and_library_exist_before_the_maker_that_uses_them(init_body):
    ledger_at = statement_index(init_body, assigns("_public_ledger"))
    library_at = statement_index(init_body, assigns("_private_library"))
    maker_at = statement_index(init_body, calls("PublicCopyMaker"))
    assert ledger_at >= 0 and library_at >= 0 and maker_at >= 0
    assert ledger_at < maker_at
    assert library_at < maker_at


def test_both_pickers_are_told_what_is_private(init_body):
    """A picker with no visibility shows a private file as an ordinary one.

    Nothing breaks, which is the problem: the badge is gone, the warning
    never appears, and the first the user hears of it is the gate at the
    end of the publish.
    """
    source = textwrap.dedent(inspect.getsource(MainWindow))
    tree = ast.parse(source)
    pickers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "MediaLibraryDialog"
    ]
    assert pickers, "no MediaLibraryDialog constructed"
    for call in pickers:
        assert attribute_argument(call, "visibility") == "_media_visibility"


def test_the_article_dialog_is_given_the_gate(init_body):
    """A cover image is the most public picture in an article.

    Without the maker the publish dialog can still warn, but it cannot
    make the copy, so a private pick becomes a refusal instead of a
    choice.
    """
    source = textwrap.dedent(inspect.getsource(MainWindow))
    tree = ast.parse(source)
    calls_found = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "PublishArticleDialog"
    ]
    assert len(calls_found) == 1
    assert attribute_argument(calls_found[0], "media_visibility") == "_media_visibility"
    assert attribute_argument(calls_found[0], "copy_maker") == "_copy_maker"


def test_every_picker_is_told_to_follow_the_private_library():
    """A picker that does not follow the library never learns anything.

    Without the binding the grid is painted once, before the load has
    answered, and never repainted. Every file stays unchecked, the
    library's own "stayed closed" sentence reaches no widget, and the
    per-file failures are read by nobody.
    """
    source = textwrap.dedent(inspect.getsource(MainWindow))
    tree = ast.parse(source)
    bound = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "bind_private_library"
    ]
    pickers = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "MediaLibraryDialog"
    ]
    assert len(bound) == len(pickers)
    for call in bound:
        assert len(call.args) == 1
        argument = call.args[0]
        assert isinstance(argument, ast.Attribute)
        assert argument.attr == "_private_library"


def test_the_article_dialog_can_open_the_library_for_its_cover_picker():
    """The cover picker is a second way into the same choice.

    Its library is never bound anywhere else, so without this argument
    every file in it reads as unchecked and no cover can be picked at
    all, which is the honest failure but a useless one.
    """
    source = textwrap.dedent(inspect.getsource(MainWindow))
    tree = ast.parse(source)
    calls_found = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "PublishArticleDialog"
    ]
    assert len(calls_found) == 1
    assert attribute_argument(
        calls_found[0], "private_library") == "_private_library"


# --------------------------------------------------------------------- #
# The media servers the account publishes (kind 10063)                   #
# --------------------------------------------------------------------- #

def test_the_published_server_list_shares_the_stores_settings(init_body):
    """What the list adopts must be what the store uploads to. A second
    settings object would adopt into a file the store never reads again
    in this session."""
    call = constructor(init_body, "UserServerList")
    settings = [k.value for k in call.keywords if k.arg == "settings"]
    assert len(settings) == 1
    value = settings[0]
    assert isinstance(value, ast.Attribute) and value.attr == "settings"
    assert isinstance(value.value, ast.Attribute) and value.value.attr == "_media_store"


def test_dead_image_addresses_are_recovered_from_the_published_servers(init_body):
    assets = constructor(init_body, "AssetManager")
    providers = [k.value for k in assets.keywords if k.arg == "recovery_provider"]
    assert len(providers) == 1
    assert isinstance(providers[0], ast.Attribute)
    assert providers[0].attr == "recovery_servers"
    list_at = statement_index(init_body, assigns("_server_list"))
    assets_at = statement_index(init_body, calls("AssetManager"))
    assert 0 <= list_at < assets_at


def test_the_media_servers_sheet_is_offered_the_published_servers():
    """The servers the account lists are offered where servers are
    managed: when the sheet opens, and while it is open when the list
    changes."""
    source = textwrap.dedent(inspect.getsource(MainWindow._open_media_servers))
    assert "suggestions=self._media_server_suggestions()" in source
    source = textwrap.dedent(inspect.getsource(MainWindow._on_media_server_suggestions))
    assert "offer(self._media_server_suggestions())" in source


def test_sharing_the_server_list_asks_first_then_publishes_what_uploads_use(monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    import main_window as mw_module
    from nostr.outbox import writer as outbox_writer

    published, refreshed, shown = [], [], []

    class FakeWriter(QObject):
        finished = Signal(object)

        def start(self):
            self.finished.emit(outbox_writer.WriteOutcome(outbox_writer.WRITTEN))

    def fake_publish(*, servers, **_deps):
        published.append(servers)
        return FakeWriter()

    answers = iter([False, True])
    monkeypatch.setattr(mw_module, "ask", lambda *a, **kw: next(answers))
    monkeypatch.setattr(mw_module, "publish_server_list", fake_publish)
    active = SimpleNamespace(user_pubkey="ab" * 32)
    host = SimpleNamespace(
        _profile_store=SimpleNamespace(default=lambda: active),
        _media_store=SimpleNamespace(target_servers=lambda: ["https://a.example",
                                                              "https://b.example/"]),
        _visible_media_library=lambda: None,
        _relay_pool=None, _relay_directory=None, _session_pool=None,
        _server_list=SimpleNamespace(refresh=lambda p, force=False: refreshed.append(force)),
        status=SimpleNamespace(showMessage=lambda text, ms=0, **context: shown.append(text)),
        is_dark_theme=False,
    )
    MainWindow._share_media_server_list(host)          # Cancel: nothing happens
    assert published == []
    MainWindow._share_media_server_list(host)          # Share List
    assert published == [["https://a.example", "https://b.example"]]
    assert refreshed == [True] and shown == ["Your media server list is shared."]


def test_a_library_upload_that_fails_with_no_library_open_is_said_in_the_window():
    """The store owns the job, so its failure reaches the person even
    after the library window that started it was closed: counted in the
    status bar (tests/test_library_failures_status.py). An open library
    says it itself."""
    from types import SimpleNamespace
    shown = []
    library = None
    host = SimpleNamespace(
        _media_store=SimpleNamespace(can_retry=lambda name: name == "Holiday.png"),
        _visible_media_library=lambda: library,
        _library_failures={},
    )
    host._show_library_failures = lambda: shown.append(dict(host._library_failures))
    reason = "Couldn’t reach your media server. Check your internet connection."
    MainWindow._on_library_upload_failed(host, "Holiday.png", reason)
    MainWindow._on_library_upload_failed(host, f"asset:{'e' * 64}", reason)   # the editor's own
    assert shown == [{"Holiday.png": reason}]
    library = object()
    MainWindow._on_library_upload_failed(host, "Holiday.png", reason)
    assert len(shown) == 1


def test_the_window_listens_for_library_upload_failures(init_body):
    source = textwrap.dedent(inspect.getsource(MainWindow.__init__))
    assert "upload_failed.connect(self._on_library_upload_failed)" in source


def _quitting_host(added, saves=()):
    from types import SimpleNamespace
    host = SimpleNamespace(
        tabs=SimpleNamespace(count=lambda: 0, widget=lambda i: None),
        _media_store=SimpleNamespace(added_uploads=lambda: list(added)),
        _media_saver=SimpleNamespace(pending=lambda: list(saves)),
    )
    for name in ("_uploading", "_still_uploading", "_what_leaving_does", "_may_leave_uploads",
                 "_save_question_words", "_editor_from_widget", "_library_uploads",
                 "_media_saves", "_unsaved_tabs", "_sheet_from_widget"):
        method = getattr(MainWindow, name)
        setattr(host, name, method.__get__(host) if not isinstance(
            MainWindow.__dict__.get(name), staticmethod) else method)
    return host


def test_quitting_while_the_library_uploads_asks_first(monkeypatch):
    """As with audio and video in a document: quitting stops the upload,
    so it is asked, with Cancel the default."""
    import main_window as mw_module
    from nostr.blossom.store import AddedUpload
    asked = []
    monkeypatch.setattr(mw_module, "confirm_destructive",
                        lambda parent, **kw: asked.append(kw) or False)
    host = _quitting_host([AddedUpload(name="Holiday.png", mime="image/png", state="sending"),
                           AddedUpload(name="Old.png", mime="image/png", state="failed")])
    assert MainWindow._resolve_unsaved_before_closing(host) is False
    assert asked[0]["title"] == "“Holiday.png” is still uploading."
    assert asked[0]["message"] == (
        "Quitting now stops the upload, and the file isn’t added to your Media Library.")
    assert asked[0]["action"] == "Quit Anyway"


def test_quitting_with_only_failed_library_uploads_asks_nothing(monkeypatch):
    import main_window as mw_module
    from nostr.blossom.store import AddedUpload
    monkeypatch.setattr(mw_module, "confirm_destructive",
                        lambda *a, **kw: pytest.fail("asked"))
    host = _quitting_host([AddedUpload(name="Old.png", mime="image/png", state="failed")])
    assert MainWindow._resolve_unsaved_before_closing(host) is True


def _saving_host(window_open=False):
    from types import SimpleNamespace
    shown = []
    host = SimpleNamespace(
        _media_window_open=lambda: window_open,
        _media_saver=SimpleNamespace(current=""),
        status=SimpleNamespace(showMessage=lambda text, ms=0, **context: shown.append((text, ms)),
                               currentMessage=lambda: shown[-1][0] if shown else ""),
        _media_save_said="",
    )
    for name in ("_on_media_save_started", "_on_media_save_finished", "_say_media_save_running",
                 "_media_save_announced"):
        setattr(host, name, getattr(MainWindow, name).__get__(host))
    return host, shown


def test_a_save_that_outlives_its_library_window_is_said_in_the_window():
    """Closing the library doesn't stop a Save As, as in the Finder: the
    status bar says it is saving, and how it went, until the next message."""
    host, shown = _saving_host()
    host._media_saver.current = "/Users/me/Movies/Talk.mp4"
    host._say_media_save_running()                       # the library closed
    host._on_media_save_finished("/Users/me/Movies/Talk.mp4", "")
    host._on_media_save_started("/Users/me/Movies/Scan.pdf")
    host._on_media_save_finished("/Users/me/Movies/Scan.pdf", "missing")
    assert shown == [
        ("Saving “Talk.mp4”…", 0),
        ("Saved: /Users/me/Movies/Talk.mp4", 0),
        ("Saving “Scan.pdf”…", 0),
        ("“Scan.pdf” isn’t on your media servers anymore, so it couldn’t be saved.", 0),
    ]


def test_an_open_library_window_says_how_its_saves_go_itself():
    host, shown = _saving_host(window_open=True)
    host._media_saver.current = "/Users/me/Movies/Talk.mp4"
    host._say_media_save_running()
    host._on_media_save_started("/Users/me/Movies/Talk.mp4")
    host._on_media_save_finished("/Users/me/Movies/Talk.mp4", "network")
    assert shown == []


def test_the_library_windows_share_the_windows_saver():
    source = inspect.getsource(MainWindow)
    # The library and the Insert Image picker; the cover pickers of
    # Article Details and Publish as Article get it as media_saver.
    assert source.count("saver=self._media_saver,") == 4
    assert source.count("media_saver=self._media_saver,") == 2
    init = textwrap.dedent(inspect.getsource(MainWindow.__init__))
    assert "self._media_saver.started.connect(self._on_media_save_started)" in init
    assert "self._media_saver.finished.connect(self._on_media_save_finished)" in init
    assert "self._media_saver.batch_finished.connect(self._on_media_saves_finished)" in init
    assert "self._message_tip = _MessageTip(self.status)" in init


def test_quitting_stops_a_save_and_its_part_file_goes(monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from tests.test_quit_sends_sources import closing_host
    order = []
    MainWindow.closeEvent(closing_host(monkeypatch, order), QCloseEvent())
    assert "saves stopped" in order


@pytest.mark.parametrize("states, title, message", [
    (["finishing"], "“Holiday.png” is still uploading.",
     "Quitting now stops copying it to your other media servers. It’s already in your Media "
     "Library."),
    (["finishing", "finishing"], "2 files are still uploading.",
     "Quitting now stops copying them to your other media servers. They’re already in your "
     "Media Library."),
    (["finishing", "sending"], "2 files are still uploading.",
     "Quitting now stops the upload, and the file isn’t added to your Media Library."),
])
def test_quitting_while_a_library_file_is_only_being_copied_says_it_is_in_the_library(
        monkeypatch, states, title, message):
    """Once its first server has it, the file is in the library: quitting
    stops only its copies to the other servers."""
    import main_window as mw_module
    from nostr.blossom.store import AddedUpload
    asked = []
    monkeypatch.setattr(mw_module, "confirm_destructive",
                        lambda parent, **kw: asked.append(kw) or False)
    names = ["Holiday.png", "Talk.mp4"]
    host = _quitting_host([AddedUpload(name=names[i], mime="image/png", state=state)
                           for i, state in enumerate(states)])
    assert MainWindow._resolve_unsaved_before_closing(host) is False
    assert (asked[0]["title"], asked[0]["message"]) == (title, message)


@pytest.mark.parametrize("states, saves, title, message", [
    ([], ["/out/Talk.mp4"], "A file is still being saved.",
     "Quitting now stops the download, and the file isn’t saved."),
    ([], ["/out/Talk.mp4", "/out/Scan.pdf"], "2 files are still being saved.",
     "Quitting now stops the downloads, and the files aren’t saved."),
    (["sending"], ["/out/Talk.mp4"], "2 files are still uploading or being saved.",
     "Quitting now stops the upload, and the file isn’t added to your Media Library. "
     "Quitting also stops saving “Talk.mp4”."),
    (["sending"], ["/out/Talk.mp4", "/out/Scan.pdf"],
     "3 files are still uploading or being saved.",
     "Quitting now stops the upload, and the file isn’t added to your Media Library. "
     "Quitting also stops saving 2 files."),
])
def test_quitting_while_a_save_runs_asks_first(monkeypatch, states, saves, title, message):
    """Quitting would throw away a download the person asked for, as
    Safari asks before quitting with downloads: in the one question that
    asks about uploads, with Cancel the default."""
    import main_window as mw_module
    from nostr.blossom.store import AddedUpload
    asked = []
    monkeypatch.setattr(mw_module, "confirm_destructive",
                        lambda parent, **kw: asked.append(kw) or False)
    host = _quitting_host([AddedUpload(name="Holiday.png", mime="image/png", state=state)
                           for state in states], saves)
    assert MainWindow._resolve_unsaved_before_closing(host) is False
    assert (asked[0]["title"], asked[0]["message"], asked[0]["action"]) == (
        title, message, "Quit Anyway")


def test_quitting_with_unsaved_changes_and_a_save_running_says_both_in_one_question():
    host = _quitting_host([], ["/out/Talk.mp4"])
    assert host._save_question_words(host._uploading(), quitting=True,
                                     saving=host._media_saves()) == (
        "A file is still being saved. Quitting now stops the download, and the file isn’t "
        "saved. Your changes will be lost if you don’t save them.")


def test_closing_a_document_while_a_save_runs_asks_nothing(monkeypatch):
    """The save goes on: only quitting stops it."""
    host = _quitting_host([], ["/out/Talk.mp4"])
    assert host._may_leave_uploads([], quitting=False) is True


def _status_host():
    """A host with a real status bar, and a library window that opens
    and closes."""
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from nostr.ui.context_widgets import ContextStatusBar
    QApplication.instance() or QApplication([])
    host = SimpleNamespace(window_open=False, _media_saver=SimpleNamespace(current=""),
                           status=ContextStatusBar(), _media_save_said="")
    host._media_window_open = lambda: host.window_open
    for name in ("_on_media_save_started", "_on_media_save_finished", "_say_media_save_running",
                 "_media_save_announced"):
        setattr(host, name, getattr(MainWindow, name).__get__(host))
    return host


@pytest.mark.parametrize("why, said", [
    ("", "Saved: /out/Big.mp4"),
    ("missing", "“Big.mp4” isn’t on your media servers anymore, so it couldn’t be saved."),
    ("cancelled", ""),
])
def test_the_status_bar_says_how_a_save_it_announced_ended_whoever_is_open(why, said):
    """It said "Saving…" when the library closed; the library was opened
    again and the save ended there. The status bar doesn't go on saying
    it is saving."""
    host = _status_host()
    host._media_saver.current = "/out/Big.mp4"
    host._say_media_save_running()                     # the library closed
    assert host.status.currentMessage() == "Saving “Big.mp4”…"
    host.window_open = True                            # and was opened again
    host._on_media_save_finished("/out/Big.mp4", why)
    assert host.status.currentMessage() == said


def test_an_open_library_leaves_another_status_message_alone():
    host = _status_host()
    host.status.showMessage("Saved: /Users/me/Essay.md")
    host.window_open = True
    host._on_media_save_started("/out/Big.mp4")
    host._on_media_save_finished("/out/Big.mp4", "")
    assert host.status.currentMessage() == "Saved: /Users/me/Essay.md"
