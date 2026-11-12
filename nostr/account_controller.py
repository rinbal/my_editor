# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The account side of the main window.

Creating, restoring and backing up an account, signing out of one, and
telling the Nostr network about a new one. The windows themselves are in
nostr/ui/account_windows.py; this is what happens around them:

- A created account is published: its relay list, then its name
  (outbox/writer.py AccountSetup), reported step by step to the window.
  Try Again in the window sends the very events signed the first time.
- If that doesn't finish, the account is marked ``setup_pending``, and
  the next time it becomes the active account (at startup, by switching,
  by connecting) the setup runs again without asking: MyEditor made the
  account, and only creates what is still missing.
- A restored account is read, not written: its relay list and its name.
  A relay list is published only after asking, because the account is the
  person's own.
- The work never depends on the window that shows it. The window can be
  closed at any time; the work goes on and reports to nobody.
- Signing out forgets every key kept for the account, and pairing a
  signer app for an account whose key is kept here offers to delete it.

The main window keeps what is about the active account across the app
(drafts, media, the chip); it hears from here through ``activate``,
``profile_changed`` and ``status``.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, Optional

import shiboken6
from PySide6.QtCore import QObject, QTimer, Signal

from alerts import (
    CANCEL, DEFAULT, DESTRUCTIVE, Button, ask, ask_try_again, confirm_destructive, inform,
)
from i18n import N_, _
from nostr import failures
from nostr.outbox import writer as outbox_writer
from nostr.outbox.defaults import STARTER_LIST
from nostr.outbox.lookup import fetch_replaceable
from nostr.outbox.policy import KIND_PROFILE, LookupState, lookup_relays
from nostr.ui.account_windows import (
    STEP_PROFILE, STEP_RELAYS, BackupAccountWindow, CreateAccountWindow, MoveToSignerWindow,
    RestoreAccountWindow,
)
from nostr.ui.connect_dialog import ConnectDialog

_STEPS = {"relays": STEP_RELAYS, "profile": STEP_PROFILE}


