# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The STANDUP answers other than the submission views (editorial
contract 12.1, 12.3 and 12.4): one typed value per answer, parsed once
here and trusted after. A malformed answer raises
:class:`~.formats.FormatError`, which the client reports as a bad
response; nothing of it reaches the app half read. The submission views,
timelines and receipts are in ``views.py``.

Pure: no Qt, no network, no clock.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from ..http_answers import clean_text
from . import formats, views
from .formats import FormatError, need
from .readers import (
    read_address,
    read_bool,
    read_hex64,
    read_int,
    read_list,
    read_object,
    read_one_of,
    read_optional_hex64,
    read_optional_int,
    read_optional_text,
    read_optional_wrap,
    read_pubkey,
    read_text,
)
from .servers import usable_server_url

MEMBER_STATUSES = ("invited", "active")
SEES = ("all", "since_join")
INVITED_ROLES = ("redaktion", "curator")

_MAX_LISTINGS = 500
_MAX_MEMBERS = 1_000
_MAX_RELAYS = 20
_MAX_KEYRING_PAGES = 1_000
_MAX_PAGE_CIPHERTEXT = 64 * 1024


# --------------------------------------------------------------------------- #
# Sessions and the instance (12.1, 12.3)                                       #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Session:
    token: str = field(repr=False)
    pubkey: str
    expires_at: int


def parse_session(data: Mapping[str, Any], *, signed_by: Optional[str],
                  now: float) -> Session:
    """``POST /api/session``: a token for the person who signed, still
    valid. A token for someone else would act as someone else."""
    token = read_hex64(data.get("token"), "the session token")
    pubkey = read_hex64(data.get("pubkey"), "the session's person")
    need(isinstance(signed_by, str) and hmac.compare_digest(pubkey, signed_by),
         "the session is for another person")
    expires_at = read_int(data.get("expires_at"), "the session's end")
    need(expires_at > now, "the session has expired already")
    return Session(token=token, pubkey=pubkey, expires_at=expires_at)


@dataclass(frozen=True)
class NoticeAccount:
    """STANDUP Bote, the account notices come from (14)."""

    pubkey: str
    npub: str
    nip05: Optional[str]


@dataclass(frozen=True)
class Instance:
    """``GET /api/editorial/instance``. ``server`` is None when the
    operator has not set the address explicitly (14): never guessed."""

    operator: str
    server: Optional[str]
    web_app_by: str
    bote: Optional[NoticeAccount]
    inbox_relays: Tuple[str, ...]


def parse_instance(data: Mapping[str, Any]) -> Instance:
    bote = data.get("bote")
    if bote is not None:
        bote = read_object(bote, "the notice account")
        bote = NoticeAccount(pubkey=read_pubkey(bote.get("pubkey"), "the notice account"),
                             npub=clean_text(bote.get("npub")),
                             nip05=clean_text(bote.get("nip05")) or None)
    relays = read_list(data.get("inbox_relays", []), "the inbox relays", limit=_MAX_RELAYS)
    return Instance(operator=clean_text(data.get("operator")),
                    server=usable_server_url(data.get("server")) or None,
                    web_app_by=clean_text(data.get("web_app_by")),
                    bote=bote,
                    inbox_relays=tuple(read_text(r, "an inbox relay") for r in relays))


def parse_notice_pubkey(data: Any) -> str:
    """``/.well-known/nostr.json?name=bote`` (not in the ``ok`` envelope)."""
    names = read_object(read_object(data, "the answer").get("names"), "the names")
    return read_pubkey(names.get("bote"), "the notice account's key")


# --------------------------------------------------------------------------- #
# Publications (12.3)                                                          #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Listing:
    """An open publication as the server lists it. Nothing here is
    trusted yet: ``trust`` verifies the key event, and the rules that
    count are the ones it carries, signed by the publication."""

    address: str
    name: str
    picture: Optional[str]
    key_event: Mapping[str, Any]


