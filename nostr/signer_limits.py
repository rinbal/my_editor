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
plaintext escaped as a JSON string, a signed event the same way.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from i18n import _, display_locale

from . import crypto
from .local_signer import LOCAL

__all__ = [
    "SIGNER_NIP44_MAX_PLAINTEXT", "nip44_plaintext_limit", "readable_back", "sign_answer_size",
    "signable", "signer_answer_size", "size_problem", "through_signer_app",
]

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

# A signer app writes its answer as JSON, the result inside it a JSON
# string: every quote and backslash takes two bytes there, a control
# character two or six. ``nak bunker`` 0.20.7 writes the answer with
# fiatjaf.com/nostr's nip46 (``var json = jsoniter.ConfigFastest``:
# EscapeHTML false, so <, >, & and U+2028/U+2029 stay raw) and a signed
# event with easyjson (``NoEscapeHTML: true``); json-iterator 1.1.12 and
# easyjson 0.9 write \n, \r and \t short and every other control
# character as \u00xx. JSON.stringify (nostr-tools, and contract 12.5's
# formula) and Python's json.dumps also write \b and \f short. Measured at
# the longer of the two.
_ESCAPE = re.compile(r"\\(.)")
_LONG = {"b": "\\u0008", "f": "\\u000c"}

# What the answer to ``sign_event`` adds to the event asked for (12.5:
# placeholders of their length): the id, the pubkey and the signature.
_SIGNED = {"id": "0" * 64, "pubkey": "0" * 64, "sig": "0" * 128}


def _as_signer_writes(value) -> str:
    """``value`` as JSON the way a signer app writes it at its longest:
    compact, raw UTF-8, \\b and \\f as \\u00xx. In JSON text a backslash
    only ever starts an escape, so reading the escapes from the left
    finds every one."""
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return _ESCAPE.sub(lambda m: _LONG.get(m.group(1), m.group(0)), text)


def signer_answer_size(result: str) -> int:
    """Bytes of a signer app's NIP-46 answer carrying ``result`` (the
    answer echoes our 16-character request id). The answer is one NIP-44
    message: at most ``crypto.NIP44_MAX_PLAINTEXT_LEN``."""
    return len(_as_signer_writes({"id": "0" * 16, "result": result}).encode("utf-8"))


def sign_answer_size(unsigned: dict) -> int:
    """Bytes of a signer app's answer to ``sign_event`` for ``unsigned``
    (contract 12.5): the signed event, escaped as a string. Our request
    carries the same event once escaped, without what the signer adds,
    so it is the smaller of the two."""
    event = {"kind": unsigned["kind"], "content": unsigned.get("content", ""),
             "tags": unsigned.get("tags", []), "created_at": int(unsigned["created_at"])}
    return signer_answer_size(_as_signer_writes(dict(event, **_SIGNED)))


def through_signer_app(profile) -> bool:
    """Whether ``profile`` signs through a signer app (NIP-46), not with a
    key kept on this computer."""
    return getattr(profile, "signer", "remote") != LOCAL


def nip44_plaintext_limit(profile) -> int:
    """The most plaintext one ``nip44_encrypt`` (or ``nip44_decrypt``) can
    carry for ``profile``: NIP-44's own 65,535 bytes when the key is kept
    on this computer, :data:`SIGNER_NIP44_MAX_PLAINTEXT` through a signer
    app."""
    if through_signer_app(profile):
        return SIGNER_NIP44_MAX_PLAINTEXT
    return crypto.NIP44_MAX_PLAINTEXT_LEN


def readable_back(profile, plaintext: str) -> bool:
    """Whether ``plaintext``, encrypted for ``profile`` itself, can be read
    back by it: NIP-44's 65,535 bytes with the key kept on this computer;
    through a signer app 40,960 bytes, and the answer to ``nip44_decrypt``,
    which carries the plaintext escaped, must fit one message too (a
    40,000-byte text of quotes the app can encrypt is 80,000 bytes in that
    answer, and the read would wait out its timeout). What drafts and
    private lists ask before they encrypt."""
    size = len(plaintext.encode("utf-8"))
    if size > nip44_plaintext_limit(profile):
        return False
    return (not through_signer_app(profile)
            or signer_answer_size(plaintext) <= crypto.NIP44_MAX_PLAINTEXT_LEN)


def signable(profile, unsigned: dict) -> bool:
    """Whether ``profile``'s signer can sign ``unsigned`` and answer: a key
    kept here signs anything; a signer app's answer must fit one message."""
    return (not through_signer_app(profile)
            or sign_answer_size(unsigned) <= crypto.NIP44_MAX_PLAINTEXT_LEN)


def _never() -> bool:
    return False


def size_problem(profile, *, draft: Optional[str] = None, event: Optional[dict] = None,
                 key_here: Callable[[], bool] = _never) -> Optional[str]:
    """Why what is about to be asked of ``profile``'s signer cannot be done,
    in words to show, or None when it can. ``draft`` is the plaintext of a
    draft, encrypted to the account and read back later; ``event`` an
    unsigned event to be signed. When the account signs through a signer
    app and ``key_here()`` says its private key is also kept on this
    computer, the words offer that key, with which the signer app's limit
    does not apply (contract 12.5). Asked before anything is sent, so the
    person learns the real limit, not a signer app's technical refusal or a
    draft lost later."""
    problem = None
    if draft is not None and not readable_back(profile, draft):
        problem = _draft_words(profile, len(draft.encode("utf-8")))
    elif event is not None and not signable(profile, event):
        problem = _("This is too long to be signed through your signer app. Shorten it, "
                    "or split it into parts.")
    if problem is None:
        return None
    if through_signer_app(profile) and key_here():
        problem += " " + _("Your key is also kept on this computer: signed with it here, "
                           "this limit doesn't apply.")
    return problem


def _draft_words(profile, size: int) -> str:
    limit = nip44_plaintext_limit(profile)
    numbers = display_locale()
    values = {"size": numbers.toString(size), "limit": numbers.toString(limit)}
    if size <= limit:
        return _("It has {size} bytes, more than your signer app can hand back in one answer "
                 "once its quotes and line breaks are counted. Split it into smaller drafts or "
                 "publish it directly.").format(**values)
    if through_signer_app(profile):
        return _("It has {size} bytes, and your signer app can encrypt at most {limit}. "
                 "Split it into smaller drafts or publish it directly.").format(**values)
    return _("It has {size} bytes, and encryption allows {limit}. "
             "Split it into smaller drafts or publish it directly.").format(**values)
