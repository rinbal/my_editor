# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What one call to an account's signer can carry, measured before it is
asked (STANDUP's editorial contract 12.5, which MyEditor follows for every
account). The one place drafts, private lists, publishing and the signer
client ask, so what the person is told matches what the signer app does.

With the key kept on this computer nothing travels: NIP-44's own 65,535
bytes is the only bound. Through a signer app every NIP-46 request and
every answer is one NIP-44 message of at most 65,535 bytes, so the answer
bounds what can be asked: an encryption's ciphertext, a decryption's
plaintext escaped as a JSON string, a signed event the same way, each
measured at its largest (nostr/answer_size.py, contract 12.5 "Answer
size") and at most 65,471 bytes.

**Relay content caps** (contract 12.5 at feat/redaktion 4076b9b). A
request or an answer travels as the content of a kind 24133 event, and
some relays cap that content: relay.nip46.com at 65,536 characters. The
signer answers on every relay of the connection and MyEditor sends to
all of them, so the cap that applies is the largest among them, and
only when every one of them states one. :func:`connection_message_limit`
says how many bytes of JSON one message of a connection may then hold
(65,536 characters carry 40,960), and every bound here follows from it:
what a request may be, what an answer may be, how much plaintext one
encryption may carry. The caps are the relays' NIP-11 documents, read
once per connection (nostr/relay_caps.py) and noted here
(:func:`note_relay_cap`). A relay whose document was not read, or could
not be, counts as stating no cap, except one whose cap the contract
names (:data:`NAMED_CAPS`): an unknown relay would otherwise refuse
every long text, and the known capped one stays bounded either way. A
private list's pages fit a connection capped at 65,536 characters
whatever this account's own is (nostr/paged_list.py, contract 007ded2),
and a page written before that this connection cannot ask for is said
(:func:`can_ask_to_decrypt`).
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

from i18n import N_, _, display_locale

from . import crypto
from .answer_size import (
    REQUEST_ID, SIGNER_ANSWER_MAX, decrypt_answer_size, encrypt_request_size, escaped,
    request_size, sign_answer_size,
)
from .local_signer import LOCAL

__all__ = [
    "NAMED_CAPS", "NIP44_MESSAGE_MAX", "SIGNER_ANSWER_MAX", "SIGNER_NIP44_MAX_PLAINTEXT",
    "answer_limit", "bound_known", "can_ask_to_decrypt", "capped", "connection_answer_limit",
    "connection_message_limit",
    "connection_plaintext_limit", "content_cap_of", "decrypt_answer_size",
    "forget_relay_caps", "message_bytes_for_content_cap", "message_limit",
    "nip44_plaintext_limit", "note_relay_cap", "page_bounds", "readable_back", "relay_key",
    "relays_bound_known",
    "relay_read", "sign_answer_size", "signable", "size_problem", "through_signer_app",
]

# One NIP-44 message, so one NIP-46 request or answer, at its largest.
NIP44_MESSAGE_MAX = crypto.NIP44_MAX_PLAINTEXT_LEN
# What a signer may add to an answer beyond its id and result, such as an
# empty "error" (contract 12.5: 65,535 less 64).
ANSWER_EXTRA = NIP44_MESSAGE_MAX - SIGNER_ANSWER_MAX
# What a relay's frame adds around a kind 24133's content, for a cap
# stated as max_message_length: ["EVENT",{"id":..,"pubkey":..,"created_at":..,
# "kind":24133,"tags":[["p",..]],"content":"..","sig":..}] is about 420
# bytes; 512 leaves room.
EVENT_FRAME_BYTES = 512
# The caps the contract names, for a relay whose document was not read.
NAMED_CAPS: Mapping[str, int] = {"wss://relay.nip46.com": 65_536}

# What the person is told when a text is too long for the signer (contract
# 12.5: the plain consequence, never the numbers; lessons 36: one
# sentence, its cause and what to do). The contract's "Diese Verbindung"
# names the connection: MyEditor says this while publishing or saving,
# away from it, so it says which one. With the account's key also kept on
# this computer, the words offer it (contract 12.5): signed or encrypted
# with it here, the signer app's limit does not apply.
_CONNECTION_SIGN = N_("The connection to your signer app carries only short messages: "
                      "shorten this text or split it into parts.")
_CONNECTION_SIGN_HERE = N_("The connection to your signer app carries only short messages: "
                           "shorten this text, or sign it with your key kept on this "
                           "computer.")
_TOO_LONG_SIGN = N_("This is too long to be signed through your signer app: shorten it or "
                    "split it into parts.")
_TOO_LONG_SIGN_HERE = N_("This is too long to be signed through your signer app: shorten it, "
                         "or sign it with your key kept on this computer.")
_CONNECTION_DRAFT = N_("The connection to your signer app carries only short messages: "
                       "split this draft into smaller ones or publish it directly.")
# Too long to encrypt there, or to be handed back once encrypted (its
# quotes and line breaks escaped in the answer): either way, too long.
_TOO_LONG_DRAFT_APP = N_("Your signer app can’t carry a draft this long: split it into "
                         "smaller drafts or publish it directly.")
_DRAFT_HERE = N_("Your signer app can’t carry a draft this long: split it into smaller "
                 "drafts, or save it with your key kept on this computer.")
_TOO_LONG_DRAFT = N_("This draft is too long to be encrypted: split it into smaller drafts "
                     "or publish it directly.")
# The most relays whose documents are kept (a connection names a few).
_KEPT_CAPS = 256

# The most plaintext a signer app can encrypt (``nip44_encrypt``) or
# decrypt (``nip44_decrypt``) for us: 40,960 bytes, not NIP-44's 65,535.
#
# Every NIP-46 request and every answer is itself one NIP-44 message, and
# NIP-44 carries at most 65,535 bytes (``crypto.NIP44_MAX_PLAINTEXT_LEN``;
# nostr-tools stops at the same number in ``nip44.js``, and Bunker46, for
# one, encrypts its answers with it). A NIP-44 ciphertext is base64 of a
# version byte, a 32-byte nonce, a 2-byte length, the padded plaintext and
# a 32-byte MAC. Above 32,768 bytes the padding rounds up to a multiple of
# 8,192, so:
#
#   40,960 bytes pad to 40,960 -> a 54,704-character ciphertext, which fits
#   40,961 bytes pad to 49,152 -> a 65,628-character ciphertext, which is
#                                 larger than a whole NIP-46 message may be
#
# The ciphertext rides inside the signer's answer to ``nip44_encrypt`` and
# inside our request for ``nip44_decrypt``, so either way 40,960 bytes is
# the end (tests/test_bunker_request_size.py measures it with the real
# encryption). ``nak bunker`` 0.20.7 answered at 40,960 and did not answer
# at 40,961. Anything larger travels encrypted some other way, and lists
# are split into pages well below this (nostr/paged_list.py).
SIGNER_NIP44_MAX_PLAINTEXT = 40_960

# A decryption's plaintext and a signed event come back escaped inside
# the signer app's answer, which must fit one NIP-44 message: measured
# with the worst-case escaper of contract 12.5 (nostr/answer_size.py).


# -- relay content caps ---------------------------------------------------------

# Relay (relay_key) -> the content cap its NIP-11 document states, None
# for none; a relay not here was not read (NAMED_CAPS, or no cap).
_read_caps: Dict[str, Optional[int]] = {}
# Relays whose document was asked for in this run and could not be read
# (as unread: NAMED_CAPS, or no cap); kept apart so a bound is known.
_unreadable: Dict[str, None] = {}


def relay_key(url: str) -> Optional[str]:
    """A relay's address in one spelling (lower-case scheme and host, no
    trailing slash), None for anything but ws:// and wss://."""
    try:
        parts = urlsplit(str(url).strip())
        parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("ws", "wss") or not parts.hostname or "@" in parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}" + (
        f"?{parts.query}" if parts.query else "")


