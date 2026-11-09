# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A submission made of an article (editorial contract 5, 7, 12.4; plan
10.2). Pure: no Qt, no network.

:class:`ArticleDraft` is the article as the window hands it over: its
details, its Markdown, and every picture that is on this computer only,
named in the text by its asset key (``myeditor-asset:<sha256>``).
Pictures already on the web stay links. :func:`encrypt_pictures` turns
each local picture into an SMF1 file under a key of its own (7), to be
uploaded to the writer's own media servers; :func:`seal` then builds
the revision with the pictures named ``standup-media:<cipher_sha256>``,
encrypts it under a fresh submission key, wraps that key to the
Redaktion's verified epoch key, the writer and each co-author, and
gives the body of ``POST /api/submissions`` together with the entry the
writer's list keeps before it is sent (12.4, "No answer after
sending").

:func:`blockers` says, in the person's words, what keeps the sheet's
Einreichen dimmed: a missing title, a secret key in the text, a private
picture of the Media Library (plan 10.2), the contract's limits (15),
the rights when the publication requires them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from i18n import N_, _

from . import crypto, formats
from .formats import FormatError
from .entries import SubmissionEntry
from .submission import submission_body
from .trust import RedaktionKey

# The client named in the revision and the body (at most 64 characters).
CLIENT = "MyEditor"

TITLE_MISSING = N_("Give the article a title to submit it.")
TITLE_TOO_LONG = N_("A title has at most {count} characters.")
RIGHTS_MISSING = N_("Confirm the rights to submit.")
PRIVATE_PICTURE = N_("“{name}” is private in your Media Library. Share it, or take it "
                     "out of the text, to submit.")
TOO_MANY_PICTURES = N_("A submission has at most {count} pictures.")
TOO_MANY_PEOPLE = N_("A submission names at most {count} co-authors.")
ALREADY_SUBMITTED = N_("You have submitted this text to {publication} already.")
MESSAGE_TOO_LONG = N_("A message has at most {count} characters.")
ROUND_UNKNOWN = N_("Whether you have submitted this text before couldn’t be checked. "
                   "Try again later.")

# A submission of a round that ended this way may be followed by the next
# round (contract 2); in any other state it is still open there.
CLOSED_STATES = ("declined", "withdrawn")


class HeldNotUsable(ValueError):
    """The list holds an entry for the submission whose secrets this app
    cannot take (another role, keys missing): nothing is built."""


class TooLarge(FormatError):
    """The encrypted revision is larger than the publication takes."""


@dataclass(frozen=True)
class LocalPicture:
    """A picture on this computer only: ``key`` is how the text names it
    (its asset key), ``data`` its bytes."""

    key: str
    name: str
    mime: str
    alt: str = ""
    data: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class ArticleDraft:
    """The article to submit. ``image`` is the cover: a web address, a
    local picture's key, or ``""``. ``authors`` are the co-authors,
    ``(pubkey, relay)`` each, as Article Details keeps them."""

    d: str
    title: str
    summary: str
    content: str
    tags: Tuple[str, ...] = ()
    image: str = ""
    authors: Tuple[Tuple[str, str], ...] = ()
    pictures: Tuple[LocalPicture, ...] = ()
    thumb: Optional[Mapping[str, str]] = None
    lang: str = ""
    published_at: Optional[int] = None


@dataclass(frozen=True)
class Choice:
    """What the person chose in the sheet."""

    publication: str              # the address
    name: str                     # the publication's name, for the words
    signer: str                   # "writer" or "publication" (who publishes)
    rights: bool
    message: str = ""


@dataclass(frozen=True)
class EncryptedPicture:
    """A local picture as an SMF1 file (7), ready to upload."""

    key: str
    name: str
    mime: str
    alt: str
    file_key: bytes = field(repr=False)
    encryptor: crypto.MediaEncryptor = field(repr=False, compare=False)
    cipher: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return self.encryptor.cipher_sha256

    @property
    def ref(self) -> str:
        return formats.media_ref(self.sha256)

    def entry(self, servers: Sequence[str]) -> dict:
        return crypto.media_entry(key=self.file_key, encryptor=self.encryptor,
                                  name=self.name, mime=self.mime, alt=self.alt,
                                  servers=list(servers)[:formats.MAX_BLOB_SERVERS])

    def blob(self, servers: Sequence[str]) -> dict:
        return {"sha256": self.sha256, "size": self.encryptor.cipher_size,
                "servers": list(servers)[:formats.MAX_BLOB_SERVERS]}


