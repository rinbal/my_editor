# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Waiting for a membership payment to settle.

Polls through a :class:`~.client.MembershipApi`; see :class:`PaymentWatcher`.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .client import MembershipApi
from .models import ApiError, ErrorCode, Invoice


# --------------------------------------------------------------------------- #
# Waiting for a payment                                                        #
# --------------------------------------------------------------------------- #

# Polling after a payment, mirroring the reference client: every five
# seconds for two minutes.
POLL_INTERVAL_MS: int = 5_000
POLL_ATTEMPTS: int = 24

# A quota wait longer than this ends the watch instead of stretching it:
# the user is better told to come back later than shown a spinner.
_MAX_POLL_BACKOFF_SECONDS: int = 60

# Failures worth another poll. Anything else (the signer said no or is
# gone, the key or the signature was refused, nothing on record) will not
# be cured by asking again, and asking again would only prompt the user's
# phone for nothing.
_POLL_AGAIN = frozenset({
    ErrorCode.OFFLINE,
    ErrorCode.TIMEOUT,
    ErrorCode.SERVER,
    ErrorCode.BAD_RESPONSE,
    ErrorCode.RATE_LIMITED,
})


class PaymentWatcher(QObject):
    """Polls for a settled payment after the user has paid.

    Polls ``refresh``, not ``me``. Both need a signature, and with a
    phone signer every signature may be a prompt, so one call per check
    matters. ``refresh`` is also the right one: it re-reads the invoice
    from the payment processor and, when the payment notification to
    the association was lost, books the payment itself. Polling ``me``
    would wait forever on exactly that failure. Once :attr:`paid` fires,
    one ``me`` call fetches the new membership.

    One request at a time: the next poll is scheduled only after the
    previous one answered. The timer is a seam; anything with
    ``timeout``, ``setSingleShot``, ``start(ms)`` and ``stop`` will do.

    Signals:
      paid(Invoice)        settled. Polling has stopped.
      still_waiting(int)   that many checks done, not settled yet.
      expired(Invoice)     the invoice expired or was invalidated, so
                           waiting is pointless; start a new checkout.
      gave_up()            the attempts ran out. Offer a manual check.
      failed(ApiError)     a failure another poll would not cure.
    """

    paid = Signal(object)
    still_waiting = Signal(int)
    expired = Signal(object)
    gave_up = Signal()
    failed = Signal(object)

    def __init__(
        self,
        api: MembershipApi,
        year: int,
        *,
        interval_ms: int = POLL_INTERVAL_MS,
        max_attempts: int = POLL_ATTEMPTS,
        timer=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._api = api
        self._year = year
        self._interval_ms = max(1, int(interval_ms))
        self._max_attempts = max(1, int(max_attempts))
        self._timer = timer if timer is not None else QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._poll)
        self._running = False
        self._polling = False
        self._attempts = 0
        self._token = 0
        self._last_error: Optional[ApiError] = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def last_error(self) -> Optional[ApiError]:
        """The most recent failure that did not stop the watcher."""
        return self._last_error

    def start(self, *, poll_now: bool = False) -> None:
        """Start (or restart) watching. The first check waits one
        interval unless ``poll_now``."""
        self.stop()
        self._running = True
        self._attempts = 0
        self._last_error = None
        if poll_now:
            self._poll()
        else:
            self._timer.start(self._interval_ms)

    def check_now(self) -> None:
        """Check immediately, for a "Check again" button. Counts as an
        attempt. Does nothing while a check is already in flight."""
        if not self._running or self._polling:
            return
        self._timer.stop()
        self._poll()

    def stop(self) -> None:
        """Stop watching. An answer still in flight is ignored."""
        self._running = False
        self._polling = False
        self._token += 1
        self._timer.stop()

    # -- internals ---------------------------------------------------------

    def _poll(self) -> None:
        if not self._running or self._polling:
            return
        self._polling = True
        token = self._token
        self._api.refresh_payment(
            self._year,
            lambda invoice, t=token: self._on_invoice(t, invoice),
            lambda error, t=token: self._on_error(t, error),
        )

    def _on_invoice(self, token: int, invoice: Invoice) -> None:
        if token != self._token:
            return
        self._polling = False
        self._attempts += 1
        self._last_error = None
        if invoice.payment.paid:
            self._finish()
            self.paid.emit(invoice)
        elif invoice.expired:
            self._finish()
            self.expired.emit(invoice)
        else:
            self._again(self._interval_ms)

    def _on_error(self, token: int, error: ApiError) -> None:
        if token != self._token:
            return
        self._polling = False
        self._attempts += 1
        too_long = (error.code == ErrorCode.RATE_LIMITED
                    and (error.retry_after or 0) > _MAX_POLL_BACKOFF_SECONDS)
        if error.code not in _POLL_AGAIN or too_long:
            self._finish()
            self.failed.emit(error)
            return
        self._last_error = error
        delay = self._interval_ms
        if error.retry_after:
            delay = max(delay, error.retry_after * 1000)
        self._again(delay)

    def _again(self, delay_ms: int) -> None:
        if self._attempts >= self._max_attempts:
            self._finish()
            self.gave_up.emit()
            return
        self.still_waiting.emit(self._attempts)
        # A slot on still_waiting may have stopped or restarted us.
        if self._running and not self._polling and not self._timer.isActive():
            self._timer.start(int(delay_ms))

    def _finish(self) -> None:
        self._running = False
        self._timer.stop()
