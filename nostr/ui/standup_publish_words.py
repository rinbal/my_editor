# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a publish run says (M-4 M13): its stages and its words, one
sentence each. Moved out of :mod:`.standup_publish`, which offers every
name here as before."""

from __future__ import annotations

from enum import Enum

from i18n import N_

BUSY = N_("Publishing …")
BUSY_DETAIL = N_("The pictures go up to your media server, then it’s signed.")
BUSY_SIGNING = N_("Waiting for your signer app.")
DONE = N_("Published.")
NOT_PUBLISHED = N_("Not published")
PICTURE_NOT_FOUND = N_("A picture of the accepted version can’t be found on the media "
                       "servers, so nothing was published.")
PICTURE_NOT_UP = N_("A picture couldn’t be put on your media server, so nothing was "
                    "published. Check your media servers in Settings, then try again.")
NOT_THE_SAME = N_("The submission changed in the meantime, so nothing was published.")
# 12.4, Before the writer's work goes out: the words both apps say.
SOMEONE_ELSES = N_("Another person’s article already stands under this link name. So that "
                   "it stays, nothing is published.")
# No relay answered the read of its address, or this server's read
# failed (12.4, RG-B5-d): the words both apps say, under NOT_PUBLISHED.
NOT_CHECKED = N_("Whether an article already stands under this link name couldn’t be "
                 "checked just now. Try again in a moment.")
# Q1, too long for the signer app (the web's N1, tooLongLocal and
# tooLongElsewhere): the text is the accepted version's, never shortened.
TOO_LONG_HERE = N_("Your signer app doesn’t sign texts this long. It works with the key on "
                   "this computer.")
TOO_LONG_ELSEWHERE = N_("Your signer app doesn’t sign texts this long. Sign in with another "
                        "signer app, or write to the editorial team.")
NOT_REPORTED = N_("Published. {publication} doesn’t know yet: try again in a moment.")
# 12.4 One work per address: a writer's work keeps its link name (F5A's
# publishedD; another d would not be this work's), so it is only said.
ADDRESS_TAKEN = N_("Published. {publication} already lists another article under this "
                   "link name, so it can’t list this one.")
NOT_PUBLISHABLE = N_("This version can’t be published as it is. Ask the editorial team "
                     "in the conversation.")
# How many relays a link to the article names (NIP-19).
LINK_RELAYS = 3


class Stage(Enum):
    READY = "ready"
    WORKING = "working"
    OFFER_HERE = "offer_here"           # the signer app can't sign it; the key is here (Q1)
    NOT_STORED = "not_stored"           # signed, no relay took it: Erneut sends it again
    NOT_REPORTED = "not_reported"       # out, not reported: Erneut reports it again
    SOMEONE_ELSES = "someone_elses"     # another person's article at its address: nothing out
    NOT_CHECKED = "not_checked"         # its address could not be read: nothing out, Erneut
    CHANGED = "changed"                 # the submission changed meanwhile: the sheet closes
    TOO_LONG = "too_long"               # the signer app can't sign it, no key here: ask
    DONE = "done"
    FAILED = "failed"                   # nothing went out; the pictures were taken down


__all__ = ["ADDRESS_TAKEN", "BUSY", "BUSY_DETAIL", "BUSY_SIGNING", "DONE", "LINK_RELAYS",
           "NOT_CHECKED", "NOT_PUBLISHABLE", "NOT_PUBLISHED", "NOT_REPORTED", "NOT_THE_SAME",
           "PICTURE_NOT_FOUND", "PICTURE_NOT_UP", "SOMEONE_ELSES", "Stage", "TOO_LONG_ELSEWHERE",
           "TOO_LONG_HERE"]
