# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify before opening anything (editorial contract 12.4, feat/redaktion
0820f0d).

The server holds no keys, but it serves every binding: a View's ``p_sub``
and the wraps next to it could be ones it minted itself. So nothing is
opened on the strength of a View. :func:`verify_revision` checks the
revision's stored signed action (10) and returns the values of its body
as a :class:`VerifiedRevision`; the openers take nothing else:

- :func:`open_submission_key`: the wrap for the person's own recipient
  name, the body's, opened by their signer; the key must have the pubkey
  ``p_sub``.
- :func:`open_envelope`: the envelope's bytes must hash to
  ``envelope_sha256``, its plaintext to ``revision_hash``.
- :class:`MediaKeys`: media keys only from an envelope so opened, kept
  per submission and ``cipher_sha256``.

A work that is already public has no envelope (contract 12.4, feat/redaktion
3b00351): a submission by link carries the signed kind 30023 in its body,
a proposal is signed by the View's ``proposal_by``. The same verification
holds, with ``revision_hash`` the id of that event, its signature, kind
and address checked, and the sid recomputed from the signer and the
event's coordinate; its submission key opens like any other.

Whatever fails is :class:`NotConfirmed`: shown as "nicht bestätigt", and
nothing is opened. A signer that cannot open a wrap right now is
:class:`NotOpened`, which asking again may change.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from .. import events as nostr_events
from ..bech32 import decode_naddr
from ..later import answer_later
from . import formats
from .crypto import (
    StandupCryptoError,
    WrapInvalid,
    _check_submission_key,
    _decrypt_media,
    _open_revision,
    pubkey_of,
    read_wrap_plaintext,
)
from .views import (
    RevisionRecord,
    SignedActionInvalid,
    SubmissionView,
    action_path,
    signed_action_body,
    signed_path,
)

__all__ = [
    "MediaKeys", "NotConfirmed", "NotOpened", "OpenedRevision", "VerifiedRevision",
    "open_envelope", "open_submission_key", "verify_revision",
]


class NotConfirmed(ValueError):
    """What is served is not what was signed: shown as "nicht bestätigt",
    and nothing is opened."""


class NotOpened(ValueError):
    """The signer could not open the wrap now (refused, failed, no answer)."""


# Only this module mints a VerifiedRevision or an OpenedRevision: one
# built from a server's values elsewhere would open what nothing checked
# (RG-M1 12). The token refuses one built elsewhere; the register of
# those made here, by identity, refuses a copy with other values
# (dataclasses.replace keeps the token, RG-M2a 5). Every opener and
# MediaKeys.add takes only what is in it.
_MINTED = object()
_MADE: Dict[int, "weakref.ReferenceType"] = {}


def _minted_here(token: object, what: str) -> None:
    if token is not _MINTED:
        raise TypeError(f"a {what} comes only from nostr/standup/opening.py")


def _made(value):
    key = id(value)
    _MADE[key] = weakref.ref(value, lambda _gone, key=key: _MADE.pop(key, None))
    return value


def _made_here(value: object, kind: type, what: str) -> None:
    ref = _MADE.get(id(value))
    if not isinstance(value, kind) or ref is None or ref() is not value:
        raise TypeError(f"a {what} comes only from nostr/standup/opening.py")


@dataclass(frozen=True)
class VerifiedRevision:
    """A revision's values, taken from its verified signed body.
    ``envelope_sha256`` is None for a work already public (no envelope;
    ``revision_hash`` is then the id of its signed kind 30023);
    ``correction`` when a Redaktion member signed it, whose body names no
    ``p_sub``, kind or title (those are the writer's verified revision's)."""

    submission_id: str
    revision: int
    revision_hash: str
    envelope_sha256: Optional[str]
    p_sub: str
    kind: str
    title: str
    wraps: Mapping[str, Mapping[str, str]]
    signer: str
    correction: bool
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _minted_here(self._token, "verified revision")


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise NotConfirmed(reason)