def parse_listings(data: Mapping[str, Any]) -> Tuple[Listing, ...]:
    """``GET /api/submissions/destinations``. An entry without a valid
    address or key event is not a publication one could submit to and
    is left out; everything else of a listing is checked by ``trust``."""
    found = []
    for item in read_list(data.get("publications"), "the publications", limit=_MAX_LISTINGS):
        if not isinstance(item, dict) or not isinstance(item.get("key_event"), dict):
            continue
        try:
            address = read_address(item.get("address"))
        except FormatError:
            continue
        picture = item.get("picture")
        found.append(Listing(address=address, name=clean_text(item.get("name")),
                             picture=picture if isinstance(picture, str)
                             and picture.startswith("https://") else None,
                             key_event=item["key_event"]))
    return tuple(found)


@dataclass(frozen=True)
class SiteSettings:
    """``GET .../settings`` (``members_count`` too) and the answer of
    ``PUT .../settings`` (without it)."""

    key_event: Optional[Mapping[str, Any]]
    settings_event: Optional[Mapping[str, Any]]
    members_count: Optional[int]


def parse_site_settings(data: Mapping[str, Any]) -> SiteSettings:
    def event(name: str) -> Optional[Mapping[str, Any]]:
        value = data.get(name)
        return None if value is None else read_object(value, f"the {name}")

    return SiteSettings(key_event=event("key_event"), settings_event=event("settings_event"),
                        members_count=read_optional_int(data.get("members_count"),
                                                        "the number of members"))


@dataclass(frozen=True)
class Member:
    pubkey: str
    role: str
    status: str
    sees: str
    invited_by: Optional[str]
    accepted_at: Optional[int]
    away_until: Optional[int]
    releaser: bool


def parse_member(value: Any) -> Member:
    data = read_object(value, "a member")
    return Member(
        pubkey=read_hex64(data.get("pubkey"), "a member's key"),
        role=read_one_of(data.get("role"), views.MEMBER_ROLES, "a member's role"),
        status=read_one_of(data.get("status"), MEMBER_STATUSES, "a member's status"),
        sees=read_one_of(data.get("sees"), SEES, "what a member sees"),
        invited_by=read_optional_hex64(data.get("invited_by"), "who invited the member"),
        accepted_at=read_optional_int(data.get("accepted_at"), "when the member accepted"),
        away_until=read_optional_int(data.get("away_until"), "until when the member is away"),
        releaser=read_bool(data.get("releaser"), "whether the member releases"))


def parse_members(data: Mapping[str, Any]) -> Tuple[Member, ...]:
    return tuple(parse_member(m) for m in read_list(data.get("members"), "the members",
                                                    limit=_MAX_MEMBERS))


def parse_one_member(data: Mapping[str, Any]) -> Member:
    """``{"member": Member}``, the answer of every membership change."""
    return parse_member(data.get("member"))


def parse_epoch_number(data: Mapping[str, Any]) -> int:
    """``{"epoch": n}``: the epoch now current."""
    return read_int(data.get("epoch"), "the epoch", low=1)


@dataclass(frozen=True)
class EpochKey:
    """One epoch of ``GET .../keys``: its key and the member's wrap."""

    epoch: int
    pubkey: str
    key_event: Mapping[str, Any]
    wrap: Optional[Mapping[str, str]]


@dataclass(frozen=True)
class EpochKeys:
    """``GET .../keys``: every epoch, and the member's own history chain
    (12.3: addressed to the member; None for "since joining")."""

    epochs: Tuple[EpochKey, ...]
    history: Optional[Mapping[str, str]]


def parse_epoch_keys(data: Mapping[str, Any]) -> EpochKeys:
    epochs = []
    for value in read_list(data.get("epochs"), "the epochs"):
        item = read_object(value, "an epoch")
        epochs.append(EpochKey(epoch=read_int(item.get("epoch"), "the epoch", low=1),
                               pubkey=read_pubkey(item.get("pubkey"), "the epoch's key"),
                               key_event=read_object(item.get("key_event"), "the key event"),
                               wrap=read_optional_wrap(item.get("wrap"), "the epoch's wrap")))
    return EpochKeys(epochs=tuple(epochs),
                     history=read_optional_wrap(data.get("history"), "the history chain"))


