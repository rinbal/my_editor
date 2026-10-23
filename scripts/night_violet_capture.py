"""Deterministic real-widget review fixtures; never connects to a live account.

Run with the repository's test environment. Set QT_QPA_PLATFORM=cocoa for
native macOS frame captures, or offscreen for reproducible client captures.
The same script is usable against the unchanged baseline checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--native", action="store_true")
parser.add_argument("--extended", action="store_true")
parser.add_argument("--states-only", action="store_true")
parser.add_argument("--only", nargs="+", help="Capture only these named fixtures")
args = parser.parse_args()
os.environ.setdefault("QT_QPA_PLATFORM", "cocoa" if args.native else "offscreen")
os.environ.setdefault("MYEDITOR_LANGUAGE", "en")
os.environ.setdefault("MYEDITOR_IPC_NAME", f"myeditor-visual-fixture-{os.getpid()}")
sys.path.insert(0, str(args.repo))
import i18n
i18n.install(os.environ["MYEDITOR_LANGUAGE"])

import shiboken6
import PySide6
from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytest import MonkeyPatch
import theme
from tests.test_imports_window import controller_for, fill, ImportsWindow
from tests.test_publish_sheets import article
from tests.test_drafts_panel import healthy_store, make_profile
from tests.test_media_library import FakeLoader, FakeStore, media

app = QApplication([])
# This fixture opens independent windows in sequence. Closing one must not
# request application shutdown while the next fixture is still being built.
app.setQuitOnLastWindowClosed(False)
i18n.install_qt_translations(app)
app.setStyle(theme.AppStyle("Fusion"))
theme.install_style_sheet_guard(app)
args.output.mkdir(parents=True, exist_ok=True)
captures = []
state_checks = []


def capture(widget, name, dark, *, close=True):
    if args.only and name not in args.only:
        # Constructors queue UI callbacks; drain them before destroying a
        # skipped fixture, just as the normal exposed-window path does.
        QTest.qWait(350)
        if hasattr(widget, "_all_editors"):
            for editor in widget._all_editors():
                editor.document().setModified(False)
        if close:
            widget.close()
            widget.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        return
    widget.show()
    widget.raise_()
    widget.activateWindow()
    QTest.qWait(350)
    filename = f"{name}-{'dark' if dark else 'light'}.png"
    record = dict(file=filename, logical_size=[widget.width(), widget.height()],
                  scale=widget.devicePixelRatioF(), platform=app.platformName())
    if args.native and sys.platform == "darwin":
        # Capture the real compositor first. QWidget.grab() repaints into a
        # separate backing store and must not precede the native evidence.
        QTest.qWaitForWindowExposed(widget, 5000)
        QTest.qWait(1800)
        from ctypes import c_void_p
        from rubicon.objc import ObjCInstance
        view = ObjCInstance(c_void_p(int(widget.winId())))
        number = int(view.window.windowNumber)
        result = subprocess.run(["screencapture", "-x", "-o", "-l", str(number),
                                 str(args.output / ("native-" + filename))], capture_output=True)
        record["native_capture"] = result.returncode == 0
        record["native_error"] = result.stderr.decode()
    assert widget.grab().save(str(args.output / filename))
    captures.append(record)
    if hasattr(widget, "_all_editors"):
        for ed in widget._all_editors():
            ed.document().setModified(False)
    if close:
        widget.close()
        widget.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def illustration(index=0):
    # Deterministic, synthetic image pixels: no user photos or network requests.
    pix = QPixmap(640, 360)
    pix.fill(QColor(("#ADC9CC", "#DFC1AB", "#BCCBAD")[index % 3]))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#F4DC9A"))
    painter.drawEllipse(QRectF(420, 35, 90, 90))
    painter.setBrush(QColor("#739299"))
    painter.drawEllipse(QRectF(-90, 140, 630, 500))
    painter.setBrush(QColor("#365C63"))
    painter.drawEllipse(QRectF(230, 195, 650, 440))
    painter.end()
    return pix


with tempfile.TemporaryDirectory(prefix="night-violet-fixture-") as temp, MonkeyPatch.context() as patch:
    import recovery
    patch.setattr(recovery, "BACKUP_DIR", str(Path(temp) / "backups"))
    for dark in (() if args.states_only else (False, True)):
        theme.apply_app_theme(dark)
        folder = Path(temp) / str(dark)
        folder.mkdir()
        controller = controller_for(folder)
        fill(controller)
        imports = ImportsWindow(controller, load_settings=lambda: {}, save_settings=lambda _: None)
        imports.apply_theme(dark)
        imports.resize(1180, 760)
        capture(imports, "imports", dark)
        sheet = article(folder, body="A short article about keeping a personal journal.",
                        default_title="A thoughtful journal", is_dark=dark)
        sheet._summary_edit.setText("A space for small discoveries and ideas worth keeping.")
        sheet._card.show_article("A thoughtful journal", "A space for small discoveries and ideas worth keeping.",
                                illustration(), "Ada Writer · 2 min read")
        capture(sheet, "publishing", dark)
        from tests.window_fixture import make_window
        from nostr.ui.save_destination_dialog import SaveDestination
        from main_window import DRAFTS_PANEL
        shell = make_window(folder / "workspace", patch, settings_values={"appearance": "dark" if dark else "light"})
        shell._set_theme(dark)
        editor = shell.current_editor()
        editor.setHtml("<h1>A personal journal</h1><p>Local writing stays neutral beside Nostr drafts.</p>"
                       "<p>A quiet space to collect ideas, revise a paragraph, and keep something worth returning to.</p>")
        shell.tabs.setTabText(shell._editor_widget_index(editor), "Personal journal")
        draft = shell.new_tab()
        draft._save_destination = SaveDestination.NOSTR_DRAFT
        draft.setPlainText("Notes for tomorrow")
        shell.tabs.setTabText(shell._editor_widget_index(draft), "Notes for tomorrow")
        shell.tabs.setCurrentIndex(shell._editor_widget_index(editor))
        drafts = healthy_store()
        drafts.set_decrypted("h0", inner={"kind": 1, "content": "A small idea, saved for later.", "tags": []})
        drafts.set_decrypted("h1", inner={"kind": 30023, "content": "The places we return to.",
            "tags": [["title", "Notes from the coast"], ["summary", "A journal of small discoveries."]]})
        shell._drafts_panel.bind_store(drafts)
        shell._drafts_panel.set_active_profile(make_profile("Ada Writer"))
        shell._side_panels.open(DRAFTS_PANEL)
        shell.profile_chip.set_profile("Ada Writer", "a" * 64)
        shell.profile_chip.show()
        shell.resize(1180, 760)
        capture(shell, "editor-drafts", dark)
        from tests.test_membership_window import FakeApi, PUBKEY
        from nostr.ui.membership_window import MembershipWindow
        member = MembershipWindow(FakeApi(), pubkey=PUBKEY, known_member=True,
                                  handle="satoshi", member_year=2026, is_dark=dark,
                                  profile_address="satoshi@einundzwanzig.space",
                                  relay_listed=True, storage_used=lambda: 1_200_000_000)
        capture(member, "membership", dark)
        from nostr.ui import media_library_dialog as media_module
        loader = FakeLoader(folder / "thumbnails")
        patch.setattr(media_module, "ThumbnailLoader", lambda parent=None: loader)
        names = ["Morning walk", "Quiet hills", "Winter light", "First ideas", "Harbour", "After the rain"]
        records = [media(i) for i in range(1, 7)]
        store = FakeStore(records, names={m.hash: names[i] + ".png" for i, m in enumerate(records)})
        library = media_module.MediaLibraryDialog(store=store, is_dark=dark)
        library.resize(860, 560)
        for i, m in enumerate(records):
            library._on_thumbnail_ready(m.hash, "", illustration(i))
        capture(library, "media", dark)
        if args.extended:
            from tests.test_publish_sheets import note
            capture(note(folder, content="A small thought worth sharing. What did you notice today?", is_dark=dark), "publish-note", dark)
            from nostr.ui.save_destination_dialog import SaveDestinationDialog
            capture(SaveDestinationDialog(is_dark=dark, default=SaveDestination.NOSTR_DRAFT), "save-destination", dark)
            from nostr.ui.stash_kind_dialog import StashKindDialog
            capture(StashKindDialog(is_dark=dark), "save-draft", dark)
            from nostr.ui.profile_window import ProfileWindow
            from tests.test_profile_window import found
            profile = ProfileWindow(read=lambda done: done(found(display_name="Ada Writer", name="ada",
                about="Notes about everyday things, thoughtful tools, and places worth remembering.")),
                save=lambda *args: None, is_dark=dark)
            capture(profile, "profile", dark)
            from tests.test_connect_dialog import FakePool
            from nostr.profiles import ProfileStore
            from nostr.ui.connect_dialog import ConnectDialog
            capture(ConnectDialog(FakePool(), ProfileStore(folder / "connect.json"), is_dark=dark), "connect", dark)
            from nostr.ui.account_windows import BackupAccountWindow, CreateAccountWindow, RestoreAccountWindow
            capture(CreateAccountWindow(store=None, vault=None, connect_signer=lambda *args, **kw: None, is_dark=dark), "create-account", dark)
            capture(RestoreAccountWindow(store=None, vault=None, is_dark=dark), "restore-account", dark)
            capture(BackupAccountWindow(secret=b"\x01" * 32, is_dark=dark), "backup-account", dark)
            from nostr.ui.cover_crop_dialog import CoverCropDialog
            capture(CoverCropDialog(illustration().toImage(), is_dark=dark), "cover-crop", dark)
            from tests.test_media_servers_dialog import sheet as servers_sheet
            servers, _, _ = servers_sheet(folder)
            if hasattr(servers, "apply_theme"):
                servers.apply_theme(dark)
            capture(servers, "media-servers", dark)
            from tests.test_reader_window import window as reader_window
            reader = reader_window()
            reader.apply_theme(dark)
            reader.set_image(illustration())
            capture(reader, "reader", dark)
            from nostr.ui.imports_sheets import FollowSheet, FileSheet, LinkSheet
            for name, cls in (("follow-source", FollowSheet), ("import-file", FileSheet), ("import-link", LinkSheet)):
                capture(cls(controller, dark=dark), name, dark)
            from nostr.ui.imports_actions import OptionsPopover
            options = OptionsPopover()
            options.show_choices(copy=True, full=None, has_images=True)
            capture(options, "import-options", dark)
            from tests.test_membership_window import to_pay
            api = FakeApi()
            payment = MembershipWindow(api, pubkey=PUBKEY, is_dark=dark)
            to_pay(payment, api)
            capture(payment, "membership-payment", dark)
            from nostr.ui.media_preview import QuickLook
            from PySide6.QtWidgets import QWidget
            grid = QWidget()
            quick = QuickLook(grid=grid, is_dark=dark)
            quick.show_file(name="Morning walk.png", detail="A fixture illustration", url="", glyph="",
                            picture=illustration())
            capture(quick, "quick-look", dark)
            from tests.test_publish_copy_dialog import build as copy_dialog, private
            copying = copy_dialog(folder, [private()])
            if hasattr(copying.dialog, "apply_theme"):
                copying.dialog.apply_theme(dark)
            capture(copying.dialog, "share-media", dark)
            from tests.test_imports_image_review import Images
            from nostr.ui.image_review_dialog import ImageReviewDialog
            images = Images()
            urls = [f"https://fixture.example/landscape-{i}.png" for i in range(3)]
            images.kept = {url: illustration(i).toImage() for i, url in enumerate(urls)}
            review = ImageReviewDialog(urls, skip_urls=[urls[-1]], image_source=images)
            capture(review, "review-images", dark)
            from nostr.ui.imports_source_settings import SourceSettingsSheet
            from nostr.imports.feed_list import source_key
            from tests.test_imports_window import JOURNAL
            capture(SourceSettingsSheet(controller, source_key(JOURNAL), dark=dark), "source-settings", dark)
            from nostr.ui.imports_activity import JobCard
            from tests.test_imports_progress import job, row
            activity = JobCard()
            activity.show_job(job(rows=[row("done"), row("existing"), row("pending", stage="images")]))
            capture(activity, "import-progress", dark)
            from nostr.ui.draft_relays_sheet import DraftRelaysSheet
            from tests.test_draft_relays_sheet import Lists
            capture(DraftRelaysSheet(Lists(["wss://relay.example"]), is_dark=dark), "draft-relays", dark)
            from tests.test_article_details_panel import panel_with
            details, _ = panel_with()
            details.set_dark(dark)
            details.resize(340, 760)
            capture(details, "article-details", dark)
            from nostr.ui.reference_popover import ReferencePopover
            capture(ReferencePopover(), "reference", dark)
            from tests.test_publish_sheets import common
            from nostr.ui.mention_picker import MentionPicker
            services = common(folder, is_dark=dark)
            picker = MentionPicker(services["known_people"], services["search_client"], is_dark=dark)
            from nostr.known_people import Person
            picker._render([Person(pubkey="a" * 64, display_name="Ada Writer"),
                            Person(pubkey="b" * 64, display_name="Bob Reader")], searching=False)
            capture(picker, "mentions", dark)
            from nostr.ui.draft_conflict_banner import DraftConflictBanner
            banner = DraftConflictBanner(is_dark=dark)
            banner.resize(720, banner.sizeHint().height())
            capture(banner, "draft-conflict", dark)
            from alerts import Alert, Button, DESTRUCTIVE
            alert_kw = dict(title="Remove this draft?", message="The draft will be deleted from your relays.",
                buttons=(Button("Cancel", False, "default"), Button("Delete", True, DESTRUCTIVE)), is_dark=dark)
            import inspect
            if "nostr" in inspect.signature(Alert).parameters:
                alert_kw["nostr"] = True
            capture(Alert(None, **alert_kw), "nostr-alert", dark)
            from nostr.ui.account_windows import MoveToSignerWindow
            capture(MoveToSignerWindow(secret=b"\x01" * 32, name="Ada Writer",
                connect_signer=lambda *args, **kw: None, is_dark=dark), "move-key", dark)
            from tests.test_settings_window import Fixture as SettingsFixture
            settings = SettingsFixture(nostr=True)
            settings.window.apply_theme(dark)
            capture(settings.window, "settings", dark)
            from tests.test_published_view import make as published_fixture, article as published_article, Profile
            published, _, relays, _, _ = published_fixture(published_article("coast", "Notes from the coast"))
            published.set_account(Profile())
            relays.run()
            published.apply_theme(dark)
            capture(published, "published", dark)
            from tests.test_drafts_preview import article as draft_article, NOW
            from nostr.ui.drafts_preview import PreviewPopover
            for full in (False, True):
                preview = PreviewPopover(is_dark=dark)
                preview.compose(draft_article(), width=460 if full else 360,
                                max_height=640 if full else 460, now=NOW, full=full)
                preview.show_preview(keyboard=True)
                capture(preview, "draft-preview-full" if full else "draft-preview", dark)
            from nostr.ui.media_viewer import MediaViewer
            from nostr.ui.media_source import PreviewResolver
            with MonkeyPatch.context() as viewer_patch:
                viewer_patch.setattr(PreviewResolver, "resolve", lambda *args, **kw: None)
                viewer = MediaViewer(loader=None, visibility=None, is_dark=dark)
                viewer.play(media(1), "audio/mp3", "Example recording", nostr=True)
                viewer._failed("The file could not be loaded. Try again.")
                capture(viewer, "media-viewer-nostr", dark, close=False)
                viewer.play(media(1), "audio/mp3", "Local recording", nostr=False)
                capture(viewer, "media-viewer-local", dark)
        controller.account_changed(None)
        for widget in (imports, sheet, shell, member, library):
            if shiboken6.isValid(widget):
                widget.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        print("captured", "dark" if dark else "light", flush=True)

    if args.extended or args.states_only:
        # Existing application commands drive already-open detached windows.
        # No system-wide preference is changed by this review fixture.
        from nostr.ui import appearance
        from tests.window_fixture import make_window
        from tests.test_reader_window import window as reader_window
        live = make_window(Path(temp) / "live", patch)
        live.resize(960, 640)
        live.show()
        editor = live.current_editor()
        editor.setPlainText("Local text survives every appearance change.")
        writing = article(Path(temp), default_title="Unsaved title", is_dark=True)
        writing._summary_edit.setText("Unsaved summary")
        reading = reader_window()
        reading.show()
        writing.show()
        writing.activateWindow()
        writing._title_edit.setFocus()
        writing._title_edit.setSelection(2, 7)
        QTest.qWait(150)
        for mode in ("light", "dark", "system"):
            selected = writing._title_edit.selectedText()
            live.set_appearance(mode)
            QTest.qWait(150)
            dark = live.is_dark_theme
            assert writing._title_edit.text() == "Unsaved title"
            assert writing._title_edit.selectedText() == selected
            assert writing._summary_edit.text() == "Unsaved summary"
            assert editor.toPlainText() == "Local text survives every appearance change."
            assert writing.palette().window().color() == QColor(appearance.roles(dark).panel)
            assert reading._is_dark == dark
            state_checks.append({"check": "open windows preserve content and selection", "mode": mode,
                                 "resolved_dark": dark, "passed": True})
            if mode != "system":
                capture(writing, "live-publishing", dark, close=False)
                capture(reading, "live-reader", dark, close=False)
        # Standard native flags remain intact across resize/fullscreen/restore.
        flags = live.windowFlags()
        live.showFullScreen()
        QTest.qWait(1800)
        assert live.isFullScreen()
        live.showNormal()
        for _ in range(50):
            QTest.qWait(100)
            if not live.isFullScreen():
                break
        live.resize(1000, 680)
        QTest.qWait(400)
        assert not live.isFullScreen(), "native fullscreen did not exit"
        assert live.windowFlags() == flags, (int(flags), int(live.windowFlags()))
        state_checks.append({"check": "native fullscreen, restore and resize", "passed": True})
        writing.close()
        reading.close()
        editor.document().setModified(False)
        live.close()

revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.repo, text=True).strip()
(args.output / "manifest.json").write_text(json.dumps(dict(
    revision=revision, dirty=bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=args.repo)),
    capture_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    os=platform.platform(), qt=PySide6.__version__, language=os.environ["MYEDITOR_LANGUAGE"], captures=captures,
    state_checks=state_checks), indent=2), encoding="utf-8")
