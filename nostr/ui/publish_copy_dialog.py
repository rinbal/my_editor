# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The question before a private image is shared, and the sharing itself.

Sharing a private image is not a setting change: it makes a new file on
a public server that anyone with the link can open, and it cannot be
unsent. So it is asked, in an alert's words: what happens (anyone with
the link will see it) and what is done on the way (camera and location
details are removed). The mechanics, a second file beside the private
original, decrypted and re-encoded here, stay in the background.

The order matters. The question comes first and nothing moves until the
person answers it: ``PublicCopyMaker.ensure_public_copies`` asks for
consent before a single byte is fetched, so a refusal here leaves
nothing uploaded and nothing recorded, which is exactly what the button
promises. Only after that does the same window show the sharing going,
with one bar, so the person watches what they just agreed to.

Two rules shape the failure side:

  A partial set is never a success. The copies that were made are in the
  ledger, and the message says Stop Sharing in the Media Library removes
  them, but the insert or publish asked for does not go ahead with a
  file missing. ``PublishSet.blobs`` is None and the caller stops.

  Try Again runs the set again, and only the failures cost anything: the
  copies that succeeded are in the ledger, so the maker reuses them;
  nothing is fetched or uploaded twice.

The dialog owns the run rather than being driven by it, which is what
lets the whole state machine be tested by calling methods: no modal
loop, no event loop, no signer, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import appearance
from i18n import _, ngettext

from ..media.media_visibility import UNKNOWN, MediaVisibility
from ..media.publish_copy import PublicCopyMaker, PublishSet
from ..media.scrub import can_scrub
from ..media.visibility import PrivateBlob, PublicBlob
import theme

# The dialog's own states. ``EXPLAIN`` is the only one from which
# nothing has happened yet, which is what makes its Cancel honest.
EXPLAIN = "explain"
WORKING = "working"
FAILED = "failed"
CANCELLED = "cancelled"


# What a pick is refused with when nobody has been able to check whether
# it is private. Said as what to do, because "unknown" on its own reads
# as a bug rather than as something the person can clear.
_UNCHECKED_PICK = _(
    "MyEditor couldn’t check yet whether this image is private. Try again "
    "in a moment, or check your signer app."
)


@dataclass(frozen=True)
class PickResolution:
    """What a picked file resolves to once visibility has been settled.

    ``ok`` false with ``cancelled`` true is the user's answer and needs
    no message; ``ok`` false without it is a fault and ``reason`` says
    which. A caller must not embed anything unless ``ok``.
    """

    sha256: str = ""
    url: str = ""
    mime: str = ""
    size: int = 0
    ok: bool = False
    cancelled: bool = False
    reason: str = ""


