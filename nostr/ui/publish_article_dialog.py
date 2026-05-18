"""Modal dialog for publishing a NIP-23 long-form article (kind 30023).

The layout is built around the *writing*, not the metadata: a large,
prominent title; a quieter inline summary; the full-width Markdown body;
and the technical fields (slug, cover image, hashtags) tucked into a
collapsible **Advanced** section that stays out of the way until needed.
"""

from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..avatar_store import AvatarStore
from ..bech32 import encode_naddr
from ..bunker import BunkerSessionPool
from ..known_people import KnownPeople
from ..outbox import RelayListCache
from ..profiles import Profile, ProfileStore
from ..publisher import PublishJob, PublishResult, build_article, slugify
from ..relay import RelayPool
from ..search import Nip50SearchClient
from .avatar import (
    AVATAR_SIZE,
    CHIP_TOTAL_WIDTH,
    compose_chip_icon,
    pixmap_for_profile,
)
from .mention_chips import MentionChipRow


# Rough words-per-minute used for the read-time hint. Public-facing prose
# tends to be ~200 wpm; long enough that the badge feels truthful.
_WPM_READ_SPEED: int = 200


# --------------------------------------------------------------------------- #
# Stylesheets — palette pulled from widgets.py / editor.py                    #
# --------------------------------------------------------------------------- #

_DARK_CSS = """
QDialog { background: #1E1E1E; }
QLabel { color: #D4D4D4; font-size: 12px; }
QLabel#article_field_label {
    color: #858585;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}
QLabel#article_status { color: #FFB347; }
QLabel#article_meta { color: #858585; font-size: 11px; }

QLineEdit#article_title {
    background: transparent;
    border: none;
    color: #FFFFFF;
    font-size: 22px;
    font-weight: 600;
    padding: 6px 0;
}
QLineEdit#article_title:focus { border-bottom: 1px solid #3C3C3C; }

QLineEdit#article_summary {
    background: transparent;
    border: none;
    color: #B5B5B5;
    font-size: 14px;
    padding: 2px 0 8px 0;
}
QLineEdit#article_summary:focus { border-bottom: 1px solid #3C3C3C; }

QLineEdit#article_advanced_field {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #264F78;
    font-size: 12px;
}

QTextEdit#article_body {
    background: #252526;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    border-radius: 4px;
    padding: 10px 12px;
    font-family: "Menlo", "Consolas", "Noto Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: #264F78;
}

QFrame#article_divider { background: #2D2D30; max-height: 1px; min-height: 1px; }

QToolButton#advanced_toggle {
    background: transparent;
    border: none;
    color: #858585;
    text-align: left;
    padding: 4px 0;
    font-size: 11px;
}
QToolButton#advanced_toggle:hover { color: #D4D4D4; }

QPushButton {
    background: #2D2D30;
    color: #D4D4D4;
    border: 1px solid #3C3C3C;
    padding: 6px 14px;
    border-radius: 4px;
}
QPushButton:hover { background: #3C3C3C; }
QPushButton:pressed { background: #1E1E1E; }
QPushButton:disabled { background: #252526; color: #6A6A6A; border-color: #2D2D2D; }

QToolButton#profile_switch {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px 6px;
    color: #CCCCCC;
}
QToolButton#profile_switch:hover { background: #3C3C3C; }
QToolButton#profile_switch::menu-indicator { image: none; width: 0; }
"""

