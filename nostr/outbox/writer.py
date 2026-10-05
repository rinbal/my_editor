# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Change the user's relay list or profile without ever overwriting it.

A relay list (kind 10002) and a profile (kind 0) are replaceable: what is
published last is the whole thing. Publishing one built from a guess does
not change a detail, it replaces everything the person set up, in every
app, with no undo. So every change here is a read-modify-write:

1. Read the current event fresh from relays (lookup.py). FOUND gives the
   base; ABSENT allows creating one only where the caller says so;
   UNKNOWN always refuses. Cached or saved data never authorizes a write.
   (A brand-new key minted in this process has nothing to read: it is
   the one case that starts from nothing without asking.)
2. Apply the change to that base, keeping everything it does not touch.
3. Time it after the base, sign it, and check the signer returned exactly
   what was asked (kind, content, tags, time, author) with a valid
   signature.
4. Publish where it belongs (policy.relay_list_targets / profile_targets)
   and require at least two relays to accept it. What they said is kept
   in the outcome, and a refusal names it. (There is no read-back: no
   caller would act on it and every change would wait for it. Whether
   the default relays keep what they accept is checked when they are
   chosen, by tests/smoke_relay_qualify.py.)
5. Hand a published relay list to the directory, so routing uses it now.

A base dated far in the future (MAX_BASE_AHEAD_S) is refused: the change
would have to be dated after it, and every later one with it.

AccountSetup runs this for a new account: the relay list first, then the
profile, reporting each step for the window to show. Running it again
sends only the steps not yet done. Finishing a setup later
(``fresh=False``, a new run of the app) reads first, and creates each
one only where none exists yet.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from .. import events
from . import defaults, policy
from .lookup import Lookup, fetch_replaceable
from .policy import KIND_PROFILE, KIND_RELAY_LIST, LookupState

# Outcomes.
WRITTEN = "written"
UNCHANGED = "unchanged"         # the change was already in place; nothing sent
EXISTS = "exists"               # asked to create, but one exists; nothing sent
UNKNOWN_BASE = "unknown_base"   # could not read the current one; nothing sent
REFUSED = "refused"             # nothing to change from (no list exists); nothing sent
FAILED = "failed"               # the signer said no, or too few relays took it

MIN_ACCEPTED = 2

# base event (or None) -> (content, tags), or None for "nothing to change"
Mutation = Callable[[Optional[dict]], Optional[Tuple[str, list]]]


@dataclass(frozen=True)
class WriteOutcome:
    status: str
    event: Optional[dict] = None
    accepted: tuple = ()     # relays that accepted it
    refused: tuple = ()      # (relay, what it said) for the others
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (WRITTEN, UNCHANGED, EXISTS)


def signed_matches(signed: object, unsigned: dict, author: str) -> bool:
    """The signer returned exactly the event asked for, validly signed."""
    if not isinstance(signed, dict) or not events.verify_event(signed):
        return False
    return (signed.get("kind") == unsigned["kind"]
            and signed.get("content") == unsigned["content"]
            and signed.get("tags") == unsigned["tags"]
            and signed.get("created_at") == unsigned["created_at"]
            and str(signed.get("pubkey", "")).lower() == author.lower())