class PublishCopyDialog(QDialog):
    """Ask before minting public copies, then show them being made."""

    def __init__(
        self,
        *,
        blobs: Sequence[PrivateBlob],
        maker: PublicCopyMaker,
        is_dark: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Share"))
        self.setModal(True)
        self.setMinimumWidth(380)
        self.apply_theme(is_dark)
        appearance.bind(self)

        self._maker = maker
        self._blobs = list(blobs)
        # What actually needs copying. Asked of the maker rather than
        # assumed, because a file that already has a live public copy
        # costs nothing to publish and must not be presented as if it
        # did. Deduped: the same picture used twice is one copy.
        self._pending: List[PrivateBlob] = []
        seen: set = set()
        for blob in self._blobs:
            if blob.sha256 in seen or maker.existing_copy(blob) is not None:
                continue
            seen.add(blob.sha256)
            self._pending.append(blob)
        # The set the user is being asked about. Consent is checked
        # against this exact set when the run starts, so a library that
        # changed underneath cannot widen what was agreed to.
        self._agreed = {blob.sha256 for blob in self._pending}

        self._state = EXPLAIN
        self._publish_set: Optional[PublishSet] = None
        self._settled = 0

        self._build_ui()
        self._apply_state()

        maker.run_progress.connect(self._on_run_progress)

    # -- what the caller reads ---------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(appearance.dialog_stylesheet(is_dark) + (
            "QLabel#copy_heading { font-size: 14px; font-weight: 600; }\n"
            f"QLabel#copy_status {{ color: {theme.attention_color(is_dark)}; }}\n"))
        appearance.surface(self, is_dark)

    @property
    def publish_set(self) -> Optional[PublishSet]:
        """The finished answer, or None while the dialog is still open.

        Never carries a partial set: it is either every file rewritten to
        its public copy, or it is not ok.
        """
        return self._publish_set

    @property
    def pending(self) -> List[PrivateBlob]:
        """The files that would be copied, in the order they were given."""
        return list(self._pending)

    @property
    def state(self) -> str:
        return self._state

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        self._heading = QLabel("")
        self._heading.setObjectName("copy_heading")
        self._heading.setWordWrap(True)
        self._heading.setTextFormat(Qt.PlainText)
        layout.addWidget(self._heading)

        count = len(self._pending)
        self._message = QLabel(ngettext(
            "Anyone with the link will be able to see it. Camera and location "
            "details are removed first.",
            "Anyone with the link will be able to see them. Camera and location "
            "details are removed first.",
            count,
        ))
        self._message.setWordWrap(True)
        layout.addWidget(self._message)

        # Determinate, because the number of images is known before the
        # first one starts. progress-indicators.md: "When possible, use a
        # determinate progress indicator."
        self._progress = QProgressBar()
        self._progress.setRange(0, max(1, count))
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setAccessibleName(_("Sharing"))
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setObjectName("copy_status")
        self._status.setWordWrap(True)
        # A failed upload puts the server's own words in here, and QLabel
        # renders markup unless it is told not to guess.
        self._status.setTextFormat(Qt.PlainText)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self._cancel_btn = QPushButton(_("Cancel"))
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        buttons.addWidget(self._cancel_btn)
        self._retry_btn = QPushButton(_("Try Again"))
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        self._retry_btn.setVisible(False)
        buttons.addWidget(self._retry_btn)
        # The action button says what it does rather than saying OK.
        self._create_btn = QPushButton(_("Share"))
        self._create_btn.setDefault(True)
        self._create_btn.clicked.connect(self._on_create_clicked)
        buttons.addWidget(self._create_btn)
        layout.addLayout(buttons)

    # -- state -------------------------------------------------------------

    def _apply_state(self) -> None:
        count = len(self._pending)
        explaining = self._state == EXPLAIN
        working = self._state == WORKING
        name = _name_of(self._pending)

        if explaining and not count:
            # Reachable only from a caller that did not check first;
            # ``resolve_pick`` never opens the dialog for a set that is
            # shared already, because there is nothing to ask.
            self._heading.setText(_("These images are shared already."))
        elif explaining:
            self._heading.setText(
                _("Share this image?") if count == 1 and not name else
                ngettext("Share “{name}”?", "Share {count} images?",
                         count).format(name=name, count=count))
        elif working:
            self._heading.setText(
                _("Sharing this image…") if count == 1 and not name else
                ngettext("Sharing “{name}”…", "Sharing {count} images…",
                         count).format(name=name, count=count))

        self._message.setVisible(explaining and bool(count))
        self._create_btn.setText(_("Share") if count else _("Continue"))
        self._progress.setVisible(working)
        self._create_btn.setVisible(explaining)
        self._retry_btn.setVisible(self._state in (FAILED, CANCELLED))
        # There is nothing to halt before the run starts and nothing to
        # halt after it has stopped, so the one button changes meaning
        # with the state rather than sitting there doing nothing.
        self._cancel_btn.setText(_("Cancel") if explaining or working else _("Close"))

    # -- the run -----------------------------------------------------------

    def _on_create_clicked(self) -> None:
        if self._state != EXPLAIN:
            return
        # A set with nothing pending is allowed through rather than
        # refused: the run settles at once with the copies that already
        # exist, and leaving the button inert would strand the dialog.
        self._start_run()

    def _on_retry_clicked(self) -> None:
        """Run again over the same set.

        Only the failures cost anything: every copy that succeeded is in
        the ledger, and the maker reuses a listed copy without fetching
        or uploading it. So this is a retry of the failures even though
        it is written as a re-run of the set.
        """
        if self._state not in (FAILED, CANCELLED):
            return
        self._start_run()

    def _start_run(self) -> None:
        self._publish_set = None
        self._settled = 0
        self._state = WORKING
        self._status.setText("")
        self._progress.setRange(0, max(1, len(self._pending)))
        self._progress.setValue(0)
        self._apply_state()
        self._maker.ensure_public_copies(
            self._blobs, consent=self._confirm_scope, on_done=self._on_run_done,
        )

    def _confirm_scope(self, needed: Sequence[PrivateBlob]) -> bool:
        """Agree only to the files that were actually shown.

        The user answered a question about a named set. If the maker has
        since decided it needs a file that was not in it, for instance
        because a copy was revoked between the question and the answer,
        that is a different question and it has not been asked. Refusing
        costs a second dialog; agreeing would publish a picture nobody
        consented to.
        """
        return all(blob.sha256 in self._agreed for blob in needed)

    def _on_run_progress(self, done: int, total: int) -> None:
        self._settled = done
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)

    def _on_run_done(self, outcome: PublishSet) -> None:
        self._publish_set = outcome
        if outcome.ok:
            # Every file has a listed public copy. This is the only path
            # that reports success, so a partial set cannot reach it.
            self.accept()
            return

        minted = len(outcome.minted)
        if outcome.cancelled:
            self._state = CANCELLED
            self._status.setText(_cancelled_line(minted))
        else:
            self._state = FAILED
            reason = outcome.failures[0].reason if outcome.failures else ""
            self._status.setText(_failed_line(reason, minted))
        self._apply_state()

    # -- cancelling --------------------------------------------------------

    def _on_cancel_clicked(self) -> None:
        self.reject()

    def reject(self) -> None:
        """Cancel, and mean it.

        Before the run there is nothing to undo, so this closes at once.
        During the run it asks the maker to stop and waits: a copy that
        is mid-upload is carried through to its ledger entry, because
        dropping it is precisely how a blob ends up public with nothing
        pointing at it. The dialog closes when the run answers.
        """
        if self._state == WORKING:
            self._status.setText(_("Stopping…"))
            self._maker.cancel()
            return
        super().reject()

    def closeEvent(self, event) -> None:
        """The window button is the Cancel button, including mid-run."""
        if self._state == WORKING:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # The maker outlives this dialog, so its signals must not keep
        # arriving at a widget Qt is about to delete.
        try:
            self._maker.run_progress.disconnect(self._on_run_progress)
        except (RuntimeError, TypeError):
            pass
        super().done(result)


