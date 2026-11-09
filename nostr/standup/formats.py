# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shapes both apps agree on: canonical JSON, identifiers, the
revision, the limits (editorial contract sections 1, 2, 5, 13 and 15).

Pure: no Qt, no network, no clock. Everything a server or another app
sends is read with :func:`read_revision` and the ``is_*`` checks, which
raise :class:`FormatError` (a ValueError) for anything malformed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from coincurve import PublicKeyXOnly

from i18n import N_

from .. import refs

# --------------------------------------------------------------------------- #
# Limits (section 15, and where else the contract names one)                  #
# --------------------------------------------------------------------------- #

CONTRACT_VERSION = 1

MAX_ENVELOPE_BYTES = 4 * 1024 * 1024        # 6: envelope_bytes
MAX_WRAP_EVENT_BYTES = 96 * 1024            # 15: one conversation item, serialized
MAX_MESSAGE_CHARS = 16_000                  # 9.1: message text
MAX_RUMOR_BYTES = 32_768                    # 9.1: the serialized rumor
MAX_TITLE_CHARS = 300                       # 12.4
MAX_PARTICIPANTS = 20                       # 15
MAX_LOOKUP_ITEMS = 50                       # 12.4 lookup
MAX_THUMB_BYTES = 24 * 1024                 # 5: thumb, at most 24 KB
MAX_PEAKS = 512                             # 5: audio peaks

# Media files (7).
MEDIA_CHUNK_SIZE = 1_048_576                # new files
MEDIA_CHUNK_MIN = 4_096                     # readers accept from here
MEDIA_CHUNK_MAX = 8_388_608                 # to here
MEDIA_REF_PREFIX = "standup-media:"

# Identifier prefixes and fixed strings (2, 3, 8, 13).
SUBMISSION_ID_PREFIX = "standup-sub:v1:"
CONTENT_KEY_SALT = b"standup-editorial-v1"
CONTENT_KEY_INFO = b"content"
ENVELOPE_AAD_PREFIX = "standup-envelope:v1:"
KEY_EVENT_D_PREFIX = "standup:redaktion-key:"
SETTINGS_EVENT_D_PREFIX = "standup:redaktion-settings:"
SUBMISSIONS_LIST_D = "einundzwanzig:submissions"
READ_STATE_D_PREFIX = "einundzwanzig:read-state:v1:"

SITE_KIND = 30004
ARTICLE_KIND = 30023
APP_DATA_KIND = 30078

KINDS = ("article", "audio")
AUDIO_MEDIUMS = ("podcast", "music", "audiobook")
CHECK_LEVELS = ("recommended", "required")
CHECK_RESULTS = ("ok", "missing", "unchecked")
THUMB_MIMES = ("image/webp", "image/jpeg")
SIGNERS = ("writer", "publication")
ROLES = ("writer", "contributor")

# The JSON integers both apps write exactly (1).
_MAX_SAFE_INTEGER = 2 ** 53 - 1

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX128 = re.compile(r"\A[0-9a-f]{128}\Z")


class FormatError(ValueError):
    """Something that is not what the contract says it is."""


# What the person is told when the text to submit holds a secret key
# (5: refused with an explanation before anything is encrypted).
SECRET_KEY_IN_SUBMISSION = N_(
    "It contains a secret key, and MyEditor never sends a secret key to a publication. "
    "Remove the key and try again.")


# --------------------------------------------------------------------------- #
# Encodings (1)                                                                #
# --------------------------------------------------------------------------- #

def _check_canonical(value: Any) -> None:
    """Refuse what canonical JSON cannot hold: floats (NaN and -0 among
    them), integers beyond 2^53 - 1, keys that are not ASCII strings.
    Walked with a list, so deep nesting cannot exhaust the stack."""
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, (bool, str)):
            continue
        if isinstance(item, int):
            if abs(item) > _MAX_SAFE_INTEGER:
                raise FormatError("an integer beyond 2^53 - 1")
            continue
        if isinstance(item, float):
            raise FormatError("a float, which canonical JSON never holds")
        if isinstance(item, dict):
            for key in item:
                if not isinstance(key, str) or not key.isascii():
                    raise FormatError("a key that is not an ASCII string")
            pending.extend(item.values())
            continue
        if isinstance(item, (list, tuple)):
            pending.extend(item)
            continue
        raise FormatError(f"a {type(item).__name__}, which JSON does not hold")