@dataclass(frozen=True)
class Keyring:
    """``GET /api/me/keyring``: encrypted pages, opened by the account."""

    version: int
    pages: Tuple[str, ...]


def parse_keyring(data: Mapping[str, Any]) -> Keyring:
    pages = read_list(data.get("pages"), "the keyring's pages", limit=_MAX_KEYRING_PAGES)
    return Keyring(version=parse_keyring_version(data),
                   pages=tuple(read_text(p, "a keyring page", limit=_MAX_PAGE_CIPHERTEXT)
                               for p in pages))


def parse_keyring_version(data: Mapping[str, Any]) -> int:
    """``{"version": n}``, also the answer of ``PUT /api/me/keyring``."""
    return read_int(data.get("version"), "the keyring's version")


# --------------------------------------------------------------------------- #
# Submissions (12.4)                                                           #
# --------------------------------------------------------------------------- #

def parse_acknowledged(data: Mapping[str, Any]) -> None:
    """``{"ok": true}`` and nothing else to read (a reminder sent)."""
    return None


def parse_seq(data: Mapping[str, Any]) -> int:
    """``{"seq": n}``: the server has the message, at ``seq``."""
    return read_int(data.get("seq"), "the message's seq", low=1)


def parse_view(data: Mapping[str, Any]) -> views.SubmissionView:
    """``{"submission": View}``, the answer of most signed actions."""
    return views.parse_view(data.get("submission"))


@dataclass(frozen=True)
class Recorded:
    """``{"submission": View, "seq": n}``: the action is in the timeline."""

    submission: views.SubmissionView
    seq: int


def parse_recorded(data: Mapping[str, Any]) -> Recorded:
    return Recorded(submission=parse_view(data), seq=parse_seq(data))


def parse_views(data: Mapping[str, Any]) -> Tuple[views.SubmissionView, ...]:
    """``{"items": [View]}`` (``GET /api/me/submissions``)."""
    return tuple(views.parse_view(i) for i in read_list(data.get("items"), "the submissions"))


@dataclass(frozen=True)
class SubmissionPage:
    """One page of a publication's submissions; ``next`` asks for more."""

    items: Tuple[views.SubmissionView, ...]
    next: Optional[str]


def parse_submission_page(data: Mapping[str, Any]) -> SubmissionPage:
    return SubmissionPage(items=parse_views(data),
                          next=read_optional_text(data.get("next"), "the next page"))


@dataclass(frozen=True)
class PublicationRole:
    address: str
    role: str
    name: str


@dataclass(frozen=True)
class Invitation:
    address: str
    name: str
    role: str
    sees: str
    invited_by: Optional[str]
    invited_at: Optional[int]


@dataclass(frozen=True)
class Editorial:
    """``GET /api/me/editorial``: whose turn, and where the person is a
    member or invited to be one."""

    turns: Tuple[views.SubmissionView, ...]
    mine: int
    publications: Tuple[PublicationRole, ...]
    invitations: Tuple[Invitation, ...]


def parse_editorial(data: Mapping[str, Any]) -> Editorial:
    counts = read_object(data.get("counts", {}), "the counts")
    publications = []
    for value in read_list(data.get("publications", []), "the publications"):
        item = read_object(value, "a publication")
        publications.append(PublicationRole(
            address=read_address(item.get("address")),
            role=read_one_of(item.get("role"), views.MEMBER_ROLES, "the role"),
            name=clean_text(item.get("name"))))
    invitations = []
    for value in read_list(data.get("invitations", []), "the invitations"):
        item = read_object(value, "an invitation")
        invitations.append(Invitation(
            address=read_address(item.get("address")),
            name=clean_text(item.get("name")),
            role=read_one_of(item.get("role"), INVITED_ROLES, "the invited role"),
            sees=read_one_of(item.get("sees"), SEES, "what the invitee sees"),
            invited_by=read_optional_hex64(item.get("invited_by"), "who invited"),
            invited_at=read_optional_int(item.get("invited_at"), "when")))
    return Editorial(
        turns=tuple(views.parse_view(i) for i in read_list(data.get("turns"), "the turns")),
        mine=read_int(counts.get("mine", 0), "the number of turns"),
        publications=tuple(publications), invitations=tuple(invitations))


