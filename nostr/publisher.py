# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
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

The builders also attach NIP-92 ``imeta`` tags for media, from records
the caller hands them. One rule governs every field: describe only media
this app itself resolved, and never fabricate a value for a URL nobody
here fetched. A foreign image passes through the content untouched and
gets no tag at all, because a wrong ``x`` tells every other client to
reject the blob it just downloaded, which is worse than an absent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, Signal

import url_safety

from . import CLIENT_NAME
from .bech32 import decode_npub, decode_nprofile, encode_nprofile
from .blossom.hashes import blob_url
from .bunker import BunkerClient, BunkerSessionPool
from .drafts import (
    DEFAULT_EXPIRATION_SECONDS,
    MAX_INNER_PAYLOAD_BYTES,
    SUPPORTED_INNER_KINDS,
    build_draft_wrap,
    build_tombstone_wrap,
    serialize_inner_event,
)
from .events import build_event
from .outbox import RelayListCache, select_draft_publish_relays, select_publish_relays
from .profiles import Profile
from .relay import RelayPool


# Max length we'll surface from a signer-provided error string. Defensive
# truncation against signers that echo plaintext back in error messages
# (we've not observed it in practice but the contract isn't ours).
_MAX_REASON_CHARS: int = 200


def _safe_reason(reason: str) -> str:
    """Clip a signer-provided failure reason to ``_MAX_REASON_CHARS``.

    Keeps log lines bounded and protects against an over-talkative
    signer echoing the request payload into the error text.
    """
    if not reason:
        return "unknown error"
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[:_MAX_REASON_CHARS - 1].rstrip() + "…"


PublishResult = Tuple[str, bool, str]  # (relay_url, ok, message)
Mention = Tuple[str, str]              # (pubkey_hex, relay_hint), hint may be empty


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
    # hint slot, usually the more specific one.
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
# Media attachments (NIP-92 imeta)                                             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PublishedMedia:
    """One image this app resolved into a URL it wrote into the content.

    Every field is something this process measured or a server it
    uploaded to confirmed. Nothing here may be inferred from a URL, and
    an unknown value stays at its default so the builder can leave the
    field out rather than guess it.
    """

    url: str
    sha256: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    alt: str = ""
    size: int = 0
    servers: Tuple[str, ...] = ()


# One event carries at most this many imeta tags. The images stay in the
# content either way; only the metadata is capped.
_MAX_IMETA_TAGS = 100

# Alt text is prose and lands in someone else's UI, so it is clipped
# rather than trusted to be short.
_MAX_ALT_CHARS = 1000

# What the asset layer records when the bytes did not sniff to a known
# image type. Emitting it would claim a measurement nobody made, so the
# ``m`` field is dropped instead and readers can sniff for themselves.
_UNKNOWN_MIME = "application/octet-stream"

_WHITESPACE_RUN_RE = re.compile(r"\s+")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _is_injection_free(value: str) -> bool:
    """Whether ``value`` can be an imeta field value unchanged.

    imeta entries are space-delimited key/value pairs inside a variadic
    tag, so whitespace in a URL forges an extra field and a newline in
    any value forges an extra entry, or an extra tag, inside an event
    the user is about to sign. Both are refused at the source.
    """
    if not isinstance(value, str) or not value:
        return False
    return not _WHITESPACE_RUN_RE.search(value) and not _CONTROL_CHAR_RE.search(value)


def _clean_alt(value: str) -> str:
    """Alt text flattened onto one line and clipped.

    Alt is the one field where spaces are legitimate, so it is repaired
    instead of refused: control characters and newlines become spaces,
    runs collapse, and the result cannot introduce a second entry.
    """
    if not isinstance(value, str) or not value:
        return ""
    flattened = _CONTROL_CHAR_RE.sub(" ", value)
    return _WHITESPACE_RUN_RE.sub(" ", flattened).strip()[:_MAX_ALT_CHARS].strip()


