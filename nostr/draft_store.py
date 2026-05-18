"""In-memory cache for the currently visible profile's NIP-37 drafts.

The drafts panel binds to a single ``DraftStore`` instance. The store
holds one ``DraftRecord`` per ``d``-tag and emits granular Qt signals
so the panel can update individual rows without rebuilding the whole
list when a decryption finishes or a tombstone arrives.

The store is **profile-scoped**: switching profiles calls ``reset()``
which clears all records. We never aggregate across identities — that
would require labelling every row with an identity badge and risks
confusing "which key signs the next publish" decisions.

The store carries no network or crypto logic. ``DraftSync`` (see
``draft_sync.py``) drives the inbound side; the publisher writes back
via ``upsert_from_inner`` after a successful stash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional

from PySide6.QtCore import QObject, Signal

from .drafts import (
    DraftWrapMeta,
    INNER_KIND_LONG_FORM,
    INNER_KIND_SHORT_NOTE,
    derive_preview_snippet,
    extract_article_metadata,
)


class DraftState(Enum):
    """Lifecycle of one draft in the store.

    LOADING — wrap has arrived, decryption hasn't returned yet.
    READY   — decrypted; title/snippet/content are populated.
    FAILED  — decryption failed (key mismatch, malformed payload, etc.).
              We keep the row so the user can retry rather than seeing
              a phantom "12 drafts but only 7 shown" mismatch.
    """

    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass
class DraftRecord:
    """One draft as the panel sees it.

    The pair ``(profile_pubkey, identifier)`` is the addressable key.
    All metadata not coming from the encrypted payload (event_id,
    created_at, expiration) is filled in from the outer wrap. Title/
    snippet/content only become available after decryption succeeds.
    """

    identifier: str                       # d-tag value
    inner_kind: int                       # 1 (note) or 30023 (article); 0 == unknown
    state: DraftState = DraftState.LOADING
    title: str = ""                       # NIP-23 title tag (articles) or first heading (notes)
    snippet: str = ""                     # short preview for the list row
    # Full decrypted body. Lives in this process only — drafts are
    # re-decrypted through the bunker on every fresh session so we
    # never persist plaintext to disk. Wiped on profile switch.
    content: str = ""
    inner_tags: List[List[str]] = field(default_factory=list)
    event_id: str = ""                    # outer wrap event id
    created_at: int = 0                   # outer wrap created_at (== last stash time)
    expiration: Optional[int] = None      # NIP-40 expiration unix, if any
    failure_reason: str = ""              # populated when state == FAILED

    @property
    def is_article(self) -> bool:
        return self.inner_kind == INNER_KIND_LONG_FORM

    @property
    def is_note(self) -> bool:
        return self.inner_kind == INNER_KIND_SHORT_NOTE


# --------------------------------------------------------------------------- #
# Store                                                                       #
# --------------------------------------------------------------------------- #

class DraftStore(QObject):
    """Process-wide draft cache for the *active* Nostr profile.

    Signals:
      record_added(str d)        — a new row appeared.
      record_changed(str d)      — an existing row was updated in place.
      record_removed(str d)      — a row was removed (tombstone or reset).
      cleared()                  — all rows removed (profile switch).
      loading_state_changed(bool) — overall "is a refresh in flight" flag.
    """

    record_added = Signal(str)
    record_changed = Signal(str)
    record_removed = Signal(str)
    cleared = Signal()
    loading_state_changed = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._profile_pubkey: Optional[str] = None
        self._records: Dict[str, DraftRecord] = {}
        self._is_loading: bool = False

    # -- read --------------------------------------------------------------

    @property
    def profile_pubkey(self) -> Optional[str]:
        return self._profile_pubkey

    @property
    def is_loading(self) -> bool:
        return self._is_loading

    def __iter__(self) -> Iterator[DraftRecord]:
        return iter(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, identifier: str) -> bool:
        return identifier in self._records

    def get(self, identifier: str) -> Optional[DraftRecord]:
        return self._records.get(identifier)

    def all(self) -> List[DraftRecord]:
        """Return records ordered by most recent stash first."""
        return sorted(
            self._records.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )

    # -- profile lifecycle -------------------------------------------------

    def bind_profile(self, pubkey_hex: Optional[str]) -> None:
        """Switch the store to a different profile.

        Always wipes existing records — drafts are per-identity and must
        not bleed across profile switches. If ``pubkey_hex`` matches the
        current binding this is a no-op so the caller can be lazy.
        """
        new_pubkey = pubkey_hex.lower() if pubkey_hex else None
        if new_pubkey == self._profile_pubkey:
            return
        self._profile_pubkey = new_pubkey
        self.reset()

    def reset(self) -> None:
        """Clear all records and emit ``cleared``."""
        if not self._records and not self._is_loading:
            # Still emit cleared so panels in an inconsistent state can
            # reconcile cheaply.
            self.cleared.emit()
            return
        self._records.clear()
        if self._is_loading:
            self._is_loading = False
            self.loading_state_changed.emit(False)
        self.cleared.emit()

    def set_loading(self, loading: bool) -> None:
        if loading == self._is_loading:
            return
        self._is_loading = loading
        self.loading_state_changed.emit(loading)

    # -- mutation: inbound from DraftSync ---------------------------------

    def upsert_skeleton(self, meta: DraftWrapMeta) -> None:
        """Insert/update from a wrap that hasn't been decrypted yet.

        Tombstones (empty ciphertext) are handled here too — they remove
        any existing record for that ``d`` and emit ``record_removed``.
        """
        if meta.is_tombstone:
            self.remove(meta.identifier)
            return

        existing = self._records.get(meta.identifier)
        if existing is None:
            record = DraftRecord(
                identifier=meta.identifier,
                inner_kind=meta.inner_kind,
                state=DraftState.LOADING,
                event_id=meta.event_id,
                created_at=meta.created_at,
                expiration=meta.expiration,
            )
            self._records[meta.identifier] = record
            self.record_added.emit(meta.identifier)
            return

        # Already known. If the wrap is newer than the cached row's
        # backing event, transition back to LOADING so the row picks up
        # the new payload after decryption; otherwise leave it alone.
        if meta.created_at <= existing.created_at:
            return
        existing.event_id = meta.event_id
        existing.created_at = meta.created_at
        existing.expiration = meta.expiration
        existing.inner_kind = meta.inner_kind
        existing.state = DraftState.LOADING
        existing.failure_reason = ""
        self.record_changed.emit(meta.identifier)

    def set_decrypted(
        self,
        identifier: str,
        *,
        inner: Dict[str, Any],
        source_event_id: Optional[str] = None,
    ) -> None:
        """Fill in the title/snippet/content for a loading row.

        ``inner`` is the parsed inner event dict (see ``drafts.parse_inner_event``).
        If no record exists for ``identifier`` we ignore — the wrap may
        have been replaced by a newer one while decryption was in flight.

        ``source_event_id`` is the id of the outer wrap whose ciphertext
        produced this plaintext. When provided, we reject the update if
        a newer wrap (with a different event id) has since arrived,
        preventing a stale decryption from briefly overwriting fresh
        content during rapid re-stashing.
        """
        record = self._records.get(identifier)
        if record is None:
            return
        if source_event_id and record.event_id != source_event_id:
            # A newer wrap has replaced what we just decrypted. The
            # newer one is either in-flight or queued; let it win.
            return
        # Article metadata is in tags; note "title" we synthesize from
        # the first heading or first non-empty line.
        if int(inner.get("kind", 0)) == INNER_KIND_LONG_FORM:
            meta = extract_article_metadata(inner)
            title = meta["title"]
            snippet = meta["summary"] or derive_preview_snippet(inner.get("content", ""))
        else:
            content = str(inner.get("content", ""))
            title = _note_title_from_content(content)
            snippet = derive_preview_snippet(content)

        record.title = title
        record.snippet = snippet
        record.content = str(inner.get("content", ""))
        record.inner_tags = [list(t) for t in inner.get("tags", []) if isinstance(t, list)]
        record.state = DraftState.READY
        record.failure_reason = ""
        self.record_changed.emit(identifier)

    def set_failed(self, identifier: str, reason: str) -> None:
        record = self._records.get(identifier)
        if record is None:
            return
        record.state = DraftState.FAILED
        record.failure_reason = reason or "decryption failed"
        self.record_changed.emit(identifier)

    def remove(self, identifier: str) -> None:
        if identifier not in self._records:
            return
        del self._records[identifier]
        self.record_removed.emit(identifier)

    # -- mutation: outbound (called after a successful local stash) -------

    def upsert_from_inner(
        self,
        *,
        identifier: str,
        inner: Dict[str, Any],
        event_id: str,
        created_at: int,
        expiration: Optional[int],
    ) -> None:
        """Optimistic update after the editor stashes a draft.

        Lets the panel reflect the new title/snippet immediately without
        waiting for the relay echo + bunker decrypt round-trip.
        """
        existing = self._records.get(identifier)
        if existing is None:
            record = DraftRecord(
                identifier=identifier,
                inner_kind=int(inner.get("kind", INNER_KIND_SHORT_NOTE)),
                state=DraftState.READY,
                event_id=event_id,
                created_at=created_at,
                expiration=expiration,
            )
            self._records[identifier] = record
            # Emit ``record_added`` *before* writing the decrypted body
            # so listeners can index the new row in their view, then
            # repaint it on the subsequent ``record_changed``.
            self.record_added.emit(identifier)
            self.set_decrypted(identifier, inner=inner, source_event_id=event_id)
            return

        existing.event_id = event_id
        existing.created_at = created_at
        existing.expiration = expiration
        existing.inner_kind = int(inner.get("kind", existing.inner_kind))
        self.set_decrypted(identifier, inner=inner, source_event_id=event_id)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _note_title_from_content(content: str) -> str:
    """Synthesize a row title for a short-note draft.

    Notes have no title field. We use the first Markdown heading if
    present, otherwise the first ~60 chars of the first non-empty line.
    If the body is empty, we return "Untitled note" so the row is still
    recognisable in the list.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip() or "Untitled note"
        return line if len(line) <= 60 else line[:59].rstrip() + "…"
    return "Untitled note"
