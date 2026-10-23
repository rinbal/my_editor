# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Media Servers: which servers keep the person's files, in which order.

The one place servers are managed (the Media Library's action menu opens
it). Everything else about servers, mirroring files to all of them,
routing around one that cannot take a file, leaving out a full one,
happens in the background and is never shown in the library.

The list is in the order uploads use it: new files go to the first
server, and the others keep copies. The members' server of an
association membership is listed after them, under the association's
name; it comes with the membership and cannot be removed here. The
servers the person's Nostr account lists (kind 10063) and this app does
not use yet are offered, and Share with Other Apps… publishes this
list for other Nostr apps, after the window that owns publishing asks.

Every change is saved at once and announced with ``changed``.
"""

from __future__ import annotations

import unicodedata
from typing import Callable, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QPalette
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import alerts
import url_safety
from . import appearance
from i18n import _

from ..blossom.settings import BlossomSettings


def server_address(text: str) -> Tuple[Optional[str], str]:
    """``(origin, "")`` for the media server ``text`` names, else
    ``(None, problem)``: "space", "hidden" (an invisible character, as
    text copied from a web page can hold), "host" (no full host: no dot
    other than localhost, a number that is no IP address) or "https"
    (anything but https; http only on this computer). A bare host means
    https. A host in another script is kept as its ASCII form, so the
    two ways of writing it are one server."""
    text = (text or "").strip()
    if any(ch.isspace() for ch in text):
        return None, "space"
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Cn", "Co", "Cs") for ch in text):
        return None, "hidden"
    if "://" not in text:
        text = "https://" + text
    url = QUrl(text, QUrl.ParsingMode.StrictMode)
    if url.isValid() and url.scheme() not in ("https", "http"):
        return None, "https"
    host = url.host().rstrip(".")
    if not url.isValid() or not host:
        return None, "host"
    if ":" not in host:                       # an IPv6 address is in brackets, and checked
        # As typed: Qt reads "1.2.3" as 1.2.0.3.
        try:
            typed = (urlsplit(text).hostname or "").rstrip(".").split(".")
        except ValueError:
            return None, "host"
        if all(label.isdigit() for label in typed):
            # A dotted number is an IPv4 address in full or nothing.
            if len(typed) != 4 or QHostAddress(host).isNull():
                return None, "host"
        elif typed[-1].isdigit() or "." not in host and host != "localhost":
            return None, "host"             # no name ends in a number, or lacks a dot
        url.setHost(host)
    if url.port() == {"https": 443, "http": 80}[url.scheme()]:
        url.setPort(-1)                       # the default port is the same server
    # Encoded, a host in another script is its ASCII form.
    origin = url_safety.origin_of(bytes(url.toEncoded()).decode("ascii", "replace"))
    if not origin:
        return None, "host"
    return (origin, "") if url_safety.is_safe_media_url(origin) else (None, "https")


# Scripts with no letter that passes for a Latin one, as Safari lists
# them: a label all in one of them is shown as written. Japanese writes
# three of them together.
_DISTINCT_SCRIPTS = frozenset({"ARABIC", "BOPOMOFO", "CJK", "DEVANAGARI", "GUJARATI",
                               "GURMUKHI", "HANGUL", "HEBREW", "HIRAGANA", "KATAKANA",
                               "TAMIL", "THAI"})
_JAPANESE = frozenset({"CJK", "HIRAGANA", "KATAKANA"})
# Scripts with letters that pass for Latin ones, and the domains of the
# countries that write them, where a name in them is expected.
_PASSES_FOR_LATIN = {
    "CYRILLIC": frozenset({"рф", "укр", "срб", "бг", "мкд", "қаз", "мон", "бел",
                           "ru", "su", "ua", "bg", "rs", "by", "kz", "mk", "mn"}),
    "GREEK": frozenset({"gr", "ελ"}),
    "ARMENIAN": frozenset({"am", "հայ"}),
}
# Latin letters beyond ASCII and its accented forms that are their own
# and don't pass for another (the dotless ı, a script ɡ, small capitals do).
_DISTINCT_LATIN = frozenset("ßæøœðþłđħŋ")


def _display_host(host: str) -> str:
    """``host`` as a person reads it. An IP address is shown as it is
    written. A host in another script, in whichever form it comes, is
    shown as it is written only where Qt (as a browser) trusts its
    domain and no label can pass for another, else in its ASCII form."""
    if not host or ":" in host:
        return host
    ascii_host = bytes(QUrl.toAce(host)).decode("ascii", "replace")
    if not ascii_host:
        return host.encode("ascii", "backslashreplace").decode("ascii")
    written = QUrl.fromAce(ascii_host.encode("ascii")) or ascii_host
    labels = written.split(".")
    if any(_lookalike(label, labels[-1].lower()) for label in labels):
        return ascii_host
    return written


def _lookalike(label: str, domain: str) -> bool:
    """Whether a label could pass for another. It can't when its digits
    are ASCII and its letters are all Latin with none that passes for
    another, all of one script with no such letter (Japanese's three
    count as one), or of a script with such letters under a domain of
    a country that writes it."""
    if any(unicodedata.category(ch)[0] == "N" and not ch.isascii() for ch in label):
        return True
    scripts = {_script(ch) for ch in label if ch.isalpha()}
    if scripts <= {"LATIN"}:
        return not all(ch.isascii() or ch in _DISTINCT_LATIN
                       or (ch.isalpha() and unicodedata.normalize("NFD", ch)[0].isascii())
                       for ch in label)
    if scripts <= _JAPANESE or len(scripts) == 1 and scripts <= _DISTINCT_SCRIPTS:
        return False
    return len(scripts) > 1 or domain not in _PASSES_FOR_LATIN.get(scripts.pop(), ())


def _script(ch: str) -> str:
    """The script a letter is written in, as its Unicode name begins."""
    name = unicodedata.name(ch, "")
    first = name.split(" ")[0] if name else "UNKNOWN"
    return {"KATAKANA-HIRAGANA": "KATAKANA", "IDEOGRAPHIC": "CJK"}.get(first, first)


def _origins(servers: Sequence[str]) -> list:
    """The servers a list names, as they are added when typed (a host in
    another script in its ASCII form), leaving out what is no server."""
    return [origin for origin, _problem in map(server_address, servers) if origin]


def _address_problem(problem: str) -> str:
    return {
        "space": _("A server address can’t have spaces in it."),
        "hidden": _("This address has an invisible character in it, as text copied from a "
                    "web page sometimes does. Type it instead."),
        "host": _("Enter a full server address, such as media.example.com."),
    }.get(problem) or _("Enter a server address that starts with https://, such as "
                        "https://media.example.com.")


class MediaServersDialog(QDialog):
    """The sheet. ``settings`` is the store's own, so a change here is
    what the next upload and the next listing use."""

    changed = Signal()
    share_requested = Signal()

    def __init__(
        self,
        *,
        settings: BlossomSettings,
        entitled: Sequence[str] = (),
        suggestions: Sequence[str] = (),
        server_label: Optional[Callable[[str], Optional[str]]] = None,
        is_dark: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Media Servers"))
        # A sheet: it blocks the window it belongs to, not the app.
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._settings = settings
        self._entitled = [url_safety.origin_of(s) or s for s in entitled]
        self._suggested = _origins(suggestions)
        self._label = server_label or (lambda _origin: None)
        self._is_dark = is_dark
        self.apply_theme(is_dark)
        appearance.bind(self)
        self.setMinimumWidth(440)
        self._build()
        self._refresh()

    # -- building ------------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(appearance.dialog_stylesheet(is_dark) + (
            f"QLabel#media_servers_error {{ color: {appearance.roles(is_dark).error}; }}\n"
            "QListWidget::item { padding: 4px 6px; }"))
        appearance.surface(self, is_dark)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        intro = QLabel(_("Images, videos, audio and PDFs you add are kept on these servers. "
                         "New files go to the first one, and the others keep copies."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.server_list = QListWidget()
        self.server_list.setAccessibleName(_("Media servers"))
        self.server_list.setMinimumHeight(110)
        self.server_list.currentRowChanged.connect(self._update_buttons)
        layout.addWidget(self.server_list, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.primary_button = QPushButton(_("Make Primary"))
        self.primary_button.setAutoDefault(False)
        self.primary_button.setToolTip(_("New files go to this server first."))
        self.primary_button.clicked.connect(self._make_primary)
        self.remove_button = QPushButton(_("Remove"))
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self._remove)
        row.addWidget(self.primary_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout.addLayout(row)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.address = QLineEdit()
        self.address.setAccessibleName(_("Server address"))
        self.address.setPlaceholderText("https://media.example.com")
        self.address.setClearButtonEnabled(True)
        self.address.textChanged.connect(self._address_edited)
        # Return in the field adds what is typed (or says why not) and
        # never closes the sheet; with nothing typed it is Done's.
        self.address.installEventFilter(self)
        self.add_button = QPushButton(_("Add Server"))
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(self._add)
        add_row.addWidget(self.address, 1)
        add_row.addWidget(self.add_button)
        layout.addLayout(add_row)
        self.address_error = QLabel()
        self.address_error.setObjectName("media_servers_error")
        self.address_error.setWordWrap(True)
        self.address_error.hide()
        layout.addWidget(self.address_error)

        self.suggestions = QWidget()
        offer = QHBoxLayout(self.suggestions)
        offer.setContentsMargins(0, 4, 0, 0)
        offer.setSpacing(8)
        self.suggestion_text = QLabel()
        self.suggestion_text.setWordWrap(True)
        self.suggestion_text.setTextFormat(Qt.TextFormat.PlainText)
        self.suggestion_button = QPushButton(_("Add to List"))
        self.suggestion_button.setAutoDefault(False)
        self.suggestion_button.clicked.connect(self._add_suggested)
        offer.addWidget(self.suggestion_text, 1)
        offer.addWidget(self.suggestion_button)
        layout.addWidget(self.suggestions)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        layout.addWidget(line)

        share_row = QHBoxLayout()
        share_row.setSpacing(8)
        share_text = QLabel(_("Share this list so other Nostr apps find your files."))
        share_text.setWordWrap(True)
        self.share_button = QPushButton(_("Share with Other Apps…"))
        self.share_button.setAutoDefault(False)
        self.share_button.clicked.connect(self.share_requested)
        share_row.addWidget(share_text, 1)
        share_row.addWidget(self.share_button)
        layout.addLayout(share_row)

        done_row = QHBoxLayout()
        done_row.addStretch(1)
        self.done_button = QPushButton(_("Done"))
        self.done_button.setDefault(True)
        self.done_button.clicked.connect(self.accept)
        done_row.addWidget(self.done_button)
        layout.addLayout(done_row)

    def event(self, event) -> bool:  # noqa: N802 (Qt's name)
        if event.type() == QEvent.Type.LayoutRequest and self.layout() is not None:
            # As tall as its wrapped texts need at its narrowest, which
            # Qt doesn't count for a window: the buttons never overlap
            # the list.
            self.setMinimumHeight(self.layout().minimumHeightForWidth(self.minimumWidth()))
        return super().event(event)

    # -- state ---------------------------------------------------------------

    def _name(self, origin: str) -> str:
        return self._label(origin) or _display_host(url_safety.host_of(origin)) or origin

    def _refresh(self, select: str = "") -> None:
        configured = self._settings.configured_servers
        self.server_list.clear()
        for origin in configured:
            item = QListWidgetItem(self._name(origin))
            item.setData(Qt.ItemDataRole.UserRole, origin)
            item.setToolTip(origin)
            self.server_list.addItem(item)
        for origin in self._entitled:
            if origin in configured:
                continue
            item = QListWidgetItem(_("{server}, included with your membership").format(
                server=self._name(origin)))
            item.setData(Qt.ItemDataRole.UserRole, "")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(self.palette().color(QPalette.ColorGroup.Disabled,
                                                    QPalette.ColorRole.Text))
            item.setToolTip(_("Included with your membership"))
            self.server_list.addItem(item)
        row = configured.index(select) if select in configured else 0
        self.server_list.setCurrentRow(row)
        missing = [s for s in self._suggested if s not in configured]
        self.suggestion_text.setText(_("Your Nostr account also lists {servers}.").format(
            servers=", ".join(_display_host(url_safety.host_of(s) or "") or s
                              for s in missing)))
        self.suggestions.setVisible(bool(missing))
        self._update_buttons()

    def _selected(self) -> str:
        item = self.server_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def _update_buttons(self, *_row) -> None:
        selected = self._selected()
        configured = self._settings.configured_servers
        self.remove_button.setEnabled(bool(selected) and len(configured) > 1)
        self.primary_button.setEnabled(bool(selected) and configured[:1] != [selected])

    def offer(self, suggestions: Sequence[str]) -> None:
        """The account's published list changed while the sheet is open."""
        self._suggested = _origins(suggestions)
        self._refresh(select=self._selected())

    # -- actions -------------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.address and event.type() == QEvent.Type.KeyPress \
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and self.address.text().strip():
            self._add()
            return True
        if watched is self.address and event.type() in (QEvent.Type.FocusIn,
                                                         QEvent.Type.FocusOut):
            self._address_edited()
        return super().eventFilter(watched, event)

    def _address_edited(self, *_text) -> None:
        """Add Server is the default while an address is being typed, as
        in System Settings' add fields; Done otherwise."""
        self.address_error.hide()
        typing = self.address.hasFocus() and bool(self.address.text().strip())
        self.add_button.setDefault(typing)
        self.done_button.setDefault(not typing)

    def _add(self) -> None:
        origin, problem = server_address(self.address.text())
        before = self._settings.configured_servers
        if origin is None:
            self.address_error.setText(_address_problem(problem))
            self.address_error.show()
            return
        if origin in before:
            self.address.clear()
            self._refresh(select=origin)
            return
        after = self._settings.add_server(origin)
        if after == before:
            self.address_error.setText(_address_problem("https"))
            self.address_error.show()
            return
        self.address.clear()
        self._refresh(select=after[-1])
        self.changed.emit()

    def _remove(self) -> None:
        origin = self._selected()
        if not origin:
            return
        if not alerts.confirm_destructive(
                self, title=_("Remove “{server}”?").format(server=self._name(origin)),
                message=_("Files already there stay there. The Media Library shows them "
                          "only while another of your servers has them too."),
                action=_("Remove"), is_dark=self._is_dark, nostr=True):
            return
        self._settings.remove_server(origin)
        self._refresh()
        self.changed.emit()

    def _make_primary(self) -> None:
        origin = self._selected()
        if not origin:
            return
        self._settings.make_primary(origin)
        self._refresh(select=origin)
        self.changed.emit()

    def _add_suggested(self) -> None:
        for server in self._suggested:
            self._settings.add_server(server)
        self._refresh()
        self.changed.emit()