def _fallbacks_for(record: PublishedMedia) -> List[str]:
    """Sibling addresses for one blob, from confirmed servers only.

    ``servers`` holds the origins that answered with this blob, and
    BUD-01 serves every endpoint from the root of the domain, so
    ``<origin>/<sha256>`` is an address this app can stand behind even
    though no server ever handed it that exact string. A configured but
    unconfirmed server never appears: a fallback nobody verified is a
    fabricated one, and it sends readers to a 404.
    """
    if not record.sha256:
        return []
    out: List[str] = []
    for origin in record.servers:
        candidate = blob_url(origin, record.sha256)
        if candidate == record.url or url_safety.same_origin(candidate, record.url):
            continue
        if candidate in out or not _is_injection_free(candidate):
            continue
        if not url_safety.is_safe_media_url(candidate):
            continue
        out.append(candidate)
    return out


def build_imeta_tags(
    media: Sequence[PublishedMedia], content: str
) -> List[List[str]]:
    """NIP-92 ``imeta`` tags for the media this app put into ``content``.

    ``media`` comes from the walk that serialized the document, which is
    the only place that knows which URL was written for which image.
    Rediscovering the URLs with a regex over the finished content was
    rejected: it would find third-party addresses this app never
    fetched, and URLs the user typed as prose, and describing either one
    means guessing metadata nobody measured.

    Field order is fixed so the same document yields the same event id
    twice running. A record is dropped whole when its ``url`` is unsafe,
    is absent from the final content, or when no other field is known,
    since NIP-92 requires ``url`` plus at least one more.
    """
    tags: List[List[str]] = []
    described: set = set()
    for record in media or ():
        if len(tags) >= _MAX_IMETA_TAGS:
            break
        url = (record.url or "").strip()
        if url in described or not _is_injection_free(url):
            continue
        if not url_safety.is_safe_media_url(url):
            continue
        # NIP-92: each tag SHOULD match a URL in the event content. The
        # user can still edit the body in the publish dialog, so an
        # image described here may no longer be there.
        if url not in content:
            continue

        entries: List[str] = []
        mime = (record.mime or "").strip().lower()
        if mime and mime != _UNKNOWN_MIME and _is_injection_free(mime):
            entries.append(f"m {mime}")
        sha = (record.sha256 or "").strip().lower()
        if sha and _is_injection_free(sha):
            entries.append(f"x {sha}")
        if record.width > 0 and record.height > 0:
            entries.append(f"dim {record.width}x{record.height}")
        alt = _clean_alt(record.alt)
        if alt:
            entries.append(f"alt {alt}")
        if record.size > 0:
            entries.append(f"size {record.size}")
        entries.extend(f"fallback {u}" for u in _fallbacks_for(record))
        if not entries:
            continue

        described.add(url)
        tags.append(["imeta", f"url {url}"] + entries)
    return tags


def _is_publishable_cover(url: str) -> bool:
    """Whether a NIP-23 cover address may be signed into an event.

    An imported article's cover is lifted straight out of untrusted feed
    HTML, so it arrives as anything: ``javascript:``, ``data:text/html``,
    ``file:///``, a protocol-relative address or a bare path.

    The mirror-source policy is the right gate rather than the media
    policy. Plenty of real blogs serve covers over plain http and
    refusing those would silently delete a legitimate image, while
    ``javascript:``, ``data:``, ``file:``, userinfo and private address
    literals are all refused. A refusal drops the tag and keeps the
    article: a per-item import must never fail over a cover.
    """
    return _is_injection_free(url) and url_safety.is_safe_mirror_source(url)


# --------------------------------------------------------------------------- #
# Pure builders                                                                #
# --------------------------------------------------------------------------- #

