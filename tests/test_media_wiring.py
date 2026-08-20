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