# --------------------------------------------------------------------------- #
# The one entry point the pickers use                                         #
# --------------------------------------------------------------------------- #

def resolve_pick(
    media: object,
    *,
    visibility: MediaVisibility,
    maker: Optional[PublicCopyMaker],
    is_dark: bool = True,
    parent: Optional[QWidget] = None,
) -> PickResolution:
    """Turn a picked library file into something safe to publish.

    A public file passes straight through, unchanged and unasked about:
    there is nothing to warn anyone of. A private one goes through the
    gate above, and what comes back is the public copy's address, never
    the original's.

    A file nobody has been able to check is refused outright, and this
    is the only branch where refusing costs the user something they may
    not deserve to lose. It is still the right way round: the cost of
    being wrong here is one message and a retry, and the cost of being
    wrong the other way is a private file addressed in a signed event
    that cannot be unsent.

    With no copy maker available this refuses rather than falling back.
    The fallback would be to embed the private file's own address, which
    is a link to ciphertext that no reader can open and that names a
    file the user did not agree to point at.
    """
    sha = str(getattr(media, "hash", "") or "")
    if visibility.state_of(sha) == UNKNOWN:
        return PickResolution(reason=_UNCHECKED_PICK)

    private = visibility.private_blobs([sha])
    if not private:
        return PickResolution(
            sha256=sha,
            url=str(getattr(media, "url", "") or ""),
            mime=str(getattr(media, "mime_type", "") or ""),
            size=int(getattr(media, "size", 0) or 0),
            ok=True,
        )

    if not all(can_scrub(blob.mime) for blob in private):
        # The question promises the camera and location details are
        # removed; for a kind this app cannot clean, that would be untrue.
        return PickResolution(
            reason=_("MyEditor can’t share this kind of private file yet, because it "
                     "can’t remove its camera and location details."),
        )

    if maker is None:
        return PickResolution(
            reason=_("This image is private and can’t be shared right now. "
                     "Connect your signer app and try again."),
        )

    if all(maker.existing_copy(blob) is not None for blob in private):
        # Every one of them is already public, so nothing new is uploaded
        # and there is nothing to consent to. alerts.md: "Avoid using
        # alerts sparingly... Avoid displaying alerts for common,
        # undoable actions." The picker already said the copy exists.
        settled: dict = {}
        maker.ensure_public_copies(
            private,
            # Unreachable while every copy is listed, and a refusal if
            # the ledger disagrees, which is the safe way to be wrong.
            consent=lambda _needed: False,
            on_done=lambda outcome: settled.update(outcome=outcome),
        )
        return resolution_from(settled.get("outcome"))

    dialog = PublishCopyDialog(
        blobs=private, maker=maker, is_dark=is_dark, parent=parent,
    )
    dialog.exec()
    return resolution_from(dialog.publish_set)


