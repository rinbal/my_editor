# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""EINUNDZWANZIG membership, as the running app sees it.

One object owns everything the app does about the association: the
roster (MembershipDirectory) and its refresh, what the active account is
entitled to, the membership window and the API client behind it, and
adding the members' relay to the account's relay list. The main window
constructs it, hands its providers to the relay and media layers, and
listens to one signal.

The providers are read on every call rather than captured, because
membership can resolve, lapse or be confirmed while the app is open:

    entitled_relays()          the members' relay, for a member
    entitled_blossom_servers() the members' media server, for a member
    quota(origin)              the space a membership gives on a server
    server_label(origin)       a friendlier name for that server

``benefits_changed`` fires only when the active account's benefits
actually changed, so a periodic roster refresh that confirms what was
known costs the rest of the app nothing (no media library walk, no
signer prompt).

A stale roster keeps its last answer while it is refreshed, and a failed
refresh keeps it too (see MembershipDirectory): a member's relay and
media server do not drop out because the association's host hiccuped.
"""

from __future__ import annotations

from typing import Callable, Optional

import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer, Signal

import url_safety
from nostr.einundzwanzig import (
    MEMBER_BENEFITS, MEMBER_RELAY, NO_BENEFITS, Benefits, MembershipDirectory,
)
from nostr.einundzwanzig_api import MembershipApi, session_signer
from nostr.relay_list_addition import (
    DONE, NO_LIST, RecommendedRelayList, RelayListAddition, outcome_message,
)
from nostr.ui.membership_window import MembershipWindow

# How often the roster is fetched while the app is open. The roster is
# cached for a quarter hour; each tick fetches it regardless of that
# cache, so an answer is never more than this old while the host answers.
REFRESH_INTERVAL_MS = 10 * 60 * 1000

# Shown once per member per session, when the membership is first known.
RECOGNIZED_MESSAGE = ("EINUNDZWANZIG membership recognized. Your members’ relay and "
                      "media server are available.")

# The Media Library's name for the members' server.
SERVER_LABEL = "EINUNDZWANZIG"


def _key(pubkey: Optional[str]) -> str:
    return (pubkey or "").strip().lower()


class MembershipController(QObject):
    """Membership for the active account: what it grants, and joining."""

    # The active account's benefits changed: resolved, lapsed, confirmed,
    # or a different account became the active one.
    benefits_changed = Signal()

    def __init__(
        self,
        *,
        profile_provider: Callable[[], object],
        relay_pool=None,
        session_pool=None,
        relay_directory=None,
        is_dark: Callable[[], bool] = lambda: True,
        open_link: Callable[[str], None] = lambda _url: None,
        connect_signer: Callable[[], None] = lambda: None,
        show_status: Callable[[str, int], None] = lambda _text, _ms: None,
        directory: Optional[MembershipDirectory] = None,
        api_factory: Optional[Callable[[object], object]] = None,
        window_factory: Optional[Callable[..., MembershipWindow]] = None,
        add_relay_job: Optional[Callable[[object], QObject]] = None,
        publish_list_job: Optional[Callable[[object], QObject]] = None,
        window_parent=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._profile_provider = profile_provider
        self._relay_pool = relay_pool
        self._session_pool = session_pool
        self._relay_directory = relay_directory
        self._is_dark = is_dark
        self._open_link = open_link
        self._connect_signer = connect_signer
        self._show_status = show_status
        self._api_factory = api_factory or self._default_api
        self._window_factory = window_factory or MembershipWindow
        self._add_relay_job = add_relay_job or self._default_add_relay_job
        self._publish_list_job = publish_list_job or self._default_publish_list_job
        # The window's Qt parent: the main window, so it stays above it.
        self._window_parent = window_parent if window_parent is not None else parent

        self.directory = directory or MembershipDirectory(parent=self)
        self.directory.resolved.connect(self._on_resolved)

        self._window: Optional[MembershipWindow] = None
        self._announced: set = set()
        # The benefits last announced through benefits_changed, by account.
        # Nothing is known before the roster answers, which is no benefits.
        self._published: tuple = (self._active_key(), NO_BENEFITS)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(lambda: self.refresh(force=True))

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Resolve the account already signed in when the app opened (it
        never passes through the connect flow), and keep the roster fresh."""
        self.refresh()
        self._timer.start()

    def refresh(self, *, force: bool = False) -> None:
        """Ask for the active account's membership. ``force`` fetches the
        roster even while the cached one is fresh."""
        profile = self._profile_provider()
        if profile is not None:
            self.directory.resolve(profile.user_pubkey, force=force)

    def account_changed(self, profile) -> None:
        """A different account is active (connected, selected). Its
        membership is resolved, and an open window that spoke for nobody
        or for another account follows it."""
        if profile is not None:
            self.directory.resolve(profile.user_pubkey)
        self._check_benefits()
        self._follow_identity()

    # ------------------------------------------------------------------ #
    # What the active account is entitled to                              #
    # ------------------------------------------------------------------ #

    def benefits(self) -> Benefits:
        """What the active account is entitled to, as far as is known.

        An unresolved membership yields no benefits rather than blocking,
        so nothing in the app ever waits on a third-party host. A stale
        roster keeps its last answer while it is refreshed.
        """
        profile = self._profile_provider()
        if profile is None:
            return NO_BENEFITS
        known = self.directory.last_known_membership(profile.user_pubkey)
        return MEMBER_BENEFITS if known else NO_BENEFITS

    def entitled_relays(self) -> list:
        relay = self.benefits().relay
        return [relay] if relay else []

    def entitled_blossom_servers(self) -> list:
        server = self.benefits().blossom_server
        return [server] if server else []

    def quota(self, origin: str) -> Optional[int]:
        """The space a membership gives on its media server, in bytes."""
        benefits = self.benefits()
        if benefits.blossom_server and \
                url_safety.origin_of(benefits.blossom_server) == url_safety.origin_of(origin):
            return benefits.per_user_bytes or None
        return None

    def server_label(self, origin: str) -> Optional[str]:
        """The Media Library's name for a server, when it has a better one
        than its host: the members' server is the association's."""
        server = self.benefits().blossom_server
        if server and url_safety.origin_of(server) == url_safety.origin_of(origin):
            return SERVER_LABEL
        return None

    # ------------------------------------------------------------------ #
    # Roster answers                                                      #
    # ------------------------------------------------------------------ #

    def _active_key(self) -> str:
        profile = self._profile_provider()
        return _key(profile.user_pubkey) if profile is not None else ""

    def _on_resolved(self, pubkey: str, is_member: bool) -> None:
        key = _key(pubkey)
        if not key or key != self._active_key():
            return
        if is_member and key not in self._announced:
            self._announced.add(key)
            self._show_status(RECOGNIZED_MESSAGE, 6000)
        self._check_benefits()

    def _check_benefits(self) -> None:
        """Announce the active account's benefits, only when they changed."""
        now = (self._active_key(), self.benefits())
        if now == self._published:
            return
        self._published = now
        self.benefits_changed.emit()

    # ------------------------------------------------------------------ #
    # The membership window                                               #
    # ------------------------------------------------------------------ #

    @property
    def window(self) -> Optional[MembershipWindow]:
        """The membership window, while it exists."""
        window = self._window
        if window is not None and not shiboken6.isValid(window):
            self._window = window = None
        return window

    def _visible_window(self) -> Optional[MembershipWindow]:
        window = self.window
        return window if window is not None and window.isVisible() else None

    def open_window(self) -> None:
        """Nostr > EINUNDZWANZIG Membership: join, pay, or see what's active.

        Not modal, so paying from a phone never locks the editor. Opening
        it signs nothing; the window asks the signer only when the person
        continues.
        """
        window = self.window
        if window is not None and not window.isVisible():
            # Rebuilt on every fresh open: it then follows the current
            # theme and starts from where the membership stands now.
            window.deleteLater()
            self._window = window = None
        if window is None:
            profile = self._profile_provider()
            window = self._window_factory(
                self._api_factory(profile), **self._identity(profile),
                is_dark=self._is_dark(), parent=self._window_parent)
            window.setWindowFlag(Qt.Window, True)
            window.connect_requested.connect(self._on_connect_requested)
            window.link_activated.connect(self._open_link)
            window.member_confirmed.connect(self._on_member_confirmed)
            window.add_relay_requested.connect(self._on_add_relay)
            window.publish_relay_list_requested.connect(self._on_publish_relay_list)
            window.address_saved.connect(self._on_address_saved)
            window.data_erased.connect(self._on_data_erased)
            self._window = window
        window.show()
        window.raise_()
        window.activateWindow()

    def close_window(self) -> None:
        """The account is being left: never show one account's membership,
        invoice or name to another."""
        window = self._visible_window()
        if window is not None:
            window.close()

    def _identity(self, profile) -> dict:
        """What the window needs to know about ``profile``."""
        pubkey = profile.user_pubkey if profile is not None else None
        return {
            "pubkey": pubkey,
            # The last answer, fresh or not: a member whose roster is being
            # refreshed is still shown what they have, not a join prompt.
            "known_member": self.directory.last_known_membership(pubkey) if pubkey else None,
            "handle": self.directory.cached_handle(pubkey) if pubkey else None,
            "signs_locally": bool(getattr(profile, "is_local", False)),
        }

    def _follow_identity(self) -> None:
        """An open window speaks for the active account, whichever way that
        account arrived (the window's own Connect Signer, the Nostr menu,
        the account chip)."""
        window = self._visible_window()
        if window is None:
            return
        profile = self._profile_provider()
        if profile is None or _key(window.pubkey) == _key(profile.user_pubkey):
            return
        window.set_identity(self._api_factory(profile), **self._identity(profile))

    def _on_connect_requested(self) -> None:
        self._connect_signer()
        if self._profile_provider() is None:
            return
        if self._visible_window() is not None:
            # Continue joining in the same window, now as the new identity.
            self._follow_identity()
        else:
            self.open_window()

    def _on_member_confirmed(self, pubkey: str) -> None:
        """The association confirmed the membership: the relay and media
        server apply from now on, without waiting for the public roster."""
        self.directory.confirm_member(pubkey)
        self._on_resolved(pubkey, True)

    def _on_address_saved(self, pubkey: str, handle: str) -> None:
        self.directory.record_handle(pubkey, handle)

    def _on_data_erased(self, pubkey: str) -> None:
        """The association deleted this account's membership data. What the
        session learned from it no longer holds, and the roster decides
        again from a fresh answer."""
        self.directory.forget(pubkey)
        self._announced.discard(_key(pubkey))
        self.directory.resolve(pubkey, force=True)

    # ------------------------------------------------------------------ #
    # The members' relay in the account's relay list                      #
    # ------------------------------------------------------------------ #

    def _on_add_relay(self) -> None:
        self._run_relay_job(self._add_relay_job)

    def _on_publish_relay_list(self) -> None:
        self._run_relay_job(self._publish_list_job)

    def _run_relay_job(self, factory) -> None:
        profile = self._profile_provider()
        window = self.window
        if profile is None or window is None:
            return
        job = factory(profile)

        def finished(outcome: str) -> None:
            if window is self.window:
                window.set_relay_result(outcome_message(outcome), done=outcome in DONE,
                                        offer_list=outcome == NO_LIST)
            job.deleteLater()

        job.finished.connect(finished)
        job.start()

    # ------------------------------------------------------------------ #
    # Defaults                                                            #
    # ------------------------------------------------------------------ #

    def _default_api(self, profile) -> MembershipApi:
        # No parent: the window takes the client it is given, and deletes
        # it when it is replaced or the window goes.
        if profile is not None:
            sign = session_signer(self._session_pool, profile)
        else:
            def sign(_unsigned, _on_success, on_failure):
                on_failure("not connected")
        return MembershipApi(sign)

    def _default_add_relay_job(self, profile) -> RelayListAddition:
        return RelayListAddition(self._relay_pool, self._session_pool, profile,
                                 MEMBER_RELAY, directory=self._relay_directory, parent=self)

    def _default_publish_list_job(self, profile) -> RecommendedRelayList:
        return RecommendedRelayList(self._relay_pool, self._session_pool, profile,
                                    MEMBER_RELAY, directory=self._relay_directory, parent=self)
