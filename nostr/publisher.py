"""End-to-end publishing: resolve relays → sign via signer → publish.

One generic ``PublishJob`` drives the pipeline for any unsigned event
(short notes, articles, anything else later). Concrete event builders
live alongside it as pure functions so callers stay declarative.

Shape of the flow:

  1. Look up the author's NIP-65 write relays (cached after first hit).
  2. Open or reuse the bunker session for the active profile.
  3. Hand the unsigned event to the signer. The user typically has to
     approve on their phone here.
  4. Publish the signed event with eager-first-accept semantics.
  5. Emit ``completed(results)`` with per-relay outcomes.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, Signal

from . import CLIENT_NAME
from .bech32 import decode_npub, decode_nprofile, encode_nprofile
from .bunker import BunkerSessionPool
from .events import build_event
from .outbox import RelayListCache, select_publish_relays
from .profiles import Profile
from .relay import RelayPool


PublishResult = Tuple[str, bool, str]  # (relay_url, ok, message)
Mention = Tuple[str, str]              # (pubkey_hex, relay_hint) — hint may be empty


# --------------------------------------------------------------------------- #
# Mention handling                                                            #
# --------------------------------------------------------------------------- #

# nostr:npub1… or nostr:nprofile1…  (NIP-21 URI form, NIP-19 bech32 body)
_NOSTR_URI_RE = re.compile(
    r"nostr:(n(?:pub|profile)1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]+)"
)


def extract_inline_mentions(content: str) -> List[Mention]:
    """Find ``nostr:npub|nprofile`` URIs in ``content`` and decode them.

    Returns ``(pubkey_hex, relay_hint)`` tuples in document order; any URI
    that fails bech32 decoding is silently skipped (e.g. a near-miss that
    looks like a URI but isn't valid bech32).
    """
    found: List[Mention] = []
    for match in _NOSTR_URI_RE.finditer(content):
        bech = match.group(1)
        try:
            if bech.startswith("npub1"):
                pk = decode_npub(bech)
                hint = ""
            else:  # nprofile1
                pk, relays = decode_nprofile(bech)
                hint = relays[0] if relays else ""
        except ValueError:
            continue
        found.append((pk, hint))
    return found


def _resolve_mentions(
    content: str, chip_mentions: Sequence[Mention]
) -> Tuple[str, List[List[str]]]:
    """Merge inline + chip mentions into (final_content, p_tags).

    Behaviour:
      - Any ``nostr:n…`` URI already inline in ``content`` is left where it is.
      - Chip mentions whose pubkey is NOT already inline are appended to the
        body as ``nostr:nprofile1…`` URIs, separated from prose by one blank
        line and from each other by single spaces.
      - The resulting p-tag list deduplicates by pubkey, preserving the
        first-seen relay hint (inline URIs are seen first).
    """
    inline = extract_inline_mentions(content)
    inline_pks = {pk for pk, _ in inline}

    appended_uris: List[str] = []
    for pk, hint in chip_mentions:
        if pk in inline_pks:
            continue
        nprofile = encode_nprofile(pk, [hint] if hint else [])
        appended_uris.append(f"nostr:{nprofile}")
        inline_pks.add(pk)  # avoid double-appending if caller passes dupes

    if appended_uris:
        sep = "\n\n" if content.strip() else ""
        final_content = content + sep + " ".join(appended_uris)
    else:
        final_content = content

    # Union of mention pubkeys for p-tags. Order: inline first, then chip
    # mentions in the order the user picked them. First seen wins the relay
    # hint slot — usually the more specific one.
    seen: set[str] = set()
    p_tags: List[List[str]] = []
    for pk, hint in list(inline) + list(chip_mentions):
        if pk in seen:
            continue
        seen.add(pk)
        tag = ["p", pk]
        if hint:
            tag.append(hint)
        p_tags.append(tag)
    return final_content, p_tags


# --------------------------------------------------------------------------- #
# Pure builders                                                                #
# --------------------------------------------------------------------------- #

def build_note(
    content: str,
    pubkey_hex: str,
    *,
    mentions: Optional[Sequence[Mention]] = None,
    extra_tags: Optional[List[List[str]]] = None,
) -> dict:
    """Construct an unsigned kind 1 event ready for a remote signer.

    ``mentions`` is a list of ``(pubkey_hex, relay_hint)`` tuples — typically
    sourced from the publish dialog's mention-chip row. They're merged with
    any inline ``nostr:n…`` URIs already in ``content`` (deduplicated), and
    their URIs are appended at the end of the body if not already present.

    Always attaches ``["client", CLIENT_NAME]`` so readers that honour the
    NIP-89 client tag display "Published from My-Editor" under the note.
    """
    final_content, p_tags = _resolve_mentions(content, mentions or [])
    tags: List[List[str]] = [["client", CLIENT_NAME]]
    tags.extend(p_tags)
    if extra_tags:
        tags.extend(extra_tags)
    return build_event(
        kind=1,
        content=final_content,
        tags=tags,
        pubkey_hex=pubkey_hex,
    )


def build_article(
    content: str,
    pubkey_hex: str,
    slug: str,
    *,
    title: str = "",
    summary: str = "",
    image: str = "",
    published_at: Optional[int] = None,
    hashtags: Iterable[str] = (),
    mentions: Optional[Sequence[Mention]] = None,
    extra_tags: Optional[List[List[str]]] = None,
) -> dict:
    """Construct an unsigned NIP-23 long-form article (kind 30023).

    The ``slug`` becomes the ``d``-tag — the identifier that makes this
    event addressable. Re-publishing with the same slug replaces the
    previous version on relays that honour parameterized replacement.

    ``mentions`` follows the same convention as ``build_note``: chip-picked
    profiles whose URIs aren't already in the body are appended at the end,
    and a ``["p", hex, relay-hint]`` tag is emitted per unique pubkey.

    Per NIP-23, ``content`` is Markdown and clients MUST NOT hard line-break
    paragraphs or accept HTML — we don't transform the content; that's the
    caller's responsibility.
    """
    if not slug.strip():
        raise ValueError("article slug (d-tag) must not be empty")
    final_content, p_tags = _resolve_mentions(content, mentions or [])
    tags: List[List[str]] = [
        ["client", CLIENT_NAME],
        ["d", slug.strip()],
    ]
    tags.extend(p_tags)
    if title.strip():
        tags.append(["title", title.strip()])
    if summary.strip():
        tags.append(["summary", summary.strip()])
    if image.strip():
        tags.append(["image", image.strip()])
    if published_at is not None:
        tags.append(["published_at", str(int(published_at))])
    for raw in hashtags:
        tag = raw.strip().lstrip("#").lower()
        if tag:
            tags.append(["t", tag])
    if extra_tags:
        tags.extend(extra_tags)
    return build_event(
        kind=30023,
        content=final_content,
        tags=tags,
        pubkey_hex=pubkey_hex,
    )


# --------------------------------------------------------------------------- #
# Slug helper                                                                  #
# --------------------------------------------------------------------------- #

_SLUG_KEEP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, fallback: str = "untitled") -> str:
    """Turn a title (or filename) into a stable d-tag identifier.

    Lowercase, ASCII-only, hyphen-separated. Returns ``fallback`` if the
    cleaned result is empty.
    """
    cleaned = _SLUG_KEEP.sub("-", text.strip().lower()).strip("-")
    return cleaned or fallback


# --------------------------------------------------------------------------- #
# PublishJob — outbox → sign → publish                                        #
# --------------------------------------------------------------------------- #

class PublishJob(QObject):
    """One end-to-end publish of any unsigned event.

    Signals (fired in this order on the happy path):
      status_changed(str)     human-readable progress text
      signed(str)             event id (hex) of the signed event
      completed(list)         final list of PublishResult tuples
                              [(url, ok, message), …] — fired even when
                              zero relays accepted
      failed(str)             short reason; terminal — no further signals
                              after this
    """

    status_changed = Signal(str)
    signed = Signal(str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        profile: Profile,
        unsigned_event: dict,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if unsigned_event.get("pubkey") != profile.user_pubkey:
            raise ValueError(
                "unsigned event pubkey does not match the publishing profile"
            )
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._profile = profile
        self._unsigned = unsigned_event

    def start(self) -> None:
        """Kick off the publish. Safe to call once per instance."""
        self.status_changed.emit("Looking up your relay list…")
        # Always include the profile's bunker relays when querying — even
        # if the user has no NIP-65 published, we still want a fast result.
        relays_to_query = list(dict.fromkeys(list(self._profile.bunker_relays)))
        self._relay_list_cache.fetch(
            self._profile.user_pubkey,
            relays=relays_to_query,
            on_done=self._on_relay_list_resolved,
        )

    # -- pipeline ----------------------------------------------------------

    def _on_relay_list_resolved(self, relay_list) -> None:
        publish_relays = select_publish_relays(relay_list.write)
        self.status_changed.emit("Connecting to your signer…")
        self._session_pool.get(
            self._profile,
            on_ready=lambda client: self._on_bunker_ready(client, publish_relays),
            on_error=self.failed.emit,
        )

    def _on_bunker_ready(self, client, publish_relays: List[str]) -> None:
        self.status_changed.emit(
            "Waiting for signature. Approve the request on your signer…"
        )
        client.sign_event(
            self._unsigned,
            on_success=lambda signed: self._on_signed(signed, publish_relays),
            on_failure=self.failed.emit,
        )

    def _on_signed(self, signed_event: dict, publish_relays: List[str]) -> None:
        self.signed.emit(signed_event["id"])
        self.status_changed.emit(
            f"Publishing to {len(publish_relays)} relays…"
        )
        job = self._relay_pool.publish(publish_relays, signed_event)
        job.first_accept.connect(self._on_first_accept)
        job.all_done.connect(self._on_publish_done)

    def _on_first_accept(self, url: str) -> None:
        # Surface the win immediately so the dialog can flip to a success
        # state even before the slower relays finish reporting.
        self.status_changed.emit(f"Accepted by {url}. Waiting for the rest…")

    def _on_publish_done(self, results: List[PublishResult]) -> None:
        accepted = sum(1 for _, ok, _ in results if ok)
        self.status_changed.emit(
            f"Published. {accepted}/{len(results)} relays accepted."
        )
        self.completed.emit(results)
