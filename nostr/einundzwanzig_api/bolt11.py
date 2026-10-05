# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The amount a Lightning invoice asks for, and nothing more.

The association's invoice may carry a BOLT 11 payment request next to
its checkout page; the window shows its amount in sats. This reads the
human-readable part only. Pure: no Qt, no network.
"""

from __future__ import annotations

import re
from typing import Optional

from ..bech32 import bech32_decode


# BOLT 11: "ln" + currency prefix + optional amount, then the bech32
# separator. Longer prefixes first so "bcrt" is not read as "bc" + "rt".
_BOLT11_HRP = re.compile(r"\Aln(?:bcrt|bc|tbs|tb|sb)(?:(?P<amount>[0-9]+)(?P<unit>[munp]?))?\Z")

# Millisatoshis per unit of each multiplier, one bitcoin being 10^11 msat.
# ``p`` is a tenth of a millisatoshi and handled separately.
_MSAT_PER_UNIT = {"": 100_000_000_000, "m": 100_000_000, "u": 100_000, "n": 100}


def bolt11_amount_sats(bolt11: str) -> Optional[int]:
    """The amount a Lightning invoice asks for, in whole sats, or None.

    None when the invoice names no amount or is not a valid invoice at
    all (bad checksum included). A sub-satoshi remainder is rounded up,
    so the figure shown is never less than what the wallet will pay.
    This reads the amount only; it is not a payment-request decoder.
    """
    if not isinstance(bolt11, str):
        return None
    text = bolt11.strip()
    if text.lower().startswith("lightning:"):
        text = text[len("lightning:"):]
    try:
        hrp, _data = bech32_decode(text)
    except ValueError:
        return None
    match = _BOLT11_HRP.match(hrp)
    if match is None or match.group("amount") is None:
        return None
    digits, unit = match.group("amount"), match.group("unit")
    if digits.startswith("0"):
        return None
    amount = int(digits)
    if unit == "p":
        # BOLT 11 makes a pico amount that is not a whole millisatoshi
        # invalid, rather than rounding it.
        if amount % 10:
            return None
        msat = amount // 10
    else:
        msat = amount * _MSAT_PER_UNIT[unit]
    return -(-msat // 1000)