def resolution_from(outcome: Optional[PublishSet]) -> PickResolution:
    """Read one finished :class:`PublishSet` as a pick answer.

    Split out from the dialog so the mapping can be tested without one,
    and so the "no blobs means do not publish" rule is written once.
    """
    if outcome is None:
        return PickResolution(cancelled=True)
    if not outcome.ok or not outcome.blobs:
        if outcome.cancelled:
            return PickResolution(cancelled=True)
        reason = outcome.failures[0].reason if outcome.failures else ""
        return PickResolution(
            reason=reason or _("The image wasn’t shared, so nothing was inserted."),
        )
    public: PublicBlob = outcome.blobs[0]
    return PickResolution(
        sha256=public.sha256,
        url=public.url,
        mime=public.mime,
        size=public.size,
        ok=True,
    )


# --------------------------------------------------------------------------- #
# Text                                                                        #
# --------------------------------------------------------------------------- #

def _name_of(pending: Sequence[PrivateBlob]) -> str:
    """The name of the first image, from its private record, or ""."""
    return (pending[0].name or "").strip() if pending else ""


def _cancelled_line(minted: int) -> str:
    """Cancelling says what did and did not happen, without softening it."""
    if not minted:
        return _("Cancelled. Nothing was shared.")
    return ngettext(
        "Cancelled. {count} image was shared before that; Stop Sharing in the "
        "Media Library undoes it.",
        "Cancelled. {count} images were shared before that; Stop Sharing in the "
        "Media Library undoes it.",
        minted,
    ).format(count=minted)


def _failed_line(reason: str, minted: int) -> str:
    """A named reason, and never a claim that the set succeeded."""
    detail = (reason or "").strip() or _("This image couldn’t be shared.")
    if not minted:
        return detail
    return ngettext(
        "{reason} {count} other image was shared already; Stop Sharing in the "
        "Media Library undoes it.",
        "{reason} {count} other images were shared already; Stop Sharing in the "
        "Media Library undoes it.",
        minted,
    ).format(reason=detail, count=minted)
