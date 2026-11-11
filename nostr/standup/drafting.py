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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from i18n import N_, _

from . import conversation, crypto, formats
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
PICTURE_TYPE = N_("“{name}” can’t be submitted: the editorial team’s app shows only PNG, "
                  "JPEG, GIF, WebP and AVIF pictures. Save it in one of these formats, or "
                  "take it out of the text, to submit.")
PICTURE_TYPE_DETAIL = N_("Its type: {type}")
TOO_MANY_PEOPLE = N_("A submission names at most {count} co-authors.")
COAUTHOR_INVALID = N_("A co-author can’t be named: their account isn’t valid. Remove them "
                      "in Article Details, or add them again, to submit.")
COAUTHOR_INVALID_DETAIL = N_("Not a valid public key: {key}")
ALREADY_SUBMITTED = N_("You have submitted this text to {publication} already.")
MESSAGE_TOO_LONG = N_("A message has at most {count} characters.")
MESSAGE_TOO_LARGE = N_("Your message is too long to send. Shorten it to submit.")
MESSAGE_TOO_LARGE_DETAIL = N_("It takes {size} bytes, and a message can take {limit}.")
# The writer's signer app is asked to sign the message, sealed, over a
# connection whose relays carry less than STANDUP takes (contract 12.5).
MESSAGE_TOO_LONG_FOR_CONNECTION = N_("Your message is too long for the connection to your "
                                     "signer app. Shorten it to submit.")
MESSAGE_TOO_LONG_FOR_CONNECTION_DETAIL = N_("Sealed, it takes {size} bytes. The relays of "
                                            "this connection carry less.")
REQUIRED_MISSING = N_("{publication} asks for {items}. Add them in Article Details to submit.")
# A lookup answered what cannot be used, or an entry names a server this
# app may not ask (a connection that failed says so: failures).
ROUND_UNKNOWN = N_("The server couldn’t tell: try again later.")
# The round step met its bounds (contract 2, bde1274: 100 rounds, 32 MiB
# of answers together).
ROUND_LIMIT = N_("This text was submitted here so often that its earlier submissions can’t "
                 "all be checked.")

# A submission of a round that ended this way may be followed by the next
# round (contract 2); in any other state it is still open there.
CLOSED_STATES = ("declined", "withdrawn")


class Blocker(str):
    """A blocker's words, and the technical fact behind them (``detail``,
    for a tooltip and the accessible description, never the words
    themselves: lesson 27). A total failure has a ``title`` too, what did
    not happen; its words are then the one sentence (lessons 36)."""

    detail: str
    title: str
    error: bool

    def __new__(cls, words: str, detail: str = "", *, title: str = "",
                error: Optional[bool] = None) -> "Blocker":
        made = super().__new__(cls, words)
        made.detail = detail
        made.title = title
        # Said in the error color: a total failure; an end that is no
        # error (submitted on another device) has a title too.
        made.error = bool(title) if error is None else error
        return made


# What a publication's checklist (contract 8.1) can be checked against
# in the work itself, by the ids STANDUP's own writer checks
# (src/services/editorial/submission-work.js, feat/redaktion-f4); any
# other point is "unchecked". Article Details sets all but the language,
# which only an article's own label names.
_CHECKS: Dict[str, Callable[["ArticleDraft"], bool]] = {
    "title": lambda draft: bool(draft.title.strip()),
    "summary": lambda draft: bool(draft.summary.strip()),
    "image": lambda draft: bool(draft.image),
    "tags": lambda draft: bool(draft.tags),
    "lang": lambda draft: bool(draft.lang),
}
_FIXED_IN_DETAILS = ("title", "summary", "image", "tags")


def checklist_results(checklist: Any, draft: "ArticleDraft") -> List[dict]:
    """The publication's checklist with each point's result (5):
    ``ok``, ``missing``, or ``unchecked`` for what the work cannot say."""
    found = []
    for item in checklist if isinstance(checklist, list) else ():
        if not (isinstance(item, dict) and isinstance(item.get("id"), str)
                and isinstance(item.get("label"), str)
                and item.get("level") in ("recommended", "required")):
            continue
        check = _CHECKS.get(item["id"])
        found.append({"id": item["id"], "label": item["label"], "level": item["level"],
                      "result": "unchecked" if check is None
                      else "ok" if check(draft) else "missing"})
    return found


def picture_type_refused(picture: LocalPicture) -> bool:
    """Whether ``picture`` is of a type a revision may not name (5)."""
    return (picture.mime or "").lower() not in formats.PICTURE_MIMES