def build_note(
    content: str,
    pubkey_hex: str,
    *,
    mentions: Optional[Sequence[Mention]] = None,
    extra_tags: Optional[List[List[str]]] = None,
    media: Sequence[PublishedMedia] = (),
) -> dict:
    """Construct an unsigned kind 1 event ready for a remote signer.

    ``mentions`` is a list of ``(pubkey_hex, relay_hint)`` tuples, typically
    sourced from the publish dialog's mention-chip row. They're merged with
    any inline ``nostr:n…`` URIs already in ``content`` (deduplicated), and
    their URIs are appended at the end of the body if not already present.

    ``media`` describes images this app resolved into URLs already in
    ``content``; the imeta tags are built here rather than threaded
    through ``extra_tags`` because only this function holds the final
    content, and the "URL is in the content" check has to run against it.

    Always attaches ``["client", CLIENT_NAME]`` so readers that honour the
    NIP-89 client tag display "Published from MyEditor" under the note.
    """
    final_content, p_tags = _resolve_mentions(content, mentions or [])
    tags: List[List[str]] = [["client", CLIENT_NAME]]
    tags.extend(p_tags)
    tags.extend(build_imeta_tags(media, final_content))
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
    media: Sequence[PublishedMedia] = (),
) -> dict:
    """Construct an unsigned NIP-23 long-form article (kind 30023).

    The ``slug`` becomes the ``d``-tag, the identifier that makes this
    event addressable. Re-publishing with the same slug replaces the
    previous version on relays that honour parameterized replacement.

    ``mentions`` follows the same convention as ``build_note``: chip-picked
    profiles whose URIs aren't already in the body are appended at the end,
    and a ``["p", hex, relay-hint]`` tag is emitted per unique pubkey.

    Per NIP-23, ``content`` is Markdown and clients MUST NOT hard line-break
    paragraphs or accept HTML. This does not transform the content; that is the
    caller's responsibility.

    ``image`` is the cover. It is gated through
    :func:`_is_publishable_cover` because an imported article's cover
    comes from untrusted feed HTML; a refused address drops the tag and
    still publishes the article.

    NIP-94's note that it is "not expected to be implemented by ...
    longform clients that deal with kind:30023 articles" is about kind
    1063 file-metadata events, not about ``imeta`` tags, which NIP-92
    defines for any event. Emitting imeta on 30023 is correct.
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
    cover = image.strip()
    if cover and _is_publishable_cover(cover):
        tags.append(["image", cover])
    if published_at is not None:
        tags.append(["published_at", str(int(published_at))])
    for raw in hashtags:
        tag = raw.strip().lstrip("#").lower()
        if tag:
            tags.append(["t", tag])
    tags.extend(build_imeta_tags(media, final_content))
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
# PublishJob: outbox, sign, publish                                           #
# --------------------------------------------------------------------------- #

class PublishJob(QObject):
    """One end-to-end publish of any unsigned event.

    Signals (fired in this order on the happy path):
      status_changed(str)     human-readable progress text
      signed(str)             event id (hex) of the signed event
      completed(list)         final list of PublishResult tuples
                              [(url, ok, message), …], fired even when
                              zero relays accepted
      failed(str)             short reason; terminal. No further signals
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
        entitled_relays: Sequence[str] = (),
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
        # Relays this account has standing on beyond its own list.
        self._entitled_relays = list(entitled_relays)
        self._profile = profile
        self._unsigned = unsigned_event

    def start(self) -> None:
        """Kick off the publish. Safe to call once per instance."""
        self.status_changed.emit("Looking up your relay list…")
        # Always include the profile's bunker relays when querying, even
        # if the user has no NIP-65 published, we still want a fast result.
        relays_to_query = list(dict.fromkeys(list(self._profile.bunker_relays)))
        self._relay_list_cache.fetch(
            self._profile.user_pubkey,
            relays=relays_to_query,
            on_done=self._on_relay_list_resolved,
        )

    # -- pipeline ----------------------------------------------------------

    def _on_relay_list_resolved(self, relay_list) -> None:
        publish_relays = select_publish_relays(
            relay_list.write, entitled=self._entitled_relays,
        )
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


