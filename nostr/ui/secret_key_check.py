# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The last look before a note or an article is signed: never with a
Nostr secret key in it.

A published secret key is a lost account: anyone can publish as its
owner and read their messages, and it can never be taken back. So the
event itself is looked at, after every field has gone into it (the
text, link addresses, title, summary, tags, every carried tag), right
before it goes to the signer. Mail asks the same way at Send, not while
the message is written. The signers refuse such an event in any case
(refs.SECRET_KEY_REFUSED); this is where the person is asked.
"""

from __future__ import annotations

from typing import Optional

from alerts import CANCEL, DEFAULT, Button, ask, inform
from i18n import N_, _

from .. import refs
from ..publisher import without_secret_keys

# A key in the link name (an article's identifier, its d tag): it is
# published as it is, and is never rewritten.
SLUG_HOLDS_A_KEY = N_("A secret key can’t be part of the link name, which is published as it "
                      "is. Change the title or the link name.")


def slug_problem(slug: str) -> Optional[str]:
    """Why ``slug`` (an article's or a draft's address) can't be used, or
    None."""
    return _(SLUG_HOLDS_A_KEY) if refs.secret_keys_in(slug) else None


def publishable(parent, unsigned: dict, *, is_dark: Optional[bool] = None) -> Optional[dict]:
    """``unsigned`` as it may be signed: itself without a secret key in
    it; with each one replaced by the link to its owner once the person
    agrees; None when nothing may go out (they cancelled, or a key is
    encrypted and has no owner to name)."""
    keys = refs.secret_keys_in_event(unsigned)
    if keys is None:
        inform(parent, title=_("This text can’t be checked for a secret key"), caution=True,
               is_dark=is_dark, message=_(refs.UNREADABLE_REFUSED), nostr=True)
        return None
    if not keys:
        return unsigned
    slug = next((tag[1] for tag in unsigned["tags"] if tag[:1] == ["d"] and len(tag) > 1), "")
    problem = slug_problem(slug)
    if problem:
        inform(parent, title=_("This text contains a secret key"), caution=True,
               is_dark=is_dark, message=problem, nostr=True)
        return None
    public = without_secret_keys(unsigned)
    if public is None:
        inform(parent, title=_("This text contains a secret key"), caution=True,
               is_dark=is_dark,
               message=_("Anyone who has it can publish as its owner, and once "
                         "published it can never be taken back. Remove it before "
                         "publishing."), nostr=True)
        return None
    choice = ask(parent, title=_("This text contains a secret key"), caution=True,
                 is_dark=is_dark,
                 message=_("Anyone who has it can publish as its owner, and once "
                           "published it can never be taken back. Replace it with the "
                           "public key, which only says who it belongs to?"),
                 buttons=(Button(_("Cancel"), False, CANCEL),
                          Button(_("Replace with Public Key"), True, DEFAULT)), nostr=True)
    return public if choice is True else None