@dataclass(frozen=True)
class Sealed:
    """What is sent (``body``) and what the list keeps before (``entry``)."""

    body: Dict[str, Any]
    entry: SubmissionEntry = field(repr=False)
    s_sub: bytes = field(repr=False)

    @property
    def submission_id(self) -> str:
        return self.body["submission_id"]


def encrypt_pictures(pictures: Sequence[LocalPicture]) -> Tuple[EncryptedPicture, ...]:
    """Each picture under a fresh 32-byte key, as one SMF1 file."""
    done = []
    for picture in pictures:
        key = crypto.new_secret_key()
        encryptor = crypto.MediaEncryptor(key)
        cipher = encryptor.update(picture.data) + encryptor.finish()
        done.append(EncryptedPicture(key=picture.key, name=picture.name, mime=picture.mime,
                                     alt=picture.alt, file_key=key, encryptor=encryptor,
                                     cipher=cipher))
    return tuple(done)


def participants_of(draft: ArticleDraft, writer: str) -> List[str]:
    """Who takes part besides the writer (plan D15): the co-authors, each
    once."""
    return list(dict.fromkeys(pk for pk, _relay in draft.authors if pk != writer))


def _work(draft: ArticleDraft, pictures: Sequence[EncryptedPicture]) -> dict:
    refs = {picture.key: picture.ref for picture in pictures}
    content = draft.content
    for key, ref in refs.items():
        content = content.replace(key, ref)
    image = refs.get(draft.image, draft.image) or None
    return {"d": draft.d, "title": draft.title, "summary": draft.summary, "image": image,
            "tags": list(draft.tags), "lang": draft.lang, "published_at": draft.published_at,
            "content": content,
            "authors": [{"pubkey": pk, "relay": relay} for pk, relay in draft.authors],
            "extra_tags": []}


def submission_id_of(draft: ArticleDraft, choice: Choice, writer: str, round_: int) -> str:
    """The ``submission_id`` of ``draft`` in ``round_`` at the chosen
    publication (contract 2)."""
    return formats.submission_id(writer, choice.publication,
                                 formats.coordinate_hash(writer, draft.d), round_)


def _secrets(sid: str, held: Optional[SubmissionEntry]) -> Tuple[bytes, str]:
    """``s_sub`` and the status key of ``sid``: the ones the list holds
    for it, or new ones when it holds none. One ``submission_id`` has one
    set of secrets for ever (contract 2): new ones for a sid already
    named could take the place of the keys of a revision the server
    stored. :class:`HeldNotUsable` for an entry of another sid, or one
    without both."""
    if held is None:
        return crypto.new_secret_key(), crypto.new_status_key()
    if held.sid != sid or held.role != "writer" or not formats.is_hex64(held.s_sub) \
            or not formats.is_hex64(held.status_key):
        raise HeldNotUsable("the entry held is not this submission's")
    return bytes.fromhex(held.s_sub), held.status_key


def seal(draft: ArticleDraft, choice: Choice, key: RedaktionKey,
         pictures: Sequence[EncryptedPicture], servers: Mapping[str, Sequence[str]], *,
         writer: str, round_: int, now: int, held: Optional[SubmissionEntry] = None) -> Sealed:
    """The revision (revision 1 of ``round_``), encrypted, and the body
    and list entry naming it. ``servers`` names, per picture's
    ``sha256``, the media servers that hold it; ``held`` is the list's
    entry for this submission, whose secrets it keeps (see
    :func:`_secrets`). Raises FormatError (a
    :class:`~.formats.SecretKeyFound` for a secret key in the text) when
    the revision cannot be built, :class:`TooLarge` when it is larger
    than the publication takes."""
    sid = submission_id_of(draft, choice, writer, round_)
    s_sub, status_key = _secrets(sid, held)
    revision = formats.build_revision(
        submission_id=sid, revision=1, kind="article", work=_work(draft, pictures),
        rights={"confirmed": choice.rights, "license": None},
        thumb=dict(draft.thumb) if draft.thumb else None,
        media=[p.entry(servers.get(p.sha256, ())) for p in pictures], client=CLIENT)
    sealed = crypto.seal_revision(revision, s_sub)
    room = key.rules.get("max_envelope_bytes")
    room = room if formats.is_int(room, low=1) else formats.MAX_ENVELOPE_BYTES
    if len(sealed.envelope) > min(room, formats.MAX_ENVELOPE_BYTES):
        raise TooLarge("larger than the publication takes")
    people = participants_of(draft, writer)
    recipients = {"redaktion": key.pubkey, "writer": writer, **{p: p for p in people}}
    body = submission_body(
        writer=writer, publication=choice.publication, key_event_id=key.event_id,
        epoch=key.epoch, d=draft.d, round_=round_, revision=1,
        revision_hash=sealed.revision_hash, kind="article", title=draft.title,
        signer=choice.signer, participants=people, p_sub=crypto.pubkey_of(s_sub),
        envelope=sealed.envelope_b64, envelope_sha256=sealed.envelope_sha256,
        wraps=crypto.wrap_submission_key(sid, s_sub, recipients),
        blobs=[p.blob(servers.get(p.sha256, ())) for p in pictures],
        status_key=status_key, client=CLIENT)
    entry = SubmissionEntry(
        sid=sid, publication=choice.publication, server=key.server, role="writer",
        s_sub=s_sub.hex(), status_key=status_key, title=draft.title, kind="article",
        d=draft.d, round=round_, revision=1, revision_hash=sealed.revision_hash,
        updated_at=now)
    return Sealed(body=body, entry=entry, s_sub=s_sub)