def verify_revision(view: SubmissionView, record: RevisionRecord, *,
                    redaktion: Iterable[str] = (),
                    writer_revision: Optional[VerifiedRevision] = None) -> VerifiedRevision:
    """``record`` of the submission ``view`` shows, verified against its
    stored signed action, or :class:`NotConfirmed`.

    A writer's submission or revision (``POST /api/submissions``) must be
    signed by the View's writer; a correction (``.../corrections``) by a
    member of the Redaktion (``redaktion``, from the member list), and it
    takes ``p_sub``, kind and title from ``writer_revision``, the writer's
    verified revision it corrects. A writer's body must name the View's
    ``publication`` and ``round``, and its ``submission_id`` must
    recompute from the signer, that ``publication``, its
    ``coordinate_hash`` and ``round`` (section 2): else a server could
    hand any sid to a writer it chose. The body's ``submission_id``,
    ``revision``, ``revision_hash``, ``envelope_sha256``, ``p_sub``,
    ``kind`` and ``title`` must equal what is shown: the record's, and the
    View's for the revision it shows. A View shows only the current
    revision, so an older one's title is not compared; show the verified
    one this returns.

    A work already public (3b00351): a body with a ``source`` (submit by
    link) has its event checked by :func:`_public_work`, ``revision_hash``
    its id and ``coordinate_hash`` its coordinate; its signer is one of
    the event's authors and the View's writer, or, for a proposal, the
    View's ``proposal_by`` with the event's author as the View's writer.
    A proposal made on the publication (``.../proposals``) is signed by
    ``proposal_by``; it is revision 1 of an article, and its sid
    recomputes from the signer, the View's publication, the event's
    coordinate and its round."""
    if writer_revision is not None:
        _made_here(writer_revision, VerifiedRevision, "verified revision")
    sid = view.submission_id
    signed = record.signed
    path = signed_path(signed) if signed is not None else None
    correction = path == action_path("correction", sid)
    proposed = path == _proposals_path(view.publication)
    if correction:
        signers = set(redaktion)
    else:
        signers = {view.proposal_by if view.proposal_by is not None else view.writer}
    try:
        body = signed_action_body(
            signed, path=path if correction or proposed else action_path("submitted", sid),
            submission_id=sid, signers=signers)
    except SignedActionInvalid as exc:
        raise NotConfirmed(str(exc)) from None
    signer = signed.event["pubkey"]
    if proposed:
        return _proposal(view, record, body, signer)
    _need(formats.is_int(body.get("revision"), low=1) and body["revision"] == record.revision,
          "the revision signed is another")
    _need(body.get("revision_hash") == record.revision_hash, "the revision hash signed is another")
    _need(body.get("envelope_sha256") == record.envelope_sha256,
          "the envelope signed is another")
    wraps = body.get("wraps")
    _need(isinstance(wraps, dict) and all(formats.is_wrap(w) for w in wraps.values()),
          "the wraps signed are malformed")
    if correction:
        _need(writer_revision is not None and writer_revision.submission_id == sid
              and not writer_revision.correction, "the writer's revision is not verified")
        p_sub, kind, title = writer_revision.p_sub, writer_revision.kind, writer_revision.title
    else:
        _writer_binding(body, view, signer)
        _source_binding(body, view, record, signer)
        p_sub, kind, title = body.get("p_sub"), body.get("kind"), body.get("title")
    _need(p_sub == view.p_sub, "the submission key signed is another")
    _need(kind == view.kind, "the kind signed is another")
    if record.revision == view.revision:
        _need(record.revision_hash == view.revision_hash and title == view.title,
              "the revision shown is not the one signed")
    return _made(VerifiedRevision(
        submission_id=sid, revision=record.revision, revision_hash=record.revision_hash,
        envelope_sha256=record.envelope_sha256, p_sub=p_sub, kind=kind, title=title,
        wraps={name: dict(wrap) for name, wrap in wraps.items()},
        signer=signer, correction=correction, _token=_MINTED))


def _proposals_path(publication: str) -> Optional[str]:
    try:
        return formats.site_path(publication) + "/proposals"
    except formats.FormatError:
        return None


def _public_work(source: Any) -> Tuple[Mapping[str, Any], str]:
    """The signed kind 30023 of a work already public and its ``d``: its
    signature verifies and its ``naddr`` names it (kind, author, ``d``),
    as the server checked (core/editorial_submission.link_source)."""
    _need(isinstance(source, dict), "the public work signed is missing")
    event = source.get("event")
    _need(nostr_events.is_verified_event(event, kind=formats.ARTICLE_KIND),
          "the public work's signature does not verify")
    d = next((tag[1] for tag in event["tags"] if tag and tag[0] == "d"), None)
    _need(isinstance(d, str), "the public work has no identifier")
    try:
        identifier, author, kind, _relays = decode_naddr(source.get("naddr"))
    except (TypeError, ValueError):
        raise NotConfirmed("the link to the public work is malformed") from None
    _need((identifier, author, kind) == (d, event["pubkey"], formats.ARTICLE_KIND),
          "the link names another work")
    return event, d


def _is_author(event: Mapping[str, Any], pubkey: str) -> bool:
    """The event's author or a co-author ``["p", pk, relay, "author"]``."""
    return pubkey == event["pubkey"] or any(
        len(tag) >= 4 and tag[0] == "p" and tag[1] == pubkey and tag[3] == "author"
        for tag in event["tags"])


