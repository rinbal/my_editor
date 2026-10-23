# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-tab notice: "A newer version of this draft was published from
another device."

Shown above the editor when ``DraftSync`` notices that the draft
currently bound to a tab has been updated from another device (same
``d``-tag, newer ``created_at`` than the one the tab was opened with).
It is the document notice every tab has (document_notice.py), with the
draft's own actions:

  - **View**: open the newer version in a new tab without touching
    this one, to compare.
  - **Reload**: replace this tab's contents with the newer version. The
    window asks first when there are unsaved edits.
  - **Close**: keep this version; the next save from this tab wins.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from document_notice import DocumentNotice
from i18n import _


class DraftConflictBanner(DocumentNotice):
    """Signals:
      view_remote(): open the newer draft in a new tab (the notice goes).
      reload():      replace this tab's contents with the newer draft; the
                     window takes the notice away once it has.
      dismissed():   the notice is done with (View, or Close), so the
                     caller can detach it from the tab.
    """

    view_remote = Signal()
    reload = Signal()
    dismissed = Signal()

    def __init__(self, *, is_dark: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, is_dark=is_dark, nostr=True)
        self.acted.connect(self._on_acted)
        self.closed.connect(self.dismissed)
        self.present(_("A newer version of this draft was published from another device."),
                     actions=[("view", _("View")), ("reload", _("Reload"))])

    def _on_acted(self, key: str) -> None:
        if key == "view":
            self.view_remote.emit()
            self.dismissed.emit()
        else:
            self.reload.emit()
