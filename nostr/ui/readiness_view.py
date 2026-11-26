# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Publication Check on screen; nostr/readiness has the verdict.

    ✓ Ready for STANDUP · 2 suggestions ▸       the Publish sheet (compact)

    ! 1 thing to fix
      Add a title before publishing.     Show

The Publish sheet shows it only when there is something to say, its
suggestions folded into one line. Article Details shows it in full, first:
the headline, what to fix, the suggestions, and the groups found fine behind
"Checked". The page shows the headline as one quiet line under the summary
(article_header). Every view gets the same Result from the window, which
checks the article as the publisher would build it (check_article).

Which verdict: Ready for STANDUP for a text handed in to a publication on
STANDUP, else Ready to Publish. The writer never picks one. Publish as
Article sends to the writer's relays, never to STANDUP, so what only STANDUP
refuses (its size) is said, as a suggestion, and never keeps it from going
out.

check_article lives here, not in the Qt-free core: it builds the article as
the publisher does (nostr.publisher, a QObject module).
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Iterable, List, Mapping, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from i18n import _
from nostr.publisher import build_article, is_publishable_cover, slugify, without_secret_keys
from nostr.readiness import (
    BLOCKER, NOSTR, RULES, STANDUP, SUGGESTION, Finding, Result, evaluate, finding_sentence,
    group_label, headline, sentence, suggestions_line,
)
from picture_names import description_of

from . import appearance
from .imports_glyphs import glyph
from .post_list import symbol_pixmap

ICON_SIZE = 16
# Who signs is unknown while checking; the key only decides which "p" tag
# is the writer's own, which nothing here reads.
_ANY_KEY = "0" * 64


def outcome_for(handed_in_to_standup: bool) -> str:
    return STANDUP if handed_in_to_standup else NOSTR


# What only STANDUP refuses: Publish as Article, which never sends to STANDUP,
# says it as a suggestion.
_ONLY_STANDUP = frozenset(rule["code"] for rule in RULES["findings"]
                          if rule["severity"].get(STANDUP) == BLOCKER
                          and rule["severity"].get(NOSTR) != BLOCKER)


def check_article(*, body: str, title: str, summary: str = "", image: str = "",
                  hashtags: Iterable[str] = (), link_name: Optional[str] = None,
                  published_at: Optional[int] = None, media: Sequence = (),
                  extra_tags: Optional[List[List[str]]] = None, pubkey: str = "",
                  outcome: str = NOSTR, uploading: bool = False,
                  descriptions: Optional[Mapping[str, str]] = None,
                  replace_keys: bool = False) -> Result:
    """The verdict on ``body`` and its details as the publisher builds them
    (build_article), with what it needs on top: the link name as it is,
    empty too (None: made of the title at Publish, so an empty title is one
    finding, not two); the cover as typed, as the publisher drops one it
    can't publish without a word (is_publishable_cover); each picture's
    description by the "No ALT" rule, from ``media`` and ``descriptions``
    (address: description, for pictures still on this computer);
    ``replace_keys``, the secret keys as Replace with Public Key sends them."""
    link_name = None if link_name is None else link_name.strip()
    made = (slugify(title) or "-") if link_name is None else ""
    event = build_article(content=body, pubkey_hex=pubkey or _ANY_KEY,
                          slug=made or link_name or "-", title=title, summary=summary,
                          published_at=published_at, hashtags=list(hashtags), media=media,
                          extra_tags=extra_tags)
    tags = [["d", made or link_name]] + [tag for tag in event["tags"] if tag[0] != "d"]
    if image.strip():
        tags.append(["image", image.strip()])
    event = dict(event, tags=tags)
    if replace_keys:
        event = without_secret_keys(event) or event
    described = {item.url: item.alt for item in media if getattr(item, "url", "")}
    described.update(descriptions or {})
    result = evaluate(event, outcome=outcome, now=int(time.time()), uploading=uploading,
                      cover_ok=is_publishable_cover,
                      describe=lambda alt, address: (described[address] if address in described
                                                     else description_of(alt, address)))
    only_standup = [f for f in result.blockers if f.code in _ONLY_STANDUP]
    if not only_standup:
        return result
    # Ready to Publish then, never "Ready for STANDUP" beside "too long for STANDUP".
    return Result(NOSTR, tuple(replace(f, severity=SUGGESTION) if f in only_standup else f
                               for f in result.findings), result.checked)