def _positive(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 \
        else None


def content_cap_of(document) -> Optional[int]:
    """The content cap a relay's NIP-11 document states, in characters, or
    None for none: ``limitation.max_content_length``, and
    ``max_message_length`` less the event's frame, whichever is smaller."""
    limitation = document.get("limitation") if isinstance(document, dict) else None
    if not isinstance(limitation, dict):
        return None
    content = _positive(limitation.get("max_content_length"))
    message = _positive(limitation.get("max_message_length"))
    caps = [cap for cap in (content, None if message is None
                            else max(0, message - EVENT_FRAME_BYTES)) if cap is not None]
    return min(caps) if caps else None


def note_relay_cap(url: str, cap: Optional[int], *, read: bool = True) -> None:
    """What ``url``'s NIP-11 document states (``cap``, None for none), or
    with ``read`` False that it could not be read."""
    key = relay_key(url)
    if key is None:
        return
    _read_caps.pop(key, None)
    _unreadable.pop(key, None)
    kept = _read_caps if read else _unreadable
    while len(kept) >= _KEPT_CAPS:
        del kept[next(iter(kept))]                           # the oldest first
    kept[key] = _positive(cap) if read else None


def relay_read(url: str) -> bool:
    """Whether ``url``'s NIP-11 document was read (in this run)."""
    return relay_key(url) in _read_caps


def forget_relay_caps() -> None:
    """Every document read is forgotten (tests; nothing else needs it)."""
    _read_caps.clear()
    _unreadable.clear()


def bound_known(profile) -> bool:
    """Whether what ``profile``'s signer connection carries is known in
    this run: always for a key kept here; otherwise as
    :func:`relays_bound_known` says for its relays. A check that would
    read a bound still unknown waits for it (lesson 29)."""
    if not through_signer_app(profile):
        return True
    return relays_bound_known(_relays(profile))


def relays_bound_known(relays: Iterable[str]) -> bool:
    """Whether what a connection over ``relays`` carries is known in this
    run: at once without relays; once one of them was read stating no
    cap; once every one of them was asked (read, or found unreadable).
    The one answer to it (nostr/relay_caps.py waits on it too)."""
    keys = {key for key in map(relay_key, relays or ()) if key}
    if any(key in _read_caps and connection_message_limit([key]) == NIP44_MESSAGE_MAX
           for key in keys):
        return True
    return all(key in _read_caps or key in _unreadable for key in keys)


def _relay_cap(key: str) -> Optional[int]:
    if key in _read_caps:
        return _read_caps[key]
    return NAMED_CAPS.get(key)


def message_bytes_for_content_cap(cap: int) -> int:
    """The most bytes of JSON one NIP-46 message may hold on a relay that
    caps content at ``cap`` characters (its NIP-44 payload in base64); 0
    when none fits. NIP-44 pads above 32,768 bytes to multiples of 8,192,
    so 65,536 characters carry 40,960 bytes."""
    if cap < crypto.payload_length(1):
        return 0
    low, high = 1, NIP44_MESSAGE_MAX
    while low < high:
        middle = (low + high + 1) // 2
        if crypto.payload_length(middle) <= cap:
            low = middle
        else:
            high = middle - 1
    return low


def connection_message_limit(relays: Iterable[str]) -> int:
    """The most bytes one NIP-46 message of a connection over ``relays``
    may hold: NIP-44's own 65,535 as soon as one relay states no cap,
    otherwise what the largest cap carries."""
    keys = {key for key in (relay_key(url) for url in relays or ()) if key}
    largest = 0
    for key in keys:
        cap = _relay_cap(key)
        if cap is None:
            return NIP44_MESSAGE_MAX
        largest = max(largest, message_bytes_for_content_cap(cap))
    return min(largest, NIP44_MESSAGE_MAX) if keys else NIP44_MESSAGE_MAX


def connection_answer_limit(relays: Iterable[str]) -> int:
    """The most bytes a signer's answer may take over ``relays``."""
    return max(0, min(SIGNER_ANSWER_MAX, connection_message_limit(relays) - ANSWER_EXTRA))


# The answer to nip44_encrypt, less its payload (base64: nothing to escape).
_ENCRYPT_ANSWER = len(escaped({"id": REQUEST_ID, "result": ""}).encode("utf-8"))
# The request of nip44_decrypt, less its payload, as bunker.py sends it.
_DECRYPT_REQUEST = request_size("nip44_decrypt", ["0" * 64, ""])


def connection_plaintext_limit(relays: Iterable[str]) -> int:
    """The most plaintext one ``nip44_encrypt`` or ``nip44_decrypt`` can
    carry over ``relays``: :data:`SIGNER_NIP44_MAX_PLAINTEXT` at most, and
    less when a cap makes the payload too long for one message. The
    payload travels twice: in the encryption's answer and in the
    decryption's request. The request is the longer of the two (131 bytes
    around the payload, the answer 53 and 64 more it may add), so a
    payload whose request fits has an answer that fits too
    (tests/test_relay_caps.py pins it)."""
    message = connection_message_limit(relays)

    def fits(size: int) -> bool:
        return _DECRYPT_REQUEST + crypto.payload_length(size) <= message

    low, high = 0, SIGNER_NIP44_MAX_PLAINTEXT
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


# -- what one account's signer can carry -----------------------------------------

def through_signer_app(profile) -> bool:
    """Whether ``profile`` signs through a signer app (NIP-46), not with a
    key kept on this computer."""
    return getattr(profile, "signer", "remote") != LOCAL


def _relays(profile) -> List[str]:
    return list(getattr(profile, "bunker_relays", None) or ())


def message_limit(profile) -> int:
    """The most bytes one message to ``profile``'s signer may hold."""
    if not through_signer_app(profile):
        return NIP44_MESSAGE_MAX
    return connection_message_limit(_relays(profile))


def answer_limit(profile) -> int:
    """The most bytes ``profile``'s signer may answer with: what one
    message of its connection carries, less what a signer may add."""
    if not through_signer_app(profile):
        return SIGNER_ANSWER_MAX
    return connection_answer_limit(_relays(profile))


def capped(profile) -> bool:
    """Whether the relays of ``profile``'s connection bound what it carries
    below what any signer app can (all of them state a small cap)."""
    return message_limit(profile) < NIP44_MESSAGE_MAX


def nip44_plaintext_limit(profile) -> int:
    """The most plaintext one ``nip44_encrypt`` (or ``nip44_decrypt``) can
    carry for ``profile``: NIP-44's own 65,535 bytes when the key is kept
    on this computer, :data:`SIGNER_NIP44_MAX_PLAINTEXT` through a signer
    app, and less when its connection's relays cap what they carry."""
    if through_signer_app(profile):
        return connection_plaintext_limit(_relays(profile))
    return crypto.NIP44_MAX_PLAINTEXT_LEN


def readable_back(profile, plaintext: str) -> bool:
    """Whether ``plaintext``, encrypted for ``profile`` itself, can be read
    back by it: NIP-44's 65,535 bytes with the key kept on this computer;
    through a signer app 40,960 bytes (less over capped relays), and the
    answer to ``nip44_decrypt``, which carries the plaintext escaped, must
    fit one message too (a 40,000-byte text of quotes the app can encrypt
    is 80,000 bytes in that answer, and the read would wait out its
    timeout), as must the ``nip44_encrypt`` request that carries it there,
    escaped as MyEditor sends it. What drafts and private lists ask before
    they encrypt."""
    size = len(plaintext.encode("utf-8"))
    if size > nip44_plaintext_limit(profile):
        return False
    return (not through_signer_app(profile)
            or (decrypt_answer_size(plaintext) <= answer_limit(profile)
                and encrypt_request_size(plaintext) <= message_limit(profile)))


def page_bounds(profile) -> Dict[str, int]:
    """What one page of a private list may take when ``profile`` writes it
    (nostr/paged_list.Paging): its plaintext, the signer app's answer
    handing it back, the request that encrypts it. Never more than a
    connection capped at 65,536 characters carries, whatever signs it (a
    key kept here, a signer app over any relays): the page caps of
    contract 12.5, "Pages fit every connection" (paged_list.PAGE_PLAINTEXT_MAX
    and PAGE_ANSWER_MAX), since a list written anywhere is read over any
    connection. Lower over the writer's own relays when they cap lower,
    and the request bound only over capped ones."""
    from .paged_list import PAGE_ANSWER_MAX, PAGE_PLAINTEXT_MAX
    if not through_signer_app(profile):
        plaintext, answer, request = (crypto.NIP44_MAX_PLAINTEXT_LEN, SIGNER_ANSWER_MAX,
                                      NIP44_MESSAGE_MAX)
    else:
        plaintext, answer, request = (nip44_plaintext_limit(profile), answer_limit(profile),
                                      message_limit(profile))
    return {"plaintext_max": min(plaintext, PAGE_PLAINTEXT_MAX),
            "answer_max": min(answer, PAGE_ANSWER_MAX), "request_max": request}


def can_ask_to_decrypt(profile, payload: str) -> bool:
    """Whether ``profile``'s signer can be asked to decrypt ``payload``,
    which the ``nip44_decrypt`` request carries whole: always with the key
    kept here; through a signer app when that request fits one message
    over the connection's relays. A page written before contract 007ded2
    (up to 32,000 bytes) may not fit a capped connection: the reader says
    so rather than ask (12.5, Pages fit every connection). Asked once the
    signer is handed out, so the bound is known (lesson 29)."""
    return (not through_signer_app(profile)
            or request_size("nip44_decrypt", ["0" * 64, payload]) <= message_limit(profile))


def signable(profile, unsigned: dict) -> bool:
    """Whether ``profile``'s signer can sign ``unsigned`` and answer: a key
    kept here signs anything; a signer app's answer must fit one message,
    over its connection's relays."""
    return (not through_signer_app(profile)
            or sign_answer_size(unsigned) <= answer_limit(profile))


class _Uncapped:
    """``profile`` as if its connection's relays capped nothing: whether a
    problem is the connection's, or any signer app's."""

    def __init__(self, profile) -> None:
        self.signer = getattr(profile, "signer", "remote")
        self.bunker_relays: List[str] = []


def _never() -> bool:
    return False


def size_problem(profile, *, draft: Optional[str] = None, event: Optional[dict] = None,
                 key_here: Callable[[], bool] = _never) -> Optional[str]:
    """Why what is about to be asked of ``profile``'s signer cannot be done,
    in one sentence to show, or None when it can. ``draft`` is the
    plaintext of a draft, encrypted to the account and read back later;
    ``event`` an unsigned event to be signed. When the account signs
    through a signer app and ``key_here()`` says its private key is also
    kept on this computer, the words offer that key where it helps (with
    it nothing travels, contract 12.5). Asked before anything is sent, so
    the person learns the real limit, not a signer app's technical refusal
    or a draft lost later. The numbers are :func:`size_detail`'s."""
    anywhere = _Uncapped(profile)
    if draft is not None and not readable_back(profile, draft):
        if not through_signer_app(profile):
            return _(_TOO_LONG_DRAFT)
        if len(draft.encode("utf-8")) <= crypto.NIP44_MAX_PLAINTEXT_LEN and key_here():
            return _(_DRAFT_HERE)
        return _(_CONNECTION_DRAFT if readable_back(anywhere, draft) else _TOO_LONG_DRAFT_APP)
    if event is not None and not signable(profile, event):
        here = key_here()           # only a signer app's own limit is met here
        if signable(anywhere, event):
            return _(_CONNECTION_SIGN_HERE if here else _CONNECTION_SIGN)
        return _(_TOO_LONG_SIGN_HERE if here else _TOO_LONG_SIGN)
    return None


def size_detail(profile, *, draft: str) -> str:
    """The numbers behind :func:`size_problem` for a draft, for Details:
    its size and the most its signer can encrypt."""
    numbers = display_locale()
    values = {"size": numbers.toString(len(draft.encode("utf-8"))),
              "limit": numbers.toString(nip44_plaintext_limit(profile))}
    return _("{size} bytes; at most {limit} can be encrypted this way.").format(**values)