def canonical_json(value: Any) -> str:
    """``value`` as canonical JSON (1): keys sorted by code point, no
    whitespace, strings escaped as NIP-01 and ``JSON.stringify`` do,
    everything else raw UTF-8. Raises FormatError for a value canonical
    JSON cannot hold."""
    _check_canonical(value)
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        text.encode("utf-8")            # a lone surrogate cannot be written
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise FormatError(f"not canonical JSON: {exc}") from None
    return text


def canonical_bytes(value: Any) -> bytes:
    """:func:`canonical_json` as the UTF-8 bytes that are hashed and sent."""
    return canonical_json(value).encode("utf-8")


def sha256_hex(data) -> str:
    """SHA-256 of ``data`` (bytes, or a string's UTF-8 bytes), lowercase hex."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(bytes(data)).hexdigest()


def b64encode(data: bytes) -> str:
    """Standard base64 with padding (1)."""
    return base64.b64encode(bytes(data)).decode("ascii")


def b64decode(text: Any, *, limit: Optional[int] = None) -> bytes:
    """Strict standard base64, or FormatError. ``limit`` bounds the
    decoded size before anything is decoded."""
    if not isinstance(text, str):
        raise FormatError("base64 must be a string")
    if limit is not None and len(text) > 4 * ((limit + 2) // 3):
        raise FormatError("larger than allowed")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise FormatError("not standard base64") from None


# --------------------------------------------------------------------------- #
# Small checks                                                                 #
# --------------------------------------------------------------------------- #

def is_hex64(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def is_hex128(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX128.match(value))


def is_pubkey(value: Any) -> bool:
    """A valid x-only public key: 64 lowercase hex digits naming a point
    on the curve (BIP-340 lift_x)."""
    if not is_hex64(value):
        return False
    try:
        PublicKeyXOnly(bytes.fromhex(value))
    except (ValueError, TypeError):
        return False
    return True


def is_int(value: Any, *, low: int = 0, high: int = _MAX_SAFE_INTEGER) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def need(condition: bool, what: str) -> None:
    if not condition:
        raise FormatError(what)


# --------------------------------------------------------------------------- #
# Identifiers (2)                                                              #
# --------------------------------------------------------------------------- #

def parse_address(address: Any) -> Tuple[str, str]:
    """``(publication pubkey, site d)`` of ``30004:<pubkey>:<site d>``."""
    need(isinstance(address, str), "the publication address must be a string")
    parts = address.split(":", 2)
    need(len(parts) == 3 and parts[0] == str(SITE_KIND), "not a publication address")
    need(is_pubkey(parts[1]), "the publication address names no valid key")
    need(bool(parts[2]), "the publication address has no identifier")
    return parts[1], parts[2]


def route_key(pubkey: str, site_d: str) -> str:
    """STANDUP's ``site_route_key``: ``by/<pubkey>/<urlsafe base64 of d
    without padding>``, used in ``/api/sites/<route key>/...``."""
    encoded = base64.urlsafe_b64encode(site_d.encode("utf-8")).decode("ascii").rstrip("=")
    return f"by/{pubkey}/{encoded}"


def coordinate_hash(writer: str, d: str) -> str:
    return sha256_hex(f"{ARTICLE_KIND}:{writer}:{d}")


def submission_id(writer: str, publication: str, coordinate: str, round_: int) -> str:
    need(is_int(round_, low=1), "the round starts at 1")
    return sha256_hex(f"{SUBMISSION_ID_PREFIX}{writer}:{publication}:{coordinate}:{round_}")


def status_key_hash(status_key: str) -> str:
    """SHA-256 over the 32 raw bytes of a status key (hex in transport)."""
    need(is_hex64(status_key), "a status key is 64 hex digits")
    return sha256_hex(bytes.fromhex(status_key))


def read_state_d(pubkey: str) -> str:
    """The read state's opaque ``d`` tag (13)."""
    return sha256_hex(READ_STATE_D_PREFIX + pubkey)