def headline_line(result: Result) -> str:
    """The headline and, when there are any, how many suggestions:
    "Ready for STANDUP · 2 suggestions"."""
    more = suggestions_line(result)
    return f"{headline(result)} · {more}" if more else headline(result)


def verdict_symbol(ready: bool, dark: bool, dpr: float = 2.0, size: int = ICON_SIZE):
    """A check in the accent colour when nothing blocks, else an alert."""
    r = appearance.roles(dark)
    if ready:
        return glyph("check", size, QColor(r.ink), dpr or 2.0)
    return symbol_pixmap("alert", size, QColor(r.error), dpr or 2.0)


class ReadinessView(QWidget):
    """A Result: compact (the Publish sheet) or in full (Article Details).
    ``action(finding)`` names the button a finding's row offers ("Show",
    or None for none), ``action_requested(finding)`` says it was pressed;
    ``size_changed`` says it now needs more or less room (rows came or
    went, the suggestions folded), for a sheet that fits what it shows."""

    action_requested = Signal(object)
    size_changed = Signal()

    def __init__(self, *, compact: bool, is_dark: bool,
                 action: Callable[[Finding], Optional[str]] = lambda finding: _("Show"),
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("readiness")
        self._compact = compact
        self._dark = is_dark
        self._action = action
        self._result: Optional[Result] = None

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._icon = QLabel()
        self._icon.setFixedSize(ICON_SIZE, ICON_SIZE)
        self._headline = QLabel()
        self._headline.setObjectName("readiness_headline")
        self._headline.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(self._icon)
        head.addWidget(self._headline)
        head.addSpacing(6)
        # Compact: "▸ 2 suggestions" folds them in and out.
        self._more = self._disclosure()
        self._more.setAccessibleName(_("Suggestions"))
        head.addWidget(self._more)
        head.addStretch(1)
        column.addLayout(head)

        self._blockers = QVBoxLayout()
        self._blockers.setSpacing(4)
        column.addLayout(self._blockers)
        self._suggestions_title = QLabel()
        self._suggestions_title.setObjectName("readiness_hint")
        column.addWidget(self._suggestions_title)
        self._suggestions = QWidget()
        rows = QVBoxLayout(self._suggestions)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(4)
        column.addWidget(self._suggestions)

        # In full: the groups found fine, folded.
        self._checked_toggle = self._disclosure()
        self._checked_toggle.setAccessibleName(sentence("checked"))
        column.addWidget(self._checked_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self._checked = QLabel()
        self._checked.setObjectName("readiness_hint")
        self._checked.setWordWrap(True)
        column.addWidget(self._checked)
        self.set_dark(is_dark)
        self.show_result(None)

    def _disclosure(self) -> QToolButton:
        """A disclosure as every sheet has them (appearance.disclosure_stylesheet)."""
        toggle = QToolButton()
        toggle.setObjectName("disclosure")
        toggle.setCheckable(True)
        toggle.setAutoRaise(True)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.toggled.connect(self._fold)
        return toggle

    # -- what it shows --------------------------------------------------------

    def show_result(self, result: Optional[Result]) -> bool:
        """Show ``result``; None or, compact, nothing to say: hidden.
        Whether anything changed (its controls are made anew then)."""
        if result == self._result and result is not None:
            return False                            # nothing new: focus stays where it is
        self._result = result
        quiet = result is None or (self._compact and not result.findings)
        self.setVisible(not quiet)
        if quiet:
            self.size_changed.emit()
            return True
        self._icon.setPixmap(self._symbol(result.ready))
        self._headline.setText(headline(result))
        self._headline.setAccessibleName(headline_line(result))
        self._fill(self._blockers, result.blockers, muted=False)
        self._fill(self._suggestions.layout(), result.suggestions, muted=True)
        count = suggestions_line(result)
        self._more.setText(count)
        self._more.setAccessibleName(count or _("Suggestions"))
        self._more.setVisible(self._compact and bool(count))
        self._suggestions_title.setText(count)
        self._suggestions_title.setVisible(not self._compact and bool(count))
        groups = [group_label(group) for group in result.checked]
        self._checked_toggle.setText(f"{sentence('checked')} ({len(groups)})")
        self._checked_toggle.setVisible(not self._compact and bool(groups))
        self._checked.setText(", ".join(groups))
        # Tab goes as the eye does: the rows made now come after the
        # disclosure above them, not at the end of the window.
        chain = self.focus_chain()
        for before, after in zip(chain, chain[1:]):
            QWidget.setTabOrder(before, after)
        self._fold()
        return True

    def focus_chain(self) -> List[QWidget]:
        """Its controls in the order they are seen, for Tab."""
        shows = [row.findChild(QPushButton) for layout in (self._blockers, self._suggestions.layout())
                 for row in (layout.itemAt(i).widget() for i in range(layout.count()))]
        return [self._more, *(show for show in shows if show is not None), self._checked_toggle]

    def result(self) -> Optional[Result]:
        return self._result

    def set_dark(self, is_dark: bool) -> None:
        self._dark = is_dark
        r = appearance.roles(is_dark)
        self.setStyleSheet(
            f"QLabel#readiness_headline {{ font-weight: 600; color: {r.text}; }}"
            f"QLabel#readiness_hint {{ color: {r.muted}; }}"
            f"QToolButton#disclosure {{ color: {r.muted}; }}"
            + appearance.disclosure_stylesheet(r) +
            f"QPushButton#readiness_show {{ color: {r.ink}; background: transparent; border: none;"
            f" padding: 0 2px; }}"
            f"QPushButton#readiness_show:hover {{ text-decoration: underline; }}"
            f"QPushButton#readiness_show:focus {{ outline: 1px solid {r.focus}; }}")
        if self._result is not None:
            self._icon.setPixmap(self._symbol(self._result.ready))

    # -- inside ---------------------------------------------------------------

    def _symbol(self, ready: bool):
        return verdict_symbol(ready, self._dark, self.devicePixelRatioF())

    def _fill(self, layout, findings: Sequence[Finding], *, muted: bool) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()                # out of sight and of Tab at once
                item.widget().deleteLater()
        for finding in findings:
            layout.addWidget(self._row(finding, muted))

    def _row(self, finding: Finding, muted: bool) -> QWidget:
        """A finding's sentence, with its action at the end. A suggestion
        is quieter than what keeps the article from going out."""
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(ICON_SIZE + 6, 0, 0, 0)
        line.setSpacing(12)
        words = QLabel(finding_sentence(finding))
        words.setWordWrap(True)
        words.setTextFormat(Qt.TextFormat.PlainText)
        if muted:
            words.setObjectName("readiness_hint")
        words.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        line.addWidget(words, 1)
        label = self._action(finding)
        if label:
            act = QPushButton(label)
            act.setObjectName("readiness_show")
            act.setAutoDefault(False)
            act.setCursor(Qt.CursorShape.PointingHandCursor)
            act.setAccessibleDescription(words.text())
            act.clicked.connect(lambda _checked=False, f=finding: self.action_requested.emit(f))
            line.addWidget(act, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _fold(self, *_args) -> None:
        """Compact: the suggestions as the toggle says. In full: always
        shown, the checked groups as their toggle says."""
        for toggle in (self._more, self._checked_toggle):
            toggle.setArrowType(Qt.ArrowType.DownArrow if toggle.isChecked()
                                else Qt.ArrowType.RightArrow)
        self._suggestions.setVisible(not self._compact or self._more.isChecked())
        self._checked.setVisible(not self._compact and self._checked_toggle.isChecked())
        self.size_changed.emit()