_LIGHT_CSS = """
QDialog { background: #FFFFFF; }
QLabel { color: #333333; font-size: 12px; }
QLabel#article_field_label {
    color: #999999;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}
QLabel#article_status { color: #A05000; }
QLabel#article_meta { color: #999999; font-size: 11px; }

QLineEdit#article_title {
    background: transparent;
    border: none;
    color: #1A1A1A;
    font-size: 22px;
    font-weight: 600;
    padding: 6px 0;
}
QLineEdit#article_title:focus { border-bottom: 1px solid #E1E1E1; }

QLineEdit#article_summary {
    background: transparent;
    border: none;
    color: #555555;
    font-size: 14px;
    padding: 2px 0 8px 0;
}
QLineEdit#article_summary:focus { border-bottom: 1px solid #E1E1E1; }

QLineEdit#article_advanced_field {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: #0078D4;
    font-size: 12px;
}

QTextEdit#article_body {
    background: #FFFFFF;
    color: #333333;
    border: 1px solid #E1E1E1;
    border-radius: 4px;
    padding: 10px 12px;
    font-family: "Menlo", "Consolas", "Noto Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: #0078D4;
}

QFrame#article_divider { background: #ECECEC; max-height: 1px; min-height: 1px; }

QToolButton#advanced_toggle {
    background: transparent;
    border: none;
    color: #777777;
    text-align: left;
    padding: 4px 0;
    font-size: 11px;
}
QToolButton#advanced_toggle:hover { color: #333333; }

QPushButton {
    background: #ECECEC;
    color: #333333;
    border: 1px solid #CCCCCC;
    padding: 6px 14px;
    border-radius: 4px;
}
QPushButton:hover { background: #E1E1E1; }
QPushButton:pressed { background: #D0D0D0; }
QPushButton:disabled { background: #F8F8F8; color: #BBBBBB; border-color: #EBEBEB; }

QToolButton#profile_switch {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 3px 6px;
    color: #555555;
}
QToolButton#profile_switch:hover { background: #E1E1E1; }
QToolButton#profile_switch::menu-indicator { image: none; width: 0; }
"""

_DARK_MENU_CSS = """
QMenu { background: #252526; color: #CCCCCC; border: 1px solid #3C3C3C; padding: 4px; }
QMenu::item { padding: 4px 20px 4px 30px; }
QMenu::item:selected { background: #1E1E1E; color: #FFFFFF; }
QMenu::separator { height: 1px; background: #3C3C3C; margin: 4px 0px; }
"""

_LIGHT_MENU_CSS = """
QMenu { background: #F8F8F8; color: #333333; border: 1px solid #E1E1E1; padding: 4px; }
QMenu::item { padding: 4px 20px 4px 30px; }
QMenu::item:selected { background: #F3F3F3; color: #000000; }
QMenu::separator { height: 1px; background: #E1E1E1; margin: 4px 0px; }
"""


# --------------------------------------------------------------------------- #
# Tiny helper widgets                                                         #
# --------------------------------------------------------------------------- #

def _make_divider(parent: Optional[QWidget] = None) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("article_divider")
    line.setFrameShape(QFrame.NoFrame)
    return line