def _source_binding(body: Mapping, view: SubmissionView, record: RevisionRecord,
                    signer: str) -> None:
    """A writer's body with an envelope, or a work already public by link
    whose revision is its event; who signed it matches what the View says
    of the writer and the proposer (12.4, D17)."""
    if "source" not in body:
        _need(formats.is_hex64(record.envelope_sha256) and view.proposal_by is None,
              "no envelope was signed")
        return
    event, d = _public_work(body["source"])
    _need(record.envelope_sha256 is None and record.revision_hash == event["id"],
          "the revision signed is not the public work")
    _need(body.get("coordinate_hash") == formats.coordinate_hash(event["pubkey"], d),
          "the address signed is not the public work's")
    if view.proposal_by is None:
        _need(_is_author(event, signer), "the work was handed in by someone not its author")
    else:
        _need(not _is_author(event, signer) and view.writer == event["pubkey"],
              "the proposal names another writer")


def _proposal(view: SubmissionView, record: RevisionRecord, body: Mapping,
              signer: str) -> VerifiedRevision:
    """A proposal made on the publication (``POST .../proposals``): its
    body names no publication, revision or hash; the path, revision 1 and
    the event stand for them (12.4, core/editorial_submission.parse_proposal)."""
    event, d = _public_work({"naddr": body.get("naddr"), "event": body.get("event")})
    _need(record.revision == 1 and record.envelope_sha256 is None
          and record.revision_hash == event["id"], "the revision signed is not the public work")
    _need(body.get("round") == view.round and formats.is_int(view.round, low=1),
          "the round signed is another")
    coordinate = formats.coordinate_hash(event["pubkey"], d)
    _need(formats.submission_id(signer, view.publication, coordinate, view.round)
          == view.submission_id, "the submission id does not belong to its proposer")
    _need(view.writer == event["pubkey"], "the proposal names another writer")
    wraps = body.get("wraps")
    _need(isinstance(wraps, dict) and all(formats.is_wrap(w) for w in wraps.values()),
          "the wraps signed are malformed")
    p_sub, title = body.get("p_sub"), body.get("title")
    _need(p_sub == view.p_sub, "the submission key signed is another")
    _need(view.kind == "article", "the kind signed is another")
    if record.revision == view.revision:
        _need(record.revision_hash == view.revision_hash and title == view.title,
              "the revision shown is not the one signed")
    return _made(VerifiedRevision(
        submission_id=view.submission_id, revision=1, revision_hash=record.revision_hash,
        envelope_sha256=None, p_sub=p_sub, kind="article", title=title,
        wraps={name: dict(wrap) for name, wrap in wraps.items()},
        signer=signer, correction=False, _token=_MINTED))


def _writer_binding(body: Mapping, view: SubmissionView, signer: str) -> None:
    """A writer's body is bound to its sid (12.4, section 2)."""
    _need(body.get("publication") == view.publication, "the publication signed is another")
    _need(body.get("round") == view.round, "the round signed is another")
    coordinate = body.get("coordinate_hash")
    _need(formats.is_hex64(coordinate) and formats.is_int(body.get("round"), low=1),
          "the submission's binding is malformed")
    _need(formats.submission_id(signer, view.publication, coordinate, view.round)
          == view.submission_id, "the submission id does not belong to its writer")


def _open_wrap_with_signer(client, wrap: Any, on_success: Callable[[dict], None],
                          on_failure: Callable[[str], None], *,
                          on_invalid: Optional[Callable[[WrapInvalid], None]] = None) -> None:
    """A wrap's plaintext, opened by the account's signer
    (``nip44_decrypt(eph, ct)``). ``on_failure`` gets the signer's short
    reason; ``on_invalid`` a :class:`WrapInvalid` for a wrap that is not
    one or holds something else (without it, ``on_failure`` its words).
    Here and not in crypto.py, which stays pure: it asks a signer, and a
    wrap refused at once is answered on the next turn."""
    invalid = on_invalid or (lambda exc: on_failure(str(exc)))
    if not formats.is_wrap(wrap):
        answer_later(invalid, WrapInvalid("not a key wrap"))
        return

    def opened(text: str) -> None:
        try:
            value = read_wrap_plaintext(text)
        except WrapInvalid as exc:
            invalid(exc)
            return
        on_success(value)

    client.nip44_decrypt(wrap["eph"], wrap["ct"], on_success=opened, on_failure=on_failure)