def key_event_d(site_d: str) -> str:
    return KEY_EVENT_D_PREFIX + site_d


def settings_event_d(site_d: str) -> str:
    return SETTINGS_EVENT_D_PREFIX + site_d


def revision_hash(revision: dict) -> str:
    return sha256_hex(canonical_bytes(revision))


def wrap_sha256(wrap: dict) -> str:
    return sha256_hex(canonical_bytes(wrap))


def media_ref(cipher_sha256: str) -> str:
    return MEDIA_REF_PREFIX + cipher_sha256


def envelope_aad(submission_id_: str, revision: int) -> bytes:
    need(is_hex64(submission_id_), "a submission id is 64 hex digits")
    need(is_int(revision, low=1), "revisions start at 1")
    return f"{ENVELOPE_AAD_PREFIX}{submission_id_}:{revision}".encode("ascii")


# --------------------------------------------------------------------------- #
# Secret keys in what is sent (5)                                              #
# --------------------------------------------------------------------------- #

def secret_keys_in_texts(texts: Iterable[Optional[str]]) -> List[str]:
    """Every secret key written in ``texts``: ``nsec1``/``ncryptsec1``
    (``refs.secret_keys_in``) and 64 hex digits a word next to them calls
    one (``refs.hex_secret_keys_in``). The keys themselves are for the
    caller to remove; they never go into an error or a log."""
    found: List[str] = []
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for key in refs.secret_keys_in(text) + refs.hex_secret_keys_in(text):
            if key not in found:
                found.append(key)
    return found


def revision_texts(revision: dict) -> List[str]:
    """The texts of a revision that are scanned before encrypting: the
    work's content and summary (5), and its title, which also travels in
    clear as the submission's title."""
    work = revision.get("work") if isinstance(revision, dict) else None
    if not isinstance(work, dict):
        return []
    texts = [work.get("title"), work.get("summary"), work.get("content")]
    texts.append(revision.get("change_summary"))
    return [t for t in texts if isinstance(t, str)]


class SecretKeyFound(FormatError):
    """The text holds a secret key. The message is the words to show
    (translated by the caller with ``_()``); the keys are never in it."""

    def __init__(self) -> None:
        super().__init__(SECRET_KEY_IN_SUBMISSION)


def refuse_secret_keys(texts: Iterable[Optional[str]]) -> None:
    """Raise :class:`SecretKeyFound` when ``texts`` hold a secret key."""
    if secret_keys_in_texts(texts):
        raise SecretKeyFound()


# --------------------------------------------------------------------------- #
# The revision (5)                                                             #
# --------------------------------------------------------------------------- #

def _check_str(value: Any, what: str, *, empty: bool = True, limit: Optional[int] = None) -> str:
    need(isinstance(value, str), f"{what} must be text")
    need(empty or bool(value), f"{what} must not be empty")
    need(limit is None or len(value) <= limit, f"{what} is longer than {limit} characters")
    return value


def _check_media_ref(value: Any, what: str) -> None:
    need(isinstance(value, str) and value.startswith(MEDIA_REF_PREFIX)
         and is_hex64(value[len(MEDIA_REF_PREFIX):]), f"{what} must be a standup-media reference")


def check_media_entry(entry: Any) -> dict:
    need(isinstance(entry, dict), "a media entry must be an object")
    need(is_hex64(entry.get("cipher_sha256")), "media cipher_sha256")
    need(is_hex64(entry.get("plain_sha256")), "media plain_sha256")
    need(entry.get("ref") == media_ref(entry["cipher_sha256"]), "media ref names its cipher hash")
    for name in ("name", "mime", "alt"):
        _check_str(entry.get(name), f"media {name}")
    need(is_int(entry.get("size")), "media size")
    need(is_int(entry.get("cipher_size")), "media cipher_size")
    need(is_int(entry.get("chunk_size"), low=MEDIA_CHUNK_MIN, high=MEDIA_CHUNK_MAX),
         "media chunk_size")
    key = b64decode(entry.get("key"), limit=32)
    need(len(key) == 32, "a media key is 32 bytes")
    servers = entry.get("servers")
    need(isinstance(servers, list) and all(isinstance(s, str) for s in servers), "media servers")
    return entry


