# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consent gate: the last thing between a private file and the public.

Publishing a private picture is not a setting change, it is a new file on
a public server that anyone with the address can open, and it cannot be
unsent. So this dialog does the four things a person needs before they
can answer honestly: it says what will be created, it says what will not
change, it says what is stripped on the way, and it says what it costs.

The order matters. The explanation comes first and nothing moves until
the user answers it: ``PublicCopyMaker.ensure_public_copies`` asks for
consent before a single byte is fetched, so a refusal here leaves
nothing uploaded and nothing recorded, which is exactly what the button
promises. Only after that does the same window become a progress view,
so the user watches the thing they just agreed to rather than being
handed a second dialog they have to interpret.

Two rules shape the failure side:

  A partial set is never a success. The copies that were made are in the
  ledger and are named, because a public blob the user cannot see is one
  they cannot revoke, but the publish they asked for does not go ahead
  with a file missing. ``PublishSet.blobs`` is None and the caller stops.

  Retry is offered for failures only. The copies that succeeded are in
  the ledger, so a second run finds them there and reuses them; nothing
  is fetched twice and nothing is uploaded twice. That is a property of
  the ledger, not of this dialog remembering anything.

The dialog owns the run rather than being driven by it, which is what
lets the whole state machine be tested by calling methods: no modal
loop, no event loop, no signer, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..media.media_visibility import UNKNOWN, MediaVisibility
from ..media.publish_copy import CopyStage, PublicCopyMaker, PublishSet
from ..media.visibility import PrivateBlob, PublicBlob
# This dialog is the second half of the picker's flow, so it borrows the
# picker's stylesheet rather than growing one of its own. Two windows in
# one gesture that do not look like the same app is how a consent prompt
# starts reading like something that arrived from somewhere else.
from .media_library_dialog import _DARK_CSS, _LIGHT_CSS, _format_size


# The dialog's own states. ``EXPLAIN`` is the only one from which
# nothing has happened yet, which is what makes its Cancel honest.
EXPLAIN = "explain"
WORKING = "working"
FAILED = "failed"
CANCELLED = "cancelled"


# One line per stage, in the order a copy passes through them.
# ``progress-indicators.md``: "If it's helpful, display a description
# that provides additional context for the task. Be accurate and
# succinct. Avoid vague terms like loading or authenticating because
# they seldom add value." So these name the actual step, and the scrub
# names what it removes rather than calling itself "preparing".
WAITING = "waiting"
_STAGE_TEXT: Dict[str, str] = {
    WAITING: "Waiting",
    CopyStage.CHECKING: "Checking",
    CopyStage.FETCHING: "Downloading the original",
    CopyStage.DECRYPTING: "Decrypting",
    CopyStage.PREPARING: "Removing camera and location data",
    CopyStage.UPLOADING: "Uploading the copy",
    CopyStage.RECORDING: "Recording it as public",
    CopyStage.REUSED: "Already has a public copy",
    CopyStage.DONE: "Public copy created",
    CopyStage.FAILED: "Failed",
    CopyStage.CANCELLED: "Cancelled, nothing was uploaded",
}

# The four promises, in the order they answer "what happens to me".
# ``writing.md``: "Be clear. Choose words that are easily understood and
# convey the right thing."
_PROMISES = (
    "A separate public copy is uploaded. Anyone with its address can open it.",
    "The private original is not changed, moved or unlocked. It stays private.",
    "Camera and location details are removed from the copy before it is sent.",
    "You can revoke the copy later. It is listed with your published media.",
)