class ReplaceableWriter(QObject):
    """One safe change to one of the user's replaceable events."""

    finished = Signal(object)    # WriteOutcome, exactly once

    def __init__(self, *, pool, directory, session_pool, profile, kind: int,
                 mutate: Mutation, on_absent: str = "refuse", on_found: str = "mutate",
                 new_key: bool = False, min_accepted: int = MIN_ACCEPTED,
                 query=fetch_replaceable,
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._pool = pool
        self._directory = directory
        self._session_pool = session_pool
        self._profile = profile
        self._author = profile.user_pubkey.lower()
        self._kind = kind
        self._mutate = mutate
        self._on_absent = on_absent
        self._on_found = on_found
        self._new_key = new_key
        self._min_accepted = min_accepted
        self._query = query
        self._clock = clock
        self._done = False
        self.signed: Optional[dict] = None

    def start(self) -> None:
        if self._new_key:
            self._with_base(None)
            return
        known = self._directory.cached(self._author)
        relays = policy.lookup_relays(known=known, own=True)
        self._query(self._pool, relays, kind=self._kind, author=self._author,
                    on_done=self._on_lookup, parent=self)

    def publish_signed(self, signed: dict, base: Optional[dict] = None) -> None:
        """Send an event signed earlier again (a retry), unchanged."""
        self.signed = signed
        self._publish(signed, base)

    # -- steps -------------------------------------------------------------------

    def _on_lookup(self, result: Lookup) -> None:
        if result.state is LookupState.UNKNOWN:
            self._finish(WriteOutcome(UNKNOWN_BASE, reason="The current one couldn’t be read."))
            return
        if result.state is LookupState.ABSENT:
            remembered = (self._directory.cached(self._author)
                          if self._kind == KIND_RELAY_LIST else None)
            if remembered is not None and remembered.found and self._on_found == "exists":
                # The relays asked have not caught up with a list this app
                # holds (published here, or read earlier): one exists.
                self._finish(WriteOutcome(EXISTS, event=remembered.event))
                return
            if self._on_absent != "create":
                self._finish(WriteOutcome(REFUSED, reason="There is none to change."))
                return
            self._with_base(None)
            return
        base = result.event
        if self._kind == KIND_RELAY_LIST:
            # A list this app published and remembers may be newer than what
            # the relays asked just now have caught up with.
            remembered = self._directory.cached(self._author)
            if remembered.found and remembered.created_at > int(base.get("created_at", 0)):
                base = remembered.event
        if self._on_found == "exists":
            self._finish(WriteOutcome(EXISTS, event=base))
            return
        self._with_base(base)

    def _with_base(self, base: Optional[dict]) -> None:
        try:
            change = self._mutate(base)
        except ValueError as exc:
            self._finish(WriteOutcome(FAILED, reason=str(exc)))
            return
        if change is None:
            self._finish(WriteOutcome(UNCHANGED, event=base))
            return
        ahead = (policy.created_at_of(base) or 0) - int(self._clock())
        if ahead > defaults.MAX_BASE_AHEAD_S:
            days = max(1, round(ahead / 86_400))
            self._finish(WriteOutcome(
                FAILED, event=base,
                reason=(f"The current one is dated about {days} day{'s' if days != 1 else ''} "
                        "ahead of this computer’s clock, and a change would have to be "
                        "dated after it. Check the clock first.")))
            return
        content, tags = change
        unsigned = {"kind": self._kind, "content": content, "tags": tags,
                    "created_at": policy.replacement_created_at(base, self._clock())}

        def signed(event: dict) -> None:
            if not signed_matches(event, unsigned, self._author):
                self._finish(WriteOutcome(FAILED, reason="The signer returned a different event."))
                return
            self.signed = event
            self._publish(event, base)

        def refused(reason: str) -> None:
            self._finish(WriteOutcome(FAILED, reason=reason or "The signer didn’t sign."))

        self._session_pool.get(self._profile,
                               lambda client: client.sign_event(unsigned, signed, refused),
                               refused)

    def _publish(self, event: dict, base: Optional[dict]) -> None:
        if self._kind == KIND_RELAY_LIST:
            old = policy.parse_relay_list(base) if base else None
            targets = policy.relay_list_targets(event, old)
        else:
            targets = policy.profile_targets(self._directory.cached(self._author))
        job = self._pool.publish(targets, event)

        def published(results):
            accepted = tuple(url for url, ok, _message in results if ok)
            refused = tuple((url, str(message or "no answer"))
                            for url, ok, message in results if not ok)
            if len(accepted) < min(self._min_accepted, len(targets)):
                said = "; ".join(f"{url}: {message}" for url, message in refused[:4])
                self._finish(WriteOutcome(
                    FAILED, event=event, accepted=accepted, refused=refused,
                    reason="Too few relays accepted it." + (f" ({said})" if said else "")))
                return
            if self._kind == KIND_RELAY_LIST:
                self._directory.remember(event)
            self._finish(WriteOutcome(WRITTEN, event=event, accepted=accepted,
                                      refused=refused))

        job.all_done.connect(published)

    def _finish(self, outcome: WriteOutcome) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit(outcome)


# --------------------------------------------------------------------------- #
# The changes the app makes                                                    #
# --------------------------------------------------------------------------- #

def add_relay(*, url: str, marker: str = "write", **deps) -> ReplaceableWriter:
    """Add one relay to the user's list; refuses when no list exists."""
    def mutate(base):
        tags = policy.relay_list_tags_adding(base, url, marker=marker)
        return None if tags is None else ("", tags)
    return ReplaceableWriter(kind=KIND_RELAY_LIST, mutate=mutate, on_absent="refuse", **deps)


def create_relay_list(*, extra_write=(), **deps) -> ReplaceableWriter:
    """Publish MyEditor's starter list; does nothing when one exists."""
    return ReplaceableWriter(
        kind=KIND_RELAY_LIST, on_absent="create", on_found="exists",
        mutate=lambda base: ("", policy.starter_relay_list_tags(extra_write=extra_write)),
        **deps)


def update_profile(*, changes: Mapping[str, Optional[str]], **deps) -> ReplaceableWriter:
    """Change fields of the user's profile, keeping every other field."""
    def mutate(base):
        content = policy.merge_profile_content(base.get("content") if base else None, changes)
        if base is not None and json.loads(content) == json.loads(base.get("content") or "{}"):
            return None
        return content, list(base.get("tags", [])) if base else []
    return ReplaceableWriter(kind=KIND_PROFILE, mutate=mutate, on_absent="create", **deps)


class AccountSetup(QObject):
    """Tell the Nostr network about a brand-new account: its relay list, then
    its profile. A retry sends the very events signed the first time.

    ``fresh`` is for a key minted in this run of the app, which has nothing
    to read. Without it (finishing a setup left for later) each event is
    read first and created only where none exists: something published
    since, here or in another app, is never replaced.
    """

    step = Signal(str, str, str)      # "relays" | "profile", state, detail
    finished = Signal(bool, str)      # ok, message

    def __init__(self, *, name: str, deps: dict, fresh: bool = True,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._name = name
        self._deps = deps
        self._fresh = fresh
        self._signed: dict = {}
        self._done: dict = {}           # step -> its outcome, once it succeeded

    def start(self) -> None:
        """Run the steps not yet done; a retry skips what already went out."""
        if "relays" in self._done:
            self._relays_done(self._done["relays"])
            return
        self.step.emit("relays", "active", "")
        writer = create_relay_list(new_key=self._fresh, parent=self, **self._deps)
        self._run(writer, "relays", self._relays_done)

    def _run(self, writer: ReplaceableWriter, key: str, then) -> None:
        if key in self._signed:
            writer.finished.connect(then)
            writer.publish_signed(self._signed[key])
            return

        def finished(outcome: WriteOutcome):
            if writer.signed is not None:
                self._signed[key] = writer.signed
            then(outcome)

        writer.finished.connect(finished)
        writer.start()

    def _relays_done(self, outcome: WriteOutcome) -> None:
        if outcome.ok:
            self._done["relays"] = outcome
        if not outcome.ok:
            self.step.emit("relays", "error", "The relays didn’t take it yet.")
            self.finished.emit(False, "Your account is saved, but the network doesn’t "
                                      "know it yet. Check your internet connection and "
                                      "try again.")
            return
        count = len(outcome.accepted)
        self.step.emit("relays", "done", f"Saved on {count} relays." if count else "")
        if not self._name:
            self.step.emit("profile", "done", "No name to publish.")
            self.finished.emit(True, "")
            return
        if "profile" in self._done:
            self._profile_done(self._done["profile"])
            return
        self.step.emit("profile", "active", "")
        # Finishing later, a profile found on the network is the person's
        # own by then, and stays as it is.
        writer = update_profile(changes={"name": self._name, "display_name": self._name},
                                new_key=self._fresh,
                                on_found="mutate" if self._fresh else "exists",
                                parent=self, **self._deps)
        self._run(writer, "profile", self._profile_done)

    def _profile_done(self, outcome: WriteOutcome) -> None:
        if outcome.ok:
            self._done["profile"] = outcome
        if not outcome.ok:
            self.step.emit("profile", "error", "Your name isn’t published yet.")
            self.finished.emit(False, "Your account is ready, but your profile isn’t "
                                      "published yet. Try again in a moment.")
            return
        self.step.emit("profile", "done", "")
        self.finished.emit(True, "")