def check_revision(revision: Any, *, submission_id_: Optional[str] = None,
                   revision_number: Optional[int] = None) -> dict:
    """``revision`` when it has the contract's shape (5), else FormatError.
    Unknown fields are left alone (readers ignore them)."""
    need(isinstance(revision, dict), "a revision is an object")
    need(revision.get("v") == 1, "revision version 1")
    need(revision.get("type") == "standup-revision", "not a standup revision")
    need(is_hex64(revision.get("submission_id")), "revision submission_id")
    if submission_id_ is not None:
        need(revision["submission_id"] == submission_id_, "a revision of another submission")
    need(is_int(revision.get("revision"), low=1), "revision number")
    if revision_number is not None:
        need(revision["revision"] == revision_number, "another revision than the one named")
    need(revision.get("kind") in KINDS, "revision kind")
    _check_work(revision.get("work"))
    _check_audio(revision.get("audio"), revision["kind"])
    _check_people(revision.get("credits"), revision.get("rights"))
    _check_checklist(revision.get("checklist"))
    summary = revision.get("change_summary")
    need(summary is None or isinstance(summary, str), "revision change_summary")
    _check_thumb(revision.get("thumb"))
    media = revision.get("media")
    need(isinstance(media, list), "revision media")
    for entry in media:
        check_media_entry(entry)
    _check_str(revision.get("client"), "revision client")
    return revision


def _check_work(work: Any) -> None:
    need(isinstance(work, dict), "revision work")
    _check_str(work.get("d"), "work d", empty=False)
    _check_str(work.get("title"), "work title", limit=MAX_TITLE_CHARS)
    _check_str(work.get("summary"), "work summary")
    _check_str(work.get("content"), "work content")
    _check_str(work.get("lang"), "work lang")
    image = work.get("image")
    need(image is None or isinstance(image, str), "work image")
    tags = work.get("tags")
    need(isinstance(tags, list) and all(isinstance(t, str) for t in tags), "work tags")
    published_at = work.get("published_at")
    need(published_at is None or is_int(published_at), "work published_at")
    authors = work.get("authors")
    need(isinstance(authors, list), "work authors")
    for author in authors:
        need(isinstance(author, dict) and is_pubkey(author.get("pubkey"))
             and isinstance(author.get("relay"), str), "work author")
    extra = work.get("extra_tags")
    need(isinstance(extra, list) and all(
        isinstance(t, list) and t and all(isinstance(x, str) for x in t) for t in extra),
        "work extra_tags")


def _check_audio(audio: Any, kind: str) -> None:
    if kind == "article":
        need(audio is None, "an article has no audio")
        return
    need(isinstance(audio, dict), "an audio submission has audio")
    need(audio.get("medium") in AUDIO_MEDIUMS, "audio medium")
    _check_media_ref(audio.get("media"), "audio media")
    need(is_int(audio.get("duration")), "audio duration")
    peaks = audio.get("peaks")
    need(isinstance(peaks, list) and len(peaks) <= MAX_PEAKS
         and all(is_int(p, high=255) for p in peaks), "audio peaks")


def _check_people(credits: Any, rights: Any) -> None:
    need(isinstance(credits, list), "revision credits")
    for credit in credits:
        need(isinstance(credit, dict) and is_pubkey(credit.get("pubkey"))
             and isinstance(credit.get("part"), str), "a credit")
    need(isinstance(rights, dict) and isinstance(rights.get("confirmed"), bool)
         and (rights.get("license") is None or isinstance(rights.get("license"), str)),
         "revision rights")


def _check_checklist(checklist: Any) -> None:
    need(isinstance(checklist, list), "revision checklist")
    for item in checklist:
        need(isinstance(item, dict) and isinstance(item.get("id"), str)
             and isinstance(item.get("label"), str) and item.get("level") in CHECK_LEVELS
             and item.get("result") in CHECK_RESULTS, "a checklist item")