# What a pick is refused with when nobody has been able to check it. It
# names the state, the consequence and the way out, because "unknown" on
# its own reads as a bug rather than as something the user can clear.
_UNCHECKED_PICK = (
    "This picture has not been checked against your private library, so it "
    "was not used. Wait for the library to finish opening, or reconnect your "
    "signer, then try again."
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
        self.setWindowTitle("Create a public copy")
        self.setModal(True)
        self.resize(560, 460)
        self.setStyleSheet(_DARK_CSS if is_dark else _LIGHT_CSS)

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
        self._rows: Dict[str, QListWidgetItem] = {}
        self._settled = 0

        self._build_ui()
        self._apply_state()

        maker.copy_progress.connect(self._on_copy_progress)
        maker.run_progress.connect(self._on_run_progress)

    # -- what the caller reads ---------------------------------------------

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
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        self._heading = QLabel("")
        font = self._heading.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        self._heading.setFont(font)
        self._heading.setWordWrap(True)
        layout.addWidget(self._heading)

        self._promises = QLabel("\n".join(f"·  {line}" for line in _PROMISES))
        self._promises.setWordWrap(True)
        layout.addWidget(self._promises)

        self._storage = QLabel("")
        self._storage.setObjectName("media_hint")
        self._storage.setWordWrap(True)
        layout.addWidget(self._storage)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.NoSelection)
        layout.addWidget(self._list, 1)
        for blob in self._pending:
            item = QListWidgetItem(_row_text(blob, WAITING))
            self._list.addItem(item)
            self._rows[blob.sha256] = item

        # Determinate, because the number of copies is known before the
        # first one starts. progress-indicators.md: "When possible, use a
        # determinate progress indicator."
        self._progress = QProgressBar()
        self._progress.setRange(0, max(1, len(self._pending)))
        self._progress.setValue(0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setObjectName("media_status")
        self._status.setWordWrap(True)
        # A failed upload puts the server's own words in here, and QLabel
        # renders markup unless it is told not to guess.
        self._status.setTextFormat(Qt.PlainText)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        buttons.addWidget(self._cancel_btn)
        self._retry_btn = QPushButton("Try again")
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        self._retry_btn.setVisible(False)
        buttons.addWidget(self._retry_btn)
        # The action button says what it does rather than saying OK.
        # alerts.md: "A specific button title like 'Erase,' 'Convert,'
        # 'Clear,' or 'Delete' helps people understand the action they're
        # taking."
        self._create_btn = QPushButton(_create_label(len(self._pending)))
        self._create_btn.setObjectName("media_primary")
        self._create_btn.setDefault(True)
        self._create_btn.clicked.connect(self._on_create_clicked)
        buttons.addWidget(self._create_btn)
        layout.addLayout(buttons)

    # -- state -------------------------------------------------------------

    def _apply_state(self) -> None:
        count = len(self._pending)
        explaining = self._state == EXPLAIN
        working = self._state == WORKING

        if explaining:
            if not count:
                # Reachable only from a caller that did not check first;
                # ``resolve_pick`` never opens the dialog for a set that
                # is already public, because there is nothing to consent
                # to and a modal asking about nothing is noise.
                self._heading.setText("These pictures already have public copies.")
            else:
                self._heading.setText(
                    "Create a public copy of this picture?"
                    if count == 1
                    else f"Create public copies of these {count} pictures?"
                )
        elif working:
            self._heading.setText(
                "Creating the public copy…"
                if count == 1
                else f"Creating {count} public copies…"
            )

        self._promises.setVisible(explaining)
        self._storage.setVisible(explaining)
        self._storage.setText(_storage_line(self._pending))
        self._progress.setVisible(working)
        self._create_btn.setVisible(explaining)
        self._retry_btn.setVisible(self._state in (FAILED, CANCELLED))
        # There is nothing to halt before the run starts and nothing to
        # halt after it has stopped, so the one button changes meaning
        # with the state rather than sitting there doing nothing.
        self._cancel_btn.setText("Cancel" if explaining or working else "Close")

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
        for sha, item in self._rows.items():
            blob = self._blob(sha)
            if blob is not None and self._maker.existing_copy(blob) is None:
                item.setText(_row_text(blob, WAITING))
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

    def _on_copy_progress(self, sha: str, stage: str) -> None:
        item = self._rows.get(sha)
        blob = self._blob(sha)
        if item is None or blob is None:
            return
        item.setText(_row_text(blob, stage))

    def _on_run_progress(self, done: int, total: int) -> None:
        self._settled = done
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)
        # feedback.md: "Consider integrating status feedback into your
        # interface... people get important information without having to
        # take action or leave their current context."
        self._status.setText(f"{done} of {total} settled.")

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
            self._status.setText("Stopping. Nothing new will be uploaded.")
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
        for signal, slot in (
            (self._maker.copy_progress, self._on_copy_progress),
            (self._maker.run_progress, self._on_run_progress),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)

    # -- internals ---------------------------------------------------------

    def _blob(self, sha: str) -> Optional[PrivateBlob]:
        for blob in self._pending:
            if blob.sha256 == sha:
                return blob
        return None


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

    if maker is None:
        return PickResolution(
            reason="This picture is private, and this app cannot make a "
                   "public copy of it right now. Connect your signer and "
                   "try again.",
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
            reason=reason or "No public copy was created, so nothing was inserted.",
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

def _create_label(count: int) -> str:
    if not count:
        return "Use the existing copies"
    return "Create public copy" if count == 1 else f"Create {count} public copies"


def _row_text(blob: PrivateBlob, stage: str) -> str:
    """One file's line: what it is, how big, and where it has got to.

    The name comes from the private record and the hash prefix stands in
    when there is not one, so a file is identifiable either way.
    """
    name = (blob.name or "").strip() or f"{blob.sha256[:8]}…"
    return f"{name}  ·  {_format_size(blob.size)}  ·  {_STAGE_TEXT.get(stage, stage)}"


def _storage_line(pending: Sequence[PrivateBlob]) -> str:
    """What the copies cost, because they count against the same quota.

    A copy does not replace the original, it sits beside it, so the space
    the user is agreeing to spend is the space the originals already
    take, again.
    """
    total = sum(max(0, blob.size) for blob in pending)
    if not total:
        return (
            "Each copy is stored beside its original, so it uses that much "
            "space again on your media servers."
        )
    if len(pending) == 1:
        return (
            f"The copy uses about {_format_size(total)} more on your media "
            f"servers. The original keeps its own space."
        )
    return (
        f"The copies use about {_format_size(total)} more on your media "
        f"servers, on top of the originals, which keep their own space."
    )


def _cancelled_line(minted: int) -> str:
    """Cancelling says what did and did not happen, without softening it."""
    if not minted:
        return "Cancelled. Nothing was uploaded and nothing was published."
    return (
        f"Cancelled. Nothing was published, but "
        f"{minted} public cop{'y' if minted == 1 else 'ies'} had already been "
        f"created. {'It is' if minted == 1 else 'They are'} listed with your "
        f"published media and can be revoked there."
    )


def _failed_line(reason: str, minted: int) -> str:
    """A named reason, and never a claim that the set succeeded."""
    detail = (reason or "").strip() or "A public copy could not be created."
    line = f"Nothing was published. {detail}"
    if minted:
        line += (
            f" {minted} cop{'y' if minted == 1 else 'ies'} made before this "
            f"{'is' if minted == 1 else 'are'} listed with your published "
            f"media and can be revoked."
        )
    return line