@dataclass(frozen=True)
class SubmissionDetail:
    """``GET /api/submissions/<sid>``: ``revisions`` holds the latest
    revision, or only the one asked for; ``timeline`` the conversation's
    items and ``internal`` the internal notes, each channel in its own
    ``seq`` order (the server sends notes to every member, a writer who
    is one among them; the writer's side shows none); ``more`` says that
    items wait beyond this answer's budget (ask again with ``since`` and
    ``since_internal``, contract 12.4 and 15, feat/redaktion ceac858)."""

    submission: views.SubmissionView
    my_role: str
    revisions: Tuple[views.RevisionRecord, ...]
    timeline: Tuple[views.TimelineItem, ...]
    members: Tuple[Member, ...]
    more: bool = False
    internal: Tuple[views.TimelineItem, ...] = ()


def parse_submission_detail(data: Mapping[str, Any]) -> SubmissionDetail:
    role = read_one_of(data.get("my_role"), views.MEMBER_ROLES + views.PARTICIPANT_ROLES,
                       "my role")
    channels = views.parse_channels(data.get("timeline", []))
    return SubmissionDetail(
        submission=parse_view(data),
        my_role=role,
        revisions=tuple(views.parse_revision_record(r)
                        for r in read_list(data.get("revisions", []), "the revisions")),
        timeline=channels["conversation"], internal=channels["internal"],
        members=tuple(parse_member(m) for m in read_list(data.get("members", []),
                                                         "the members", limit=_MAX_MEMBERS)),
        more=_read_more(data))


def _read_more(data: Mapping[str, Any]) -> bool:
    more = data.get("more", False)
    need(isinstance(more, bool), "more is true or false")
    return more


@dataclass(frozen=True)
class LookupAnswer:
    """``POST /api/submissions/lookup``, by status key: ``found`` holds
    what the server has; a key in neither ``found`` nor ``unreadable``
    is one the server does not know. ``unreadable`` holds the keys whose
    answer could not be read: for them the server has *something*, so
    "not stored" must not be concluded (12.4)."""

    found: Mapping[str, views.LookupItem]
    unreadable: FrozenSet[str]


def parse_lookup(data: Mapping[str, Any], *, asked: Mapping[str, str]) -> LookupAnswer:
    """``asked`` maps each status key's hash to the key. An item that
    cannot be read does not hide the others; when it does not even say
    whose it is, every key without an answer counts as unreadable."""
    found: Dict[str, views.LookupItem] = {}
    unreadable = set()
    anonymous = False
    for raw in read_list(data.get("items"), "the lookup answer",
                         limit=formats.MAX_LOOKUP_ITEMS):
        key = _asked_key(raw, asked)
        try:
            item = views.parse_lookup_item(raw)
        except FormatError:
            if key is None:
                anonymous = True
            else:
                unreadable.add(key)
            continue
        if key is not None:
            found[key] = item
    if anonymous:
        unreadable.update(k for k in asked.values() if k not in found)
    return LookupAnswer(found=found, unreadable=frozenset(unreadable - set(found)))


def _asked_key(raw: Any, asked: Mapping[str, str]) -> Optional[str]:
    """The status key an item names, compared in constant time."""
    claimed = raw.get("status_key_hash") if isinstance(raw, dict) else None
    if not formats.is_hex64(claimed):
        return None
    return next((key for digest, key in asked.items()
                 if hmac.compare_digest(digest, claimed)), None)