def _make_field(label_text: str, edit: QLineEdit) -> QWidget:
    """Compact label-above-input column, used inside the Advanced panel."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(label_text)
    label.setObjectName("article_field_label")
    edit.setObjectName("article_advanced_field")
    layout.addWidget(label)
    layout.addWidget(edit)
    return container


# --------------------------------------------------------------------------- #
# Dialog                                                                       #
# --------------------------------------------------------------------------- #

class PublishArticleDialog(QDialog):
    """Compose the metadata + body for a kind 30023 and publish it.

    ``published(naddr_str, results)`` is emitted on success. The naddr is
    suitable for pasting into another client. The dialog closes itself
    after a successful publish; failures are shown inline so the user can
    edit and retry.
    """

    # Args: (naddr_string, list[PublishResult])
    published = Signal(str, list)

    def __init__(
        self,
        *,
        body_markdown: str,
        active_profile: Profile,
        store: ProfileStore,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        known_people: KnownPeople,
        search_client: Nip50SearchClient,
        avatars: AvatarStore,
        default_title: str = "",
        default_slug: str = "",
        parent=None,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Publish as Article")
        self.setModal(True)
        self.resize(760, 680)
        self.setMinimumSize(640, 520)

        self._store = store
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._known_people = known_people
        self._search_client = search_client
        self._avatars = avatars
        self._is_dark = is_dark
        self._current_profile = active_profile
        self._job: Optional[PublishJob] = None
        self._signed_event_id: Optional[str] = None
        # Tracks whether the user has manually edited the slug. As long as
        # they haven't, slug stays in sync with title.
        self._slug_is_auto = True
        self._advanced_open = False

        self._build_ui(
            body_markdown=body_markdown,
            default_title=default_title,
            default_slug=default_slug,
        )
        self._apply_theme()
        self._refresh_profile_chip()
        self._refresh_meta()
        self._refresh_publish_enabled()

        # Repaint the profile-switcher button if the active profile's
        # avatar lands while the dialog is open.
        self._avatars.avatar_added.connect(self._on_avatar_added)

    def _on_avatar_added(self, pubkey_hex: str, _pixmap) -> None:
        if pubkey_hex == self._current_profile.user_pubkey:
            self._refresh_profile_chip()

    # -- UI ----------------------------------------------------------------

    def _build_ui(
        self,
        *,
        body_markdown: str,
        default_title: str,
        default_slug: str,
    ) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 16)
        root.setSpacing(8)

        # ---- Title (large, prominent) -----------------------------------
        self._title_edit = QLineEdit()
        self._title_edit.setObjectName("article_title")
        self._title_edit.setPlaceholderText("Article title")
        self._title_edit.setText(default_title)
        self._title_edit.textChanged.connect(self._on_title_changed)
        root.addWidget(self._title_edit)

        # ---- Summary (quieter, italic-feel via lower contrast) ----------
        self._summary_edit = QLineEdit()
        self._summary_edit.setObjectName("article_summary")
        self._summary_edit.setPlaceholderText("Short summary (one sentence)")
        root.addWidget(self._summary_edit)

        root.addWidget(_make_divider(self))
        root.addSpacing(6)

        # ---- Body (full-width Markdown) ---------------------------------
        self._body_edit = QTextEdit()
        self._body_edit.setObjectName("article_body")
        self._body_edit.setAcceptRichText(False)
        self._body_edit.setPlainText(body_markdown)
        self._body_edit.setPlaceholderText("Write your article in Markdown…")
        self._body_edit.textChanged.connect(self._on_body_changed)
        root.addWidget(self._body_edit, 1)

        # ---- Meta strip (word count / read time) ------------------------
        self._meta_label = QLabel("")
        self._meta_label.setObjectName("article_meta")
        self._meta_label.setAlignment(Qt.AlignRight)
        root.addWidget(self._meta_label)

        # ---- Mention chip row -------------------------------------------
        self._mention_row = MentionChipRow(
            self._known_people,
            self._search_client,
            avatars=self._avatars,
            parent=self,
            is_dark=self._is_dark,
        )
        root.addWidget(self._mention_row)

        # ---- Advanced toggle + panel ------------------------------------
        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setObjectName("advanced_toggle")
        self._advanced_toggle.setCursor(Qt.PointingHandCursor)
        self._advanced_toggle.clicked.connect(self._toggle_advanced)
        root.addWidget(self._advanced_toggle)

        self._advanced_panel = QWidget()
        adv = QHBoxLayout(self._advanced_panel)
        adv.setContentsMargins(0, 4, 0, 4)
        adv.setSpacing(12)

        self._slug_edit = QLineEdit()
        self._slug_edit.setPlaceholderText("article-slug")
        seed_slug = default_slug or (slugify(default_title) if default_title else "")
        self._slug_edit.setText(seed_slug)
        self._slug_edit.textEdited.connect(self._on_slug_edited)

        self._image_edit = QLineEdit()
        self._image_edit.setPlaceholderText("https://example.com/cover.png")

        self._tags_edit = QLineEdit()
        self._tags_edit.setPlaceholderText("comma, separated, hashtags")

        adv.addWidget(_make_field("Slug (article ID)", self._slug_edit), 1)
        adv.addWidget(_make_field("Cover image URL", self._image_edit), 2)
        adv.addWidget(_make_field("Hashtags", self._tags_edit), 1)

        self._advanced_panel.setVisible(False)
        root.addWidget(self._advanced_panel)

        root.addSpacing(4)
        root.addWidget(_make_divider(self))
        root.addSpacing(4)

        # ---- Footer: Publishing-as + status + buttons -------------------
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)

        footer.addWidget(QLabel("Publishing as"))

        self._profile_switch = QToolButton()
        self._profile_switch.setObjectName("profile_switch")
        self._profile_switch.setPopupMode(QToolButton.InstantPopup)
        self._profile_switch.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._profile_switch.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        self._profile_switch.setCursor(Qt.PointingHandCursor)
        self._switch_menu = QMenu(self._profile_switch)
        self._profile_switch.setMenu(self._switch_menu)
        footer.addWidget(self._profile_switch)
        footer.addStretch(1)

        self._status = QLabel("")
        self._status.setObjectName("article_status")
        self._status.setWordWrap(True)
        footer.addWidget(self._status, 2)

        buttons = QDialogButtonBox()
        self._cancel_btn = buttons.addButton(QDialogButtonBox.Cancel)
        self._publish_btn = buttons.addButton("Publish", QDialogButtonBox.AcceptRole)
        self._publish_btn.setDefault(True)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._publish_btn.clicked.connect(self._on_publish)
        footer.addWidget(buttons)

        root.addLayout(footer)

        self._refresh_advanced_toggle_label()

    def _apply_theme(self) -> None:
        self.setStyleSheet(_DARK_CSS if self._is_dark else _LIGHT_CSS)
        self._switch_menu.setStyleSheet(
            _DARK_MENU_CSS if self._is_dark else _LIGHT_MENU_CSS
        )

    # -- title ↔ slug coupling --------------------------------------------

    def _on_title_changed(self, text: str) -> None:
        if self._slug_is_auto:
            self._slug_edit.blockSignals(True)
            self._slug_edit.setText(slugify(text))
            self._slug_edit.blockSignals(False)
        self._refresh_publish_enabled()

    def _on_slug_edited(self, _text: str) -> None:
        # The user took manual control of the slug; stop tracking the title.
        self._slug_is_auto = False
        self._refresh_publish_enabled()

    def _on_body_changed(self) -> None:
        self._refresh_meta()
        self._refresh_publish_enabled()

    # -- advanced panel ----------------------------------------------------

    def _toggle_advanced(self) -> None:
        self._advanced_open = not self._advanced_open
        self._advanced_panel.setVisible(self._advanced_open)
        self._refresh_advanced_toggle_label()

    def _refresh_advanced_toggle_label(self) -> None:
        if self._advanced_open:
            self._advanced_toggle.setText("▾  Advanced")
        else:
            self._advanced_toggle.setText("▸  Advanced  ·  slug, cover image, hashtags")

    # -- meta strip --------------------------------------------------------

    def _refresh_meta(self) -> None:
        words = len(self._body_edit.toPlainText().split())
        if words == 0:
            self._meta_label.setText("")
            return
        minutes = max(1, round(words / _WPM_READ_SPEED))
        self._meta_label.setText(f"{words:,} words · ~{minutes} min read")

    # -- profile switcher --------------------------------------------------

    def _refresh_profile_chip(self) -> None:
        profile = self._current_profile
        avatar_pix = self._avatars.get(profile.user_pubkey)
        avatar = pixmap_for_profile(
            profile.display_name, profile.user_pubkey, avatar_pix, size=AVATAR_SIZE
        )
        self._profile_switch.setIcon(compose_chip_icon(avatar, self._chevron_color()))
        self._profile_switch.setIconSize(QSize(CHIP_TOTAL_WIDTH, AVATAR_SIZE))
        label = profile.display_name or profile.npub_short()
        self._profile_switch.setText(f"  {label}")
        self._profile_switch.setToolTip(profile.npub_short())
        self._rebuild_switch_menu()

    def _chevron_color(self) -> QColor:
        return QColor("#CCCCCC") if self._is_dark else QColor("#555555")

    def _rebuild_switch_menu(self) -> None:
        menu = self._switch_menu
        menu.clear()
        profiles = self._store.list()
        if not profiles:
            act = menu.addAction("(no profiles)")
            act.setEnabled(False)
            return
        for profile in profiles:
            label = profile.display_name or profile.npub_short()
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(profile.user_pubkey == self._current_profile.user_pubkey)
            act.triggered.connect(
                lambda _checked=False, p=profile: self._on_profile_switched(p)
            )

    def _on_profile_switched(self, profile: Profile) -> None:
        if profile.user_pubkey == self._current_profile.user_pubkey:
            return
        self._current_profile = profile
        self._store.set_default(profile.user_pubkey)
        self._refresh_profile_chip()

    # -- helpers / state ---------------------------------------------------

    def _hashtag_list(self) -> List[str]:
        return [t.strip() for t in self._tags_edit.text().split(",") if t.strip()]

    def _refresh_publish_enabled(self) -> None:
        slug = self._slug_edit.text().strip()
        body = self._body_edit.toPlainText().strip()
        # NIP-23 requires the d-tag; body without anything to say isn't useful.
        self._publish_btn.setEnabled(self._job is None and bool(slug) and bool(body))

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.setText(text)
        if error:
            color = "#FF6B6B" if self._is_dark else "#C0392B"
        else:
            color = "#FFB347" if self._is_dark else "#A05000"
        self._status.setStyleSheet(f"color: {color};")

    def _set_busy(self, busy: bool) -> None:
        for w in (
            self._title_edit,
            self._summary_edit,
            self._slug_edit,
            self._image_edit,
            self._tags_edit,
            self._body_edit,
        ):
            w.setReadOnly(busy)
        self._profile_switch.setEnabled(not busy)
        self._advanced_toggle.setEnabled(not busy)
        self._refresh_publish_enabled()
        if busy:
            self._publish_btn.setEnabled(False)

    # -- publish flow ------------------------------------------------------

    def _on_publish(self) -> None:
        slug = self._slug_edit.text().strip()
        body = self._body_edit.toPlainText().strip()
        if not slug or not body or self._job is not None:
            return

        try:
            unsigned = build_article(
                content=body,
                pubkey_hex=self._current_profile.user_pubkey,
                slug=slug,
                title=self._title_edit.text(),
                summary=self._summary_edit.text(),
                image=self._image_edit.text(),
                published_at=int(time.time()),
                hashtags=self._hashtag_list(),
                mentions=self._mention_row.mentions(),
            )
        except ValueError as exc:
            # Make sure the Advanced panel is open so the slug field is visible
            # when we flag it as the offender.
            if not self._advanced_open:
                self._toggle_advanced()
            self._set_status(f"Cannot build article: {exc}", error=True)
            return

        self._set_busy(True)
        self._set_status("")

        self._job = PublishJob(
            relay_pool=self._relay_pool,
            relay_list_cache=self._relay_list_cache,
            session_pool=self._session_pool,
            profile=self._current_profile,
            unsigned_event=unsigned,
            parent=self,
        )
        self._job.status_changed.connect(self._set_status)
        self._job.signed.connect(self._on_signed)
        self._job.completed.connect(self._on_completed)
        self._job.failed.connect(self._on_failed)
        self._job.start()

    def _on_signed(self, event_id_hex: str) -> None:
        self._signed_event_id = event_id_hex

    def _on_completed(self, results: List[PublishResult]) -> None:
        self._job = None
        accepted = sum(1 for _, ok, _ in results if ok)
        if accepted == 0:
            self._set_status(
                "No relay accepted the article. See log for details.", error=True
            )
            self._set_busy(False)
            return

        hint_relays = [url for url, ok, _ in results if ok][:2]
        naddr = encode_naddr(
            identifier=self._slug_edit.text().strip(),
            author_pubkey_hex=self._current_profile.user_pubkey,
            kind=30023,
            relays=hint_relays,
        )
        self.published.emit(naddr, results)
        self.accept()

    def _on_failed(self, reason: str) -> None:
        self._job = None
        self._set_status(f"Publish failed: {reason}", error=True)
        self._set_busy(False)

    def _on_cancel(self) -> None:
        if self._job is not None:
            self._job = None
        self.reject()