# --------------------------------------------------------------------------- #
# DraftPublishJob: stash an unsigned inner event as a NIP-37 draft            #
# --------------------------------------------------------------------------- #

class DraftPublishJob(QObject):
    """End-to-end stash of one inner event as a NIP-37 draft wrap.

    Pipeline:
      1. Resolve NIP-65 relay list.
      2. Acquire bunker session.
      3. Bunker NIP-44 encrypts ``serialize_inner_event(inner)``.
      4. Wrap the ciphertext in a kind-31234 event.
      5. Bunker signs the wrap.
      6. Publish to ``select_draft_publish_relays`` (write ∪ read ∪
         bunker ∪ base) so other devices reading from any of those
         sets can decrypt the same draft.

    Signals (in firing order on the happy path):
      status_changed(str)        progress text
      stashed(str, str, int)     (identifier, event_id, created_at) on
                                 successful sign. Fires before relay
                                 results are in so the panel can update
                                 optimistically.
      completed(list)            list of PublishResult tuples
      failed(str)                terminal. No further signals after this

    Cancellation: ``cancel()`` flips a flag that suppresses all future
    signal emissions. The in-flight NIP-46 RPC can't actually be
    recalled, but the dialog (now destroyed) will no longer be
    notified, preventing "wrapped C/C++ object has been deleted"
    warnings on PySide6.
    """

    status_changed = Signal(str)
    stashed = Signal(str, str, int)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        profile: Profile,
        inner_event: dict,
        identifier: str,
        expiration_seconds: int = DEFAULT_EXPIRATION_SECONDS,
        extra_wrap_tags: Optional[List[List[str]]] = None,
        entitled_relays: Sequence[str] = (),
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if inner_event.get("pubkey", "").lower() != profile.user_pubkey.lower():
            raise ValueError(
                "inner event pubkey does not match the stashing profile"
            )
        if not identifier:
            raise ValueError("draft identifier (d-tag) must not be empty")
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        # Relays this account has standing on beyond its own list.
        self._entitled_relays = list(entitled_relays)
        self._profile = profile
        self._inner_event = inner_event
        self._identifier = identifier
        self._expiration_seconds = expiration_seconds
        self._extra_wrap_tags = extra_wrap_tags
        self._wrap_unsigned: Optional[dict] = None
        self._cancelled: bool = False

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Kick off the stash. Safe to call once per instance."""
        self._emit_status("Looking up your relay list…")
        relays_to_query = list(dict.fromkeys(list(self._profile.bunker_relays)))
        self._relay_list_cache.fetch(
            self._profile.user_pubkey,
            relays=relays_to_query,
            on_done=self._on_relay_list_resolved,
        )

    def cancel(self) -> None:
        """Suppress further signal emissions; the in-flight RPC runs out."""
        self._cancelled = True

    # -- signal-emission helpers (guarded by ``_cancelled``) --------------

    def _emit_status(self, text: str) -> None:
        if not self._cancelled:
            self.status_changed.emit(text)

    def _emit_failed(self, reason: str) -> None:
        if not self._cancelled:
            self.failed.emit(_safe_reason(reason))

    # -- pipeline ----------------------------------------------------------

    def _on_relay_list_resolved(self, relay_list) -> None:
        if self._cancelled:
            return
        publish_relays = select_draft_publish_relays(
            relay_list,
            bunker_relays=self._profile.bunker_relays,
            entitled=self._entitled_relays,
        )
        self._emit_status("Connecting to your signer…")
        self._session_pool.get(
            self._profile,
            on_ready=lambda client: self._on_bunker_ready(client, publish_relays),
            on_error=self._emit_failed,
        )

    def _on_bunker_ready(self, client: BunkerClient, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        try:
            plaintext = serialize_inner_event(self._inner_event)
        except (KeyError, TypeError, ValueError) as exc:
            self._emit_failed(f"Could not serialize draft: {exc}")
            return
        # Pre-flight against the NIP-44 v2 plaintext cap (65535 bytes
        # post-encode). The bunker would reject larger payloads anyway,
        # surfacing it here yields a clearer message and avoids one
        # round-trip + approval prompt for an inevitable failure.
        payload_bytes = len(plaintext.encode("utf-8"))
        if payload_bytes > MAX_INNER_PAYLOAD_BYTES:
            self._emit_failed(
                f"Draft is too large to encrypt "
                f"({payload_bytes:,} of {MAX_INNER_PAYLOAD_BYTES:,} bytes). "
                "Split it across smaller drafts or publish directly."
            )
            return
        self._emit_status(
            "Encrypting draft. Approve on your signer if prompted…"
        )
        client.nip44_encrypt_self(
            plaintext,
            on_success=lambda ct: self._on_encrypted(ct, client, publish_relays),
            on_failure=lambda reason: self._emit_failed(
                f"Could not encrypt draft: {_safe_reason(reason)}"
            ),
        )

    def _on_encrypted(
        self,
        ciphertext: str,
        client: BunkerClient,
        publish_relays: List[str],
    ) -> None:
        if self._cancelled:
            return
        try:
            self._wrap_unsigned = build_draft_wrap(
                identifier=self._identifier,
                inner_kind=int(self._inner_event["kind"]),
                encrypted_content=ciphertext,
                pubkey_hex=self._profile.user_pubkey,
                client_name=CLIENT_NAME,
                expiration_seconds=self._expiration_seconds,
                extra_tags=self._extra_wrap_tags,
            )
        except ValueError as exc:
            self._emit_failed(f"Could not build draft wrap: {exc}")
            return

        self._emit_status(
            "Waiting for signature. Approve the request on your signer…"
        )
        client.sign_event(
            self._wrap_unsigned,
            on_success=lambda signed: self._on_signed(signed, publish_relays),
            on_failure=self._emit_failed,
        )

    def _on_signed(self, signed_event: dict, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        self.stashed.emit(
            self._identifier,
            signed_event["id"],
            int(signed_event["created_at"]),
        )
        self._emit_status(
            f"Publishing draft to {len(publish_relays)} relays…"
        )
        job = self._relay_pool.publish(publish_relays, signed_event)
        job.all_done.connect(self._on_publish_done)

    def _on_publish_done(self, results: List[PublishResult]) -> None:
        if self._cancelled:
            return
        accepted = sum(1 for _, ok, _ in results if ok)
        self._emit_status(
            f"Draft saved to {accepted}/{len(results)} relays."
        )
        self.completed.emit(results)


# --------------------------------------------------------------------------- #
# DraftDeleteJob: tombstone an existing draft                                 #
# --------------------------------------------------------------------------- #

class DraftDeleteJob(QObject):
    """Publish a blank-content replacement wrap to soft-delete a draft.

    Per NIP-37 the deletion mechanism is *not* NIP-09; the addressable
    event is replaced with one whose ``content`` is empty. Same shape
    as ``DraftPublishJob`` minus the encryption step (no plaintext).

    Signals:
      status_changed(str)
      tombstoned(str, str)       (identifier, event_id) right after the
                                 signer returns the signed tombstone,
                                 lets the panel remove the row before
                                 relay results land.
      completed(list)            list of PublishResult tuples
      failed(str)
    """

    status_changed = Signal(str)
    tombstoned = Signal(str, str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        profile: Profile,
        identifier: str,
        inner_kind: int,
        entitled_relays: Sequence[str] = (),
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if not identifier:
            raise ValueError("draft identifier (d-tag) must not be empty")
        if inner_kind not in SUPPORTED_INNER_KINDS:
            raise ValueError(
                f"unsupported inner kind {inner_kind!r}; "
                f"expected one of {SUPPORTED_INNER_KINDS}"
            )
        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        # Relays this account has standing on beyond its own list.
        self._entitled_relays = list(entitled_relays)
        self._profile = profile
        self._identifier = identifier
        self._inner_kind = inner_kind
        self._cancelled: bool = False

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        self._emit_status("Looking up your relay list…")
        self._relay_list_cache.fetch(
            self._profile.user_pubkey,
            relays=list(dict.fromkeys(self._profile.bunker_relays)),
            on_done=self._on_relay_list_resolved,
        )

    def cancel(self) -> None:
        self._cancelled = True

    # -- guarded emit helpers ---------------------------------------------

    def _emit_status(self, text: str) -> None:
        if not self._cancelled:
            self.status_changed.emit(text)

    def _emit_failed(self, reason: str) -> None:
        if not self._cancelled:
            self.failed.emit(_safe_reason(reason))

    # -- pipeline ----------------------------------------------------------

    def _on_relay_list_resolved(self, relay_list) -> None:
        if self._cancelled:
            return
        publish_relays = select_draft_publish_relays(
            relay_list,
            bunker_relays=self._profile.bunker_relays,
            entitled=self._entitled_relays,
        )
        self._session_pool.get(
            self._profile,
            on_ready=lambda client: self._on_bunker_ready(client, publish_relays),
            on_error=self._emit_failed,
        )

    def _on_bunker_ready(self, client: BunkerClient, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        unsigned = build_tombstone_wrap(
            identifier=self._identifier,
            inner_kind=self._inner_kind,
            pubkey_hex=self._profile.user_pubkey,
            client_name=CLIENT_NAME,
        )
        self._emit_status(
            "Waiting for signature. Approve the deletion on your signer…"
        )
        client.sign_event(
            unsigned,
            on_success=lambda signed: self._on_signed(signed, publish_relays),
            on_failure=self._emit_failed,
        )

    def _on_signed(self, signed_event: dict, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        self.tombstoned.emit(self._identifier, signed_event["id"])
        self._emit_status(f"Removing draft from {len(publish_relays)} relays…")
        job = self._relay_pool.publish(publish_relays, signed_event)
        job.all_done.connect(self._on_publish_done)

    def _on_publish_done(self, results: List[PublishResult]) -> None:
        if self._cancelled:
            return
        accepted = sum(1 for _, ok, _ in results if ok)
        self._emit_status(
            f"Draft removed on {accepted}/{len(results)} relays."
        )
        self.completed.emit(results)


class DraftBulkDeleteJob(QObject):
    """Delete several drafts, one signer round-trip at a time.

    Sequential rather than parallel, and not as a performance choice.
    Every deletion needs its own signature, and a remote signer answers
    one request at a time whatever this end does; firing all of them at
    once would put N prompts on the user's phone in an order nobody
    chose, and take away the ability to stop after the third.

    One failure does not end the run. The user asked for these to go,
    and the ones that can go should. What they get at the end is a count
    of what did not, because a partial result reported as a whole one is
    the version of this that loses drafts quietly.

    Cancelling stops the run before the next signature is requested. It
    cannot recall a deletion already signed and published, so the count
    in ``finished`` is what actually happened rather than what was asked
    for.

    Signals:
      progress(int, int)   drafts settled so far, total asked for.
      tombstoned(str, str) (identifier, event_id) as each one is signed,
                           forwarded so the panel can drop the row before
                           the relay results land.
      finished(int, list)  how many were deleted, and the
                           (identifier, reason) pairs for those that
                           were not.
    """

    progress = Signal(int, int)
    tombstoned = Signal(str, str)
    finished = Signal(int, list)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_list_cache: RelayListCache,
        session_pool: BunkerSessionPool,
        profile: Profile,
        targets: Sequence[Tuple[str, int]],
        entitled_relays: Sequence[str] = (),
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        # Validated here, before a single signature is asked for. A run
        # that would die on its ninth draft because of something knowable
        # at the start has already cost the user eight approvals.
        self._targets: List[Tuple[str, int]] = []
        for identifier, inner_kind in targets:
            if not identifier:
                raise ValueError("draft identifier (d-tag) must not be empty")
            if inner_kind not in SUPPORTED_INNER_KINDS:
                raise ValueError(
                    f"unsupported inner kind {inner_kind!r}; "
                    f"expected one of {SUPPORTED_INNER_KINDS}"
                )
            self._targets.append((identifier, int(inner_kind)))

        self._relay_pool = relay_pool
        self._relay_list_cache = relay_list_cache
        self._session_pool = session_pool
        self._entitled_relays = list(entitled_relays)
        self._profile = profile

        self._index: int = 0
        self._deleted: int = 0
        self._failures: List[Tuple[str, str]] = []
        self._current: Optional[DraftDeleteJob] = None
        self._cancelled: bool = False
        # Re-entrancy guard, the same shape the decrypt queues use: a
        # signer that answers synchronously would otherwise recurse once
        # per draft.
        self._pumping: bool = False
        self._inflight: bool = False
        self._done: bool = False

    # -- public API --------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self._targets)

    def start(self) -> None:
        if not self._targets:
            self._finish()
            return
        self._pump()

    def cancel(self) -> None:
        """Stop before the next signature is asked for.

        The in-flight deletion is cancelled too, but it may already be
        signed and on its way to the relays, which is why this reports
        rather than promises.
        """
        if self._cancelled or self._done:
            return
        self._cancelled = True
        if self._current is not None:
            self._current.cancel()
            self._current = None
        self._finish()

    # -- the queue ---------------------------------------------------------

    def _pump(self) -> None:
        if self._pumping or self._inflight or self._done:
            return
        self._pumping = True
        try:
            while (
                not self._cancelled
                and not self._inflight
                and self._index < len(self._targets)
            ):
                identifier, inner_kind = self._targets[self._index]
                self._inflight = True
                self._start_one(identifier, inner_kind)
        finally:
            self._pumping = False
        if not self._inflight and not self._cancelled:
            self._finish()

    def _start_one(self, identifier: str, inner_kind: int) -> None:
        try:
            job = DraftDeleteJob(
                relay_pool=self._relay_pool,
                relay_list_cache=self._relay_list_cache,
                session_pool=self._session_pool,
                profile=self._profile,
                identifier=identifier,
                inner_kind=inner_kind,
                entitled_relays=self._entitled_relays,
                parent=self,
            )
        except ValueError as exc:
            # Cannot happen for targets that passed the constructor, but
            # a run that dies here would strand the whole queue.
            self._settle(identifier, reason=str(exc))
            return

        self._current = job
        job.tombstoned.connect(self.tombstoned)
        job.completed.connect(
            lambda results, ident=identifier: self._on_one_completed(ident, results)
        )
        job.failed.connect(
            lambda reason, ident=identifier: self._settle(ident, reason=reason)
        )
        job.start()

    def _on_one_completed(self, identifier: str, results: List[PublishResult]) -> None:
        # ``completed`` fires even when every relay refused, so the
        # results decide whether this counts as deleted. Reporting a
        # deletion no relay accepted would tell the user a draft is gone
        # from a place it is still on.
        if any(ok for _, ok, _ in results):
            self._settle(identifier)
            return
        reasons = "; ".join(msg for _url, ok, msg in results if not ok and msg)
        self._settle(
            identifier,
            reason=f"no relay accepted the deletion{f' ({reasons})' if reasons else ''}",
        )

    def _settle(self, identifier: str, *, reason: str = "") -> None:
        if self._done:
            return
        if reason:
            self._failures.append((identifier, _safe_reason(reason)))
        else:
            self._deleted += 1
        self._current = None
        self._index += 1
        self._inflight = False
        self.progress.emit(self._index, len(self._targets))
        if self._cancelled:
            return
        self._pump()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit(self._deleted, list(self._failures))