@dataclass(frozen=True)
class RoundChoice:
    """The round revision 1 of a work goes in (contract 2, "Choosing the
    round and the secrets"): ``round``, or None while a round of it is
    still open there (``open`` names that entry: the work is handed in).
    ``unsent`` is the entry the server has no receipt for that is sent
    again, with its own secrets ("not sent": Erneut or Verwerfen)."""

    round: Optional[int]
    open: Optional[SubmissionEntry] = None
    unsent: Optional[SubmissionEntry] = None


def choose_round(entries: Sequence[SubmissionEntry],
                 states: Mapping[str, Optional[str]]) -> RoundChoice:
    """The round for the writer's ``entries`` of one work at one
    publication, given ``states``: per entry's sid, the state the server
    reports (None: it has no receipt, the entry was never stored). Every
    entry must have been asked about (KeyError otherwise): a round is
    never taken from the list alone."""
    asked = [(entry, states[entry.sid]) for entry in entries]
    still_open = [entry for entry, state in asked
                  if state is not None and state not in CLOSED_STATES]
    if still_open:
        return RoundChoice(None, open=max(still_open, key=lambda e: e.round))
    closed = max((entry.round for entry, state in asked if state is not None), default=0)
    # An entry of a round before the last closed one can never be sent:
    # the server expects the next round.
    unsent = [entry for entry, state in asked if state is None and entry.round > closed]
    if unsent:
        entry = min(unsent, key=lambda e: e.round)
        return RoundChoice(entry.round, unsent=entry)
    return RoundChoice(closed + 1)


def blockers(draft: ArticleDraft, *, writer: str, rights: bool, rights_required: bool,
             private_names: Sequence[str] = (), message: str = "",
             submitted_to: str = "") -> List[str]:
    """Why the article cannot be submitted as it is, in the person's
    words (translated), most pressing first; empty when it can.
    ``private_names`` are the names of its pictures that are private in
    the Media Library; ``submitted_to`` names the publication when this
    text is submitted there already and still open."""
    found: List[str] = []
    title = draft.title.strip()
    if not title:
        found.append(_(TITLE_MISSING))
    elif len(draft.title) > formats.MAX_TITLE_CHARS:
        found.append(_(TITLE_TOO_LONG).format(count=formats.MAX_TITLE_CHARS))
    if formats.secret_keys_in_texts([draft.title, draft.summary, draft.content, message]):
        found.append(_(formats.SECRET_KEY_IN_SUBMISSION))
    for name in private_names[:1]:
        found.append(_(PRIVATE_PICTURE).format(name=name))
    if len(draft.pictures) > formats.MAX_BLOBS:
        found.append(_(TOO_MANY_PICTURES).format(count=formats.MAX_BLOBS))
    if len(participants_of(draft, writer)) > formats.MAX_PARTICIPANTS:
        found.append(_(TOO_MANY_PEOPLE).format(count=formats.MAX_PARTICIPANTS))
    if len(message) > formats.MAX_MESSAGE_CHARS:
        found.append(_(MESSAGE_TOO_LONG).format(count=formats.MAX_MESSAGE_CHARS))
    if submitted_to:
        found.append(_(ALREADY_SUBMITTED).format(publication=submitted_to))
    if rights_required and not rights:
        found.append(_(RIGHTS_MISSING))
    return found


__all__ = [
    "ALREADY_SUBMITTED", "ArticleDraft", "HeldNotUsable", "CLIENT", "CLOSED_STATES", "Choice",
    "EncryptedPicture", "LocalPicture", "RoundChoice", "Sealed", "TooLarge", "blockers",
    "choose_round", "encrypt_pictures", "participants_of", "seal", "submission_id_of",
]