def _profile_name(event) -> str:
    """The name in a profile event, or "" for anything else."""
    if not isinstance(event, dict):
        return ""
    try:
        content = json.loads(event.get("content") or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(content, dict):
        return ""
    name = content.get("display_name") or content.get("name") or ""
    return name.strip() if isinstance(name, str) else ""


# A restore step no relay answered, after the quiet reads: the one line in
# place, no alert (lessons 36), and never "not found" (lesson 30). The
# profile is read again whenever the account is made the active one, and
# the relay list before anything is published.
_NOT_CHECKED = N_("Not checked yet: MyEditor looks again later.")


class _Report:
    """Speaks to a window's SetupReport while the window exists, and is
    quiet after: the work never depends on the window that shows it."""

    def __init__(self, report) -> None:
        self._report = report

    @property
    def watching(self) -> bool:
        return self._report is not None and shiboken6.isValid(self._report)

    def step(self, key: str, state: str, detail: str = "") -> None:
        if self.watching:
            self._report.step.emit(key, state, detail)

    def finished(self, ok: bool, message: str = "") -> None:
        if self.watching:
            self._report.finished.emit(ok, message)


class AccountController(QObject):
    """Account windows and the network work behind them."""

    activate = Signal(object)          # Profile: make it the active account
    profile_changed = Signal(object)   # Profile: its name was learned
    status = Signal(str, int)          # a status bar message, and for how long
    link_activated = Signal(str)       # a link the person chose to open

    def __init__(self, *, relay_pool, directory, vault, session_pool, store, window,
                 is_dark: Callable[[], bool] = lambda: True, fetch=fetch_replaceable,
                 writer_deps: Optional[dict] = None,
                 later: Callable[[int, Callable[[], None]], None] = QTimer.singleShot,
                 parent: Optional[QObject] = None) -> None:
        """``window`` is the parent of every window and alert shown here.
        ``fetch``, ``writer_deps`` (extra ReplaceableWriter arguments,
        such as ``query`` and ``clock``) and ``later`` (the wait between
        quiet reads) are seams for tests."""
        super().__init__(parent)
        self._relay_pool = relay_pool
        self._directory = directory
        self._vault = vault
        self._session_pool = session_pool
        self._store = store
        self._window = window
        self._is_dark = is_dark
        self._fetch = fetch
        self._writer_deps = dict(writer_deps or {})
        self._later = later
        # One setup per account at a time: the window's, or the one that
        # finishes a setup left for later.
        self._setups: Dict[str, QObject] = {}
        self._relay_list_declined: set = set()
        # A paired signer that names other relays (switch_relays) is
        # reached there from now on: the account keeps them (audit F17).
        session_pool.relays_switched.connect(self.keep_signer_relays)

    # -- the windows ---------------------------------------------------------------

    def create_account(self) -> CreateAccountWindow:
        window = CreateAccountWindow(store=self._store, vault=self._vault,
                                     connect_signer=self._pair_signer,
                                     is_dark=self._is_dark(), parent=self._window)
        window.account_ready.connect(
            lambda profile, name, report: self._account_ready(profile, name, report,
                                                              created=True))
        window.link_activated.connect(self.link_activated)
        window.finished.connect(window.deleteLater)
        window.open()
        return window

    def restore_account(self) -> RestoreAccountWindow:
        window = RestoreAccountWindow(store=self._store, vault=self._vault,
                                      is_dark=self._is_dark(), parent=self._window)
        window.account_ready.connect(
            lambda profile, name, report: self._account_ready(profile, name, report,
                                                              created=False))
        window.finished.connect(window.deleteLater)
        window.open()
        return window

    def backup_account(self) -> Optional[BackupAccountWindow]:
        active = self._store.default()
        if active is None:
            return None
        secret = self._vault.load(active.user_pubkey)
        if secret is None:
            if active.is_local:
                inform(self._window, title=_("This account’s key isn’t on this computer"),
                       message=_("MyEditor can’t find the private key it kept for this "
                                 "account. If you saved a backup, restore the account "
                                 "from it."),
                       is_dark=self._is_dark(), nostr=True)
            else:
                inform(self._window, title=_("This account’s key is in your signer app"),
                       message=_("Back it up there."), is_dark=self._is_dark(), nostr=True)
            return None
        window = BackupAccountWindow(secret=secret, is_dark=self._is_dark(),
                                     parent=self._window)
        window.finished.connect(window.deleteLater)
        window.open()
        return window

    def move_to_signer(self) -> Optional[MoveToSignerWindow]:
        """Move the active account's key from this computer into Amber."""
        active = self._store.default()
        if active is None or not active.is_local:
            return None
        secret = self._vault.load(active.user_pubkey)
        if secret is None:
            inform(self._window, title=_("This account’s key isn’t on this computer"),
                   message=_("MyEditor can’t find the private key it kept for this "
                             "account. If you saved a backup, restore the account "
                             "from it."),
                   is_dark=self._is_dark(), nostr=True)
            return None
        window = MoveToSignerWindow(secret=secret, name=active.display_name,
                                    connect_signer=self._pair_signer,
                                    is_dark=self._is_dark(), parent=self._window)
        window.moved.connect(self._signer_moved_in)
        window.link_activated.connect(self.link_activated)
        window.finished.connect(window.deleteLater)
        window.open()
        return window

    def _signer_moved_in(self, profile) -> None:
        """Amber signs as the account now: it becomes the account's signer
        (the local one is let go), and the key kept here is offered for
        deletion."""
        self._save(profile)
        self._session_pool.drop(profile.user_pubkey)
        self.activate.emit(profile)
        # After the window has shown that Amber holds the key.
        QTimer.singleShot(0, lambda: self.signer_paired(profile))

    def _pair_signer(self, on_profile, parent=None) -> None:
        """Pair a signer app for Create Account, over its window. Nothing is
        saved here: the window checks the account first."""
        dialog = ConnectDialog(self._relay_pool, self._store, parent=parent or self._window,
                               is_dark=self._is_dark(), persist=False,
                               offer_alternatives=False)
        dialog.profile_connected.connect(on_profile)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    # -- a created or restored account ---------------------------------------------

    def _account_ready(self, profile, name: str, report, *, created: bool) -> None:
        """The new account becomes the active one, and the network learns
        about it (created) or is asked about it (restored)."""
        if created:
            profile.setup_pending = True
            self._save(profile)
            # Registered before the account becomes active, so activating it
            # doesn't start a second setup of its own.
            start = self._prepare_setup(profile, name, report, fresh=True)
            self.activate.emit(profile)
            start()
            return
        self.activate.emit(profile)
        self._read_restored(profile, report)

    def profile_activated(self, profile) -> None:
        """An account became the active one. If MyEditor created it and its
        setup was left for later, finish it now, without asking."""
        if profile is None:
            return
        stored = self._store.get(profile.user_pubkey)
        if stored is None or not stored.setup_pending or stored.user_pubkey in self._setups:
            return
        self._prepare_setup(stored, stored.display_name, None, fresh=False)()

    def _prepare_setup(self, profile, name: str, report, *, fresh: bool) -> Callable[[], None]:
        """Set up the account's setup and return what starts it. It lives as
        long as the controller needs it: until it finished and no window
        can ask for it again."""
        pubkey = profile.user_pubkey
        setup = outbox_writer.AccountSetup(name=name, deps=self._outbox_deps(profile),
                                           fresh=fresh, parent=self)
        self._setups[pubkey] = setup
        window = _Report(report)
        state = {"running": False}

        def start() -> None:
            if not state["running"] and shiboken6.isValid(setup):
                state["running"] = True
                setup.start()

        def finished(ok: bool, message: str) -> None:
            state["running"] = False
            if ok:
                self._set_pending(pubkey, False)
                if not window.watching:
                    self.status.emit(_("Your new account is set up on the Nostr network."),
                                     6000)
            window.finished(ok, message)
            if not window.watching:
                self._forget_setup(pubkey, setup)

        def window_gone(*_args) -> None:
            if not state["running"]:
                self._forget_setup(pubkey, setup)

        setup.step.connect(lambda key, st, detail: window.step(_STEPS[key], st, detail))
        setup.finished.connect(finished)
        if report is not None:
            # Try Again in the window: the same setup, the same signed events.
            report.retry = start
            report.destroyed.connect(window_gone)
        return start

    def _forget_setup(self, pubkey: str, setup: QObject) -> None:
        if self._setups.get(pubkey) is setup:
            del self._setups[pubkey]
        if shiboken6.isValid(setup):
            setup.deleteLater()

    def _set_pending(self, pubkey: str, pending: bool) -> None:
        stored = self._store.get(pubkey)
        if stored is not None and stored.setup_pending != pending:
            stored.setup_pending = pending
            self._save(stored)

    def keep_signer_relays(self, pubkey: str, relays: list) -> None:
        """The account's signer listens on ``relays`` now: kept with it, so
        the next start reaches it there."""
        stored = self._store.get(pubkey)
        if stored is not None and not stored.is_local and relays \
                and list(stored.bunker_relays) != list(relays):
            stored.bunker_relays = list(relays)
            self._save(stored)

    def _save(self, profile) -> None:
        try:
            self._store.upsert(profile)
        except OSError:
            pass        # kept in memory; the next save writes it

    def _read_restored(self, profile, report) -> None:
        """What a restored account already has: its relay list, then its name."""
        pubkey = profile.user_pubkey
        window = _Report(report)
        state = {"relays": None}

        # A read no relay answered is read again quietly before the step
        # says so, in place and once (lessons 36): the account is restored
        # either way.
        def read(attempt, then) -> None:
            failures.retry_quietly(attempt, on_done=then, later=self._later,
                                   settled=lambda found: found.state is not LookupState.UNKNOWN)

        def relays_known(relay_list) -> None:
            state["relays"] = relay_list
            # In plain words: the step says what was looked for.
            if relay_list.found:
                window.step(STEP_RELAYS, "done", _("Found."))
            elif relay_list.state is LookupState.ABSENT:
                window.step(STEP_RELAYS, "done", _("None yet."))
            else:
                window.step(STEP_RELAYS, "error", _(_NOT_CHECKED))
            window.step(STEP_PROFILE, "active")
            read(lambda answer: self._fetch(
                self._relay_pool, lookup_relays(known=relay_list, own=True), kind=KIND_PROFILE,
                author=pubkey, on_done=answer, parent=self), profile_known)

        def profile_known(result) -> None:
            name = _profile_name(result.event)
            if name:
                stored = self._store.get(pubkey)
                if stored is not None and stored.display_name != name:
                    stored.display_name = name
                    self._save(stored)
                    self.profile_changed.emit(stored)
                window.step(STEP_PROFILE, "done", _("Welcome back, {name}.").format(name=name))
            elif result.state is LookupState.UNKNOWN:
                window.step(STEP_PROFILE, "error", _(_NOT_CHECKED))
            else:
                window.step(STEP_PROFILE, "done", _("No public profile found."))
            window.finished(True, "")
            if state["relays"] is not None and state["relays"].state is LookupState.ABSENT:
                QTimer.singleShot(0, lambda: self.offer_relay_list(profile))

        window.step(STEP_RELAYS, "active")
        read(lambda answer: self._directory.lookup(pubkey, answer, fresh=True), relays_known)

    # -- relay lists ---------------------------------------------------------------

    def check_relay_list(self, profile) -> None:
        """After connecting an existing account: if it really has no relay
        list, offer one. Never published without asking: it is the person's."""
        def known(relay_list) -> None:
            if relay_list.state is LookupState.ABSENT:
                self.offer_relay_list(profile)
        self._directory.lookup(profile.user_pubkey, known, fresh=True)

    def offer_relay_list(self, profile) -> None:
        if profile.user_pubkey in self._relay_list_declined:
            return
        hosts = ", ".join(url.split("://", 1)[1] for url, _marker in STARTER_LIST)
        choice = ask(
            self._window, title=_("Tell Nostr apps where your posts are?"),
            message=_("Other Nostr apps find your notes and articles through a "
                      "relay list, and this account doesn’t have one yet. MyEditor "
                      "can publish one with its recommended relays: {hosts}."
                      ).format(hosts=hosts),
            buttons=(Button(_("Not Now"), False, CANCEL),
                     Button(_("Publish Relay List"), True, DEFAULT)),
            is_dark=self._is_dark(), nostr=True)
        if choice is not True:
            self._relay_list_declined.add(profile.user_pubkey)
            return
        self._publish_relay_list(profile)

    def _publish_relay_list(self, profile, signed: Optional[dict] = None) -> None:
        """Publish the recommended relay list; ``signed``, one signed for it
        before, is sent again as it is, the signer app not asked twice."""
        writer = outbox_writer.create_relay_list(parent=self, **self._outbox_deps(profile))

        def done(outcome) -> None:
            writer.deleteLater()
            if outcome.status == outbox_writer.WRITTEN:
                self.status.emit(_("Your relay list is published."), 5000)
                return
            if outcome.status == outbox_writer.EXISTS:
                self.status.emit(_("This account already has a relay list."), 5000)
                return
            # Said once (lessons 36): what did not happen, why and what to
            # do, what the relays said behind Details, and Try Again.
            again = writer.signed if outcome.cause == outbox_writer.CAUSE_RELAYS else None
            if ask_try_again(self._window, title=_("Your relay list wasn’t published"),
                             message=outbox_writer.failure_words(outcome),
                             details=outbox_writer.failure_detail(outcome),
                             is_dark=self._is_dark(), nostr=True):
                self._publish_relay_list(profile, again)

        writer.finished.connect(done)
        if signed is not None:
            writer.publish_signed(signed)
        else:
            writer.start()

    def _outbox_deps(self, profile) -> dict:
        return {"pool": self._relay_pool, "directory": self._directory,
                "session_pool": self._session_pool, "profile": profile,
                **self._writer_deps}

    # -- signer apps and signing out -----------------------------------------------

    def signer_paired(self, profile) -> None:
        """Connect Signer paired a signer app for ``profile``. A key still
        kept here for the account is offered for deletion (the app holds
        it now), then the account's relay list is checked."""
        if self._vault.has(profile.user_pubkey):
            self._offer_to_delete_key(profile)
        QTimer.singleShot(0, lambda: self.check_relay_list(profile))

    def _offer_to_delete_key(self, profile) -> None:
        name = profile.display_name or profile.npub_short()
        choice = ask(
            self._window, title=_("Delete the private key kept on this computer?"),
            message=_("{name} now signs with your signer app, which holds the same key. "
                      "MyEditor still keeps a copy of it on this computer. Deleting it "
                      "leaves the key only where you approve what is signed."
                      ).format(name=name),
            # Deleting is the person's own choice, never the default:
            # without a backup, the signer app would hold the only copy.
            buttons=(Button(_("Delete Key"), True, DESTRUCTIVE),
                     Button(_("Keep Key"), False, DEFAULT)),
            caution=True, is_dark=self._is_dark(), nostr=True)
        if choice is True:
            self._forget_key(profile.user_pubkey)

    def _forget_key(self, pubkey: str) -> bool:
        try:
            self._vault.forget(pubkey)
        except OSError:
            inform(self._window, title=_("The private key couldn’t be deleted"),
                   message=_("MyEditor couldn’t change the file it keeps keys in. Make "
                             "sure the disk isn’t full or read-only, then try again."),
                   caution=True, is_dark=self._is_dark(), nostr=True)
            return False
        return True

    def sign_out(self, profile) -> bool:
        """Ask, then forget the account: every key kept for it here, its
        signer, and its profile. True when it was signed out."""
        return self.confirm_sign_out(profile) and self.forget_account(profile)

    def confirm_sign_out(self, profile) -> bool:
        """Ask whether to sign out of ``profile``; nothing is forgotten
        yet, so what still needs its signer (a read state written before
        sign-out) can go first. :meth:`forget_account` then."""
        name = profile.display_name or profile.npub_short()
        title = _("Sign out of {name}?").format(name=name)
        has_key = self._vault.has(profile.user_pubkey)
        if profile.is_local and has_key:
            confirmed = confirm_destructive(
                self._window, title=title,
                message=_("MyEditor deletes the private key it keeps for this account. "
                          "Without a backup, nobody can get this account back."),
                action=_("Sign Out and Delete Key"), caution=True, is_dark=self._is_dark(), nostr=True)
        elif has_key:
            confirmed = confirm_destructive(
                self._window, title=title,
                message=_("Your key stays in your signer app. MyEditor forgets this "
                          "connection "
                          "and deletes the copy of the key it keeps on this computer."),
                action=_("Sign Out and Delete Key"), is_dark=self._is_dark(), nostr=True)
        elif profile.is_local:
            confirmed = confirm_destructive(
                self._window, title=title,
                message=_("This account’s private key isn’t on this computer. "
                          "MyEditor forgets the account."),
                action=_("Sign Out"), is_dark=self._is_dark(), nostr=True)
        else:
            confirmed = confirm_destructive(
                self._window, title=title,
                message=_("Your key stays in your signer app. MyEditor only forgets "
                          "this connection."),
                action=_("Sign Out"), is_dark=self._is_dark(), nostr=True)
        return bool(confirmed)

    def forget_account(self, profile) -> bool:
        """Forget every key kept here for ``profile``, its signer and its
        profile, once the person confirmed. False when the key could not
        be deleted (they were told): the account stays."""
        if self._vault.has(profile.user_pubkey) and not self._forget_key(profile.user_pubkey):
            return False
        self._session_pool.drop(profile.user_pubkey)
        try:
            self._store.remove(profile.user_pubkey)
        except OSError:
            pass        # gone from this run; the next save leaves it out too
        return True