def _check_thumb(thumb: Any) -> None:
    if thumb is None:
        return
    need(isinstance(thumb, dict) and thumb.get("mime") in THUMB_MIMES, "revision thumb")
    need(len(b64decode(thumb.get("data"), limit=MAX_THUMB_BYTES)) <= MAX_THUMB_BYTES,
         "a thumbnail larger than 24 KB")


def build_revision(
    *,
    submission_id: str,
    revision: int,
    kind: str,
    work: dict,
    credits: Sequence[dict] = (),
    rights: Optional[dict] = None,
    checklist: Sequence[dict] = (),
    change_summary: Optional[str] = None,
    thumb: Optional[dict] = None,
    media: Sequence[dict] = (),
    audio: Optional[dict] = None,
    client: str,
) -> dict:
    """A revision with every field of section 5, checked; raises
    FormatError for a bad field and :class:`SecretKeyFound` when its
    texts hold a secret key."""
    work = dict(work)
    work.setdefault("summary", "")
    work.setdefault("image", None)
    work.setdefault("tags", [])
    work.setdefault("lang", "")
    work.setdefault("published_at", None)
    work.setdefault("authors", [])
    work.setdefault("extra_tags", [])
    value = {
        "v": 1,
        "type": "standup-revision",
        "submission_id": submission_id,
        "revision": revision,
        "kind": kind,
        "work": work,
        "audio": audio,
        "credits": [dict(c) for c in credits],
        "rights": dict(rights) if rights is not None else {"confirmed": False, "license": None},
        "checklist": [dict(c) for c in checklist],
        "change_summary": change_summary,
        "thumb": dict(thumb) if thumb is not None else None,
        "media": [dict(m) for m in media],
        "client": client,
    }
    check_revision(value)
    _check_canonical(value)
    refuse_secret_keys(revision_texts(value))
    return value


def read_revision(plaintext: bytes, *, submission_id_: str, revision_number: int) -> dict:
    """The revision in an envelope's plaintext, checked against the
    submission and revision it was named for, whose hash the caller
    compares with ``sha256_hex(plaintext)``."""
    try:
        value = json.loads(bytes(plaintext).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise FormatError("the revision is not JSON") from None
    return check_revision(value, submission_id_=submission_id_, revision_number=revision_number)


# --------------------------------------------------------------------------- #
# Small shared shapes                                                          #
# --------------------------------------------------------------------------- #

def is_wrap(value: Any) -> bool:
    """A key wrap's shape (4): ``{"eph": pubkey, "ct": base64}`` and nothing else."""
    return (isinstance(value, dict) and set(value) == {"eph", "ct"}
            and is_pubkey(value.get("eph")) and isinstance(value.get("ct"), str)
            and 0 < len(value["ct"]) <= 90_000)


class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"the key {key!r} appears twice")
        result[key] = value
    return result


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def strict_json(raw: Any, *, limit: int) -> Any:
    """``raw`` (bytes or text) read as JSON the strict way: at most
    ``limit`` bytes, UTF-8, no key twice in one object (two readers could
    take different ones), no NaN or Infinity. Raises FormatError."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    need(isinstance(data, (bytes, bytearray)), "JSON must be text")
    need(len(data) <= limit, f"larger than {limit} bytes")
    try:
        return json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise FormatError(f"not valid JSON: {exc}") from None


def tag_values(event: dict, name: str) -> List[str]:
    """Every value of the tags ``name`` in ``event``."""
    return [entry[1] for entry in event.get("tags") or ()
            if isinstance(entry, list) and len(entry) >= 2 and entry[0] == name
            and isinstance(entry[1], str)]


def tag_value(event: dict, name: str) -> Optional[str]:
    """The first value of the tag ``name`` in ``event``, or None."""
    for entry in event.get("tags") or ():
        if isinstance(entry, list) and len(entry) >= 2 and entry[0] == name:
            return entry[1] if isinstance(entry[1], str) else None
    return None