def invalid_coauthors(draft: "ArticleDraft") -> List[str]:
    """The co-authors whose key is not one (an address imported from
    another app's tags may be anything)."""
    return [pubkey for pubkey, _relay in draft.authors if not formats.is_pubkey(pubkey)]


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

    @property
    def p_sub(self) -> str:
        """The submission's room key (hex), which its messages are wrapped
        to (contract 9.2): the body carries it, checked when it was built
        (submission.submission_body)."""
        return self.body["p_sub"]


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
         writer: str, round_: int, now: int, held: Optional[SubmissionEntry] = None,
         revision: int = 1, change_summary: Optional[str] = None) -> Sealed:
    """The revision (``revision`` of ``round_``: 1; the next one the
    writer hands in, with the one line of its version card,
    ``change_summary``; or the next one after a stored one that did not
    verify, 12.4 at bfa8844), encrypted, and the body and list entry
    naming it. A revision after the first keeps the submission's keys:
    without the list's entry for it (``held``), :class:`HeldNotUsable`. ``servers`` names, per picture's
    ``sha256``, the media servers that hold it; ``held`` is the list's
    entry for this submission, whose secrets it keeps (see
    :func:`_secrets`). Raises FormatError (a
    :class:`~.formats.SecretKeyFound` for a secret key in the text) when
    the revision cannot be built (a picture of a type it may not name,
    a co-author whose key is not one: :func:`blockers` says so first),
    :class:`TooLarge` when it is larger than the publication takes."""
    formats.need(not any(picture_type_refused(p) for p in pictures), "a picture's type")
    sid = submission_id_of(draft, choice, writer, round_)
    if revision > 1 and held is None:
        raise HeldNotUsable("a later revision keeps the keys of its submission")
    s_sub, status_key = _secrets(sid, held)
    number = revision
    revision = formats.build_revision(
        submission_id=sid, revision=number, kind="article", work=_work(draft, pictures),
        rights={"confirmed": choice.rights, "license": None},
        checklist=checklist_results(key.rules.get("checklist"), draft),
        change_summary=change_summary if number > 1 else None,
        thumb=dict(draft.thumb) if draft.thumb else None,
        media=[p.entry(servers.get(p.sha256, ())) for p in pictures], client=CLIENT)
    sealed = crypto.seal_revision(revision, s_sub)
    room = key.rules.get("max_envelope_bytes")
    room = room if formats.is_int(room, low=1) else formats.MAX_ENVELOPE_BYTES
    if len(sealed.envelope) > min(room, formats.MAX_ENVELOPE_BYTES):
        raise TooLarge("larger than the publication takes")
    people = participants_of(draft, writer)
    recipients = {"redaktion": key.pubkey, "writer": writer, **{p: p for p in people}}
    wraps = crypto.wrap_submission_key(sid, s_sub, recipients)
    body = submission_body(
        writer=writer, publication=choice.publication, key_event_id=key.event_id,
        epoch=key.epoch, d=draft.d, round_=round_, revision=number,
        revision_hash=sealed.revision_hash, kind="article", title=draft.title,
        signer=choice.signer, participants=people, p_sub=crypto.pubkey_of(s_sub),
        envelope=sealed.envelope_b64, envelope_sha256=sealed.envelope_sha256,
        wraps=wraps, blobs=[p.blob(servers.get(p.sha256, ())) for p in pictures],
        status_key=status_key, client=CLIENT)
    entry = SubmissionEntry(
        sid=sid, publication=choice.publication, server=key.server, role="writer",
        s_sub=s_sub.hex(), status_key=status_key, title=draft.title, kind="article",
        d=draft.d, round=round_, revision=number, revision_hash=sealed.revision_hash,
        # What a lookup after a restart compares, with no body (12.4).
        envelope_sha256=body["envelope_sha256"], wraps_sha256=crypto.wraps_sha256(body["wraps"]),
        # Once stored, stays stored (13 at 72d5ddc).
        stored=held is not None and held.stored, updated_at=now)
    return Sealed(body=body, entry=entry, s_sub=s_sub)


@dataclass(frozen=True)
class RoundChoice:
    """The round revision 1 of a work goes in (contract 2, "Choosing the
    round and the secrets"): ``round``, or None while a round of it is
    still open there (``open`` names that entry: the work is handed in).
    ``unsent`` is the entry the server has no receipt for that is sent
    again, with its own secrets ("not sent": Erneut or Verwerfen).
    ``elsewhere`` is such an entry whose revision 1 the server refused as
    ``stale_revision``: another device stored that round, so it is never
    sent again (``round`` None) and the list is read again for that
    device's entry (contract 2 at 0456312)."""

    round: Optional[int]
    open: Optional[SubmissionEntry] = None
    unsent: Optional[SubmissionEntry] = None
    elsewhere: Optional[SubmissionEntry] = None