def open_submission_key(client, verified: VerifiedRevision, recipient: str,
                        on_success: Callable[[bytes], None],
                        on_failure: Callable[[ValueError], None], *,
                        served: Optional[Mapping[str, str]] = None) -> None:
    """The submission key from the wrap for ``recipient`` (``"writer"``,
    ``"proposer"``, ``"redaktion"`` or a contributor's pubkey), the signed
    body's, opened by the account's signer ``client``. ``served`` is the
    wrap the server handed out for it, which must be canonically the same.
    ``on_failure`` gets :class:`NotConfirmed` or :class:`NotOpened`."""
    _made_here(verified, VerifiedRevision, "verified revision")
    wrap = verified.wraps.get(recipient)
    if wrap is None:
        answer_later(on_failure, NotConfirmed("no key was signed for you"))
        return
    if served is not None and not (formats.is_wrap(dict(served)) and formats.canonical_json(
            dict(served)) == formats.canonical_json(wrap)):
        answer_later(on_failure, NotConfirmed("the key served is not the one signed"))
        return

    def opened(plaintext: dict) -> None:
        try:
            secret = _check_submission_key(plaintext, submission_id=verified.submission_id,
                                          p_sub=verified.p_sub)
        except WrapInvalid as exc:
            on_failure(NotConfirmed(str(exc)))
            return
        on_success(secret)

    # The signer said no or did not answer: asking again may change that.
    # The wrap holds something else: it never will.
    _open_wrap_with_signer(client, wrap, opened, lambda reason: on_failure(NotOpened(reason)),
                          on_invalid=lambda exc: on_failure(NotConfirmed(str(exc))))


@dataclass(frozen=True)
class OpenedRevision:
    """A revision opened from a verified envelope: the only source of
    media keys (:class:`MediaKeys`)."""

    verified: VerifiedRevision
    revision: Mapping
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _minted_here(self._token, "opened revision")


def open_envelope(verified: VerifiedRevision, envelope_b64: Optional[str],
                  s_sub: bytes) -> OpenedRevision:
    """The revision in the envelope, when its bytes hash to the signed
    ``envelope_sha256``, it opens with the submission key whose pubkey is
    ``p_sub``, and its plaintext hashes to ``revision_hash``; else
    :class:`NotConfirmed`."""
    _made_here(verified, VerifiedRevision, "verified revision")
    _need(verified.envelope_sha256 is not None and isinstance(envelope_b64, str),
          "no envelope was signed")
    _need(pubkey_of(s_sub) == verified.p_sub, "the key is not the submission's")
    try:
        revision = _open_revision(envelope_b64, s_sub, submission_id=verified.submission_id,
                                 revision=verified.revision,
                                 revision_hash=verified.revision_hash,
                                 envelope_sha256=verified.envelope_sha256)
    except (StandupCryptoError, formats.FormatError) as exc:
        raise NotConfirmed(str(exc)) from None
    return _made(OpenedRevision(verified=verified, revision=revision, _token=_MINTED))


class MediaKeys:
    """Media keys, taken only from opened verified revisions and kept per
    submission and ``cipher_sha256`` (12.4): a file is decrypted with a
    key its own submission's signed revisions name for exactly its bytes.
    Two revisions naming the same bytes with different keys keep both
    (only the right one decrypts them; AES-GCM tells)."""

    def __init__(self) -> None:
        self._keys: Dict[Tuple[str, str], Tuple[bytes, ...]] = {}

    def clear(self) -> None:
        """Sign-out or another account: no key outlives the account."""
        self._keys = {}

    def add(self, opened: OpenedRevision) -> None:
        _made_here(opened, OpenedRevision, "opened revision")
        sid = opened.verified.submission_id
        for entry in opened.revision.get("media", ()):
            where = (sid, entry["cipher_sha256"])
            key = formats.b64decode(entry["key"])
            if key not in self._keys.get(where, ()):
                self._keys[where] = self._keys.get(where, ()) + (key,)

    def keys(self, submission_id: str, cipher_sha256: str) -> Tuple[bytes, ...]:
        return self._keys.get((submission_id, cipher_sha256), ())

    def decrypt(self, submission_id: str, data: bytes, cipher_sha256: str) -> bytes:
        """The file's plaintext, or :class:`NotConfirmed` when no verified
        revision of this submission names these bytes, or they are not
        what it names."""
        keys = self.keys(submission_id, cipher_sha256)
        _need(bool(keys), "no verified revision names this file")
        _need(formats.sha256_hex(data) == cipher_sha256, "the media file is not the one named")
        for key in keys:
            try:
                return _decrypt_media(data, key, cipher_sha256=cipher_sha256)
            except (StandupCryptoError, formats.FormatError):
                continue
        raise NotConfirmed("the media file does not open with the key signed for it")