def _never_refused(_entry: SubmissionEntry) -> bool:
    return False


def choose_round(entries: Sequence[SubmissionEntry],
                 states: Mapping[str, Optional[str]],
                 refused: Callable[[SubmissionEntry], bool] = _never_refused) -> RoundChoice:
    """The round for the writer's ``entries`` of one work at one
    publication, given ``states``: per entry's sid, the state the server
    reports (None: it has no receipt, the entry was never stored). Every
    entry must have been asked about (KeyError otherwise): a round is
    never taken from the list alone. ``refused(entry)``: whether the
    entry's revision 1 was refused as ``stale_revision`` (held)."""
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
        if refused(entry):
            return RoundChoice(None, elsewhere=entry)
        return RoundChoice(entry.round, unsent=entry)
    return RoundChoice(closed + 1)


def _signs_anything(_unsigned: dict) -> bool:
    return True


def blockers(draft: ArticleDraft, *, writer: str, rights: bool, rights_required: bool,
             private_names: Sequence[str] = (), message: str = "",
             submitted_to: str = "", checklist: Any = (), publication: str = "",
             signable: Callable[[dict], bool] = _signs_anything) -> List[str]:
    """Why the article cannot be submitted as it is, in the person's
    words (translated), most pressing first; empty when it can.
    ``private_names`` are the names of its pictures that are private in
    the Media Library; ``submitted_to`` names the publication when this
    text is submitted there already and still open; ``checklist`` is the
    one of ``publication``, whose required points Article Details can
    set must be there (as STANDUP's writer asks). ``signable(unsigned)``
    says whether the writer's signer can sign an event (the message's
    seal; signer_limits.signable)."""
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
    for picture in [p for p in draft.pictures if picture_type_refused(p)][:1]:
        found.append(Blocker(_(PICTURE_TYPE).format(name=picture.name),
                             _(PICTURE_TYPE_DETAIL).format(type=picture.mime or "?")))
    if len(draft.pictures) > formats.MAX_BLOBS:
        found.append(_(TOO_MANY_PICTURES).format(count=formats.MAX_BLOBS))
    for key in invalid_coauthors(draft)[:1]:
        found.append(Blocker(_(COAUTHOR_INVALID),
                             _(COAUTHOR_INVALID_DETAIL).format(key=key[:100])))
    if len(participants_of(draft, writer)) > formats.MAX_PARTICIPANTS:
        found.append(_(TOO_MANY_PEOPLE).format(count=formats.MAX_PARTICIPANTS))
    size = conversation.message_size(message.strip(), sender=writer)
    if len(message) > formats.MAX_MESSAGE_CHARS:
        found.append(_(MESSAGE_TOO_LONG).format(count=formats.MAX_MESSAGE_CHARS))
    elif message.strip() and size > formats.MAX_RUMOR_BYTES:
        # Letters outside Latin script take more bytes: within the
        # characters it would still fail after the receipt, where Send
        # Again could never help (RG-M2b 11).
        found.append(Blocker(_(MESSAGE_TOO_LARGE), _(MESSAGE_TOO_LARGE_DETAIL).format(
            size=size, limit=formats.MAX_RUMOR_BYTES)))
    elif message.strip() and not signable(conversation.seal_measure(size)):
        # Within what STANDUP takes, but its seal is more than the relays
        # of the writer's signer connection carry: the signer would never
        # be asked (the client refuses it), and Send Again could not help.
        found.append(Blocker(_(MESSAGE_TOO_LONG_FOR_CONNECTION),
                             _(MESSAGE_TOO_LONG_FOR_CONNECTION_DETAIL).format(size=size)))
    asked = [item["label"] for item in checklist_results(checklist, draft)
             if item["level"] == "required" and item["result"] == "missing"
             and item["id"] in _FIXED_IN_DETAILS]
    if asked:
        found.append(_(REQUIRED_MISSING).format(publication=publication, items=", ".join(asked)))
    if submitted_to:
        found.append(_(ALREADY_SUBMITTED).format(publication=submitted_to))
    if rights_required and not rights:
        found.append(_(RIGHTS_MISSING))
    return found


__all__ = [
    "ALREADY_SUBMITTED", "ArticleDraft", "Blocker", "CLIENT", "CLOSED_STATES", "Choice",
    "EncryptedPicture", "HeldNotUsable", "LocalPicture", "RoundChoice", "Sealed", "TooLarge",
    "blockers", "checklist_results", "choose_round", "encrypt_pictures", "invalid_coauthors",
    "participants_of", "picture_type_refused", "seal", "submission_id_of",
]
