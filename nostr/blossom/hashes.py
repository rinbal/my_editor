# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one rule for reading a sha256 out of a blob URL (BUD-03).

BUD-03 states the rule once, and every Blossom client is expected to
apply it verbatim: "When extracting the SHA256 hash from the URL clients
MUST use the last occurrence of a 64 char hex string" (specs/bud-03.md
line 40). The same section lists six URL shapes that must all yield the
same hash, including one whose path also carries a 64 character pubkey.

The scan is deliberately plain: non-overlapping, not anchored to a path
segment, case-insensitive going in and lowercase coming out. Being
cleverer than the rule would be a defect rather than an improvement,
because interoperability here means agreeing with other clients on the
parse, not parsing better than they do.

Pure stdlib, no Qt, and nothing else in this package is imported: the
publish walk, the asset layer and the recovery ladder all call it, and
none of them should have to load protocol code to ask what hash a URL
names.
"""

from __future__ import annotations

import re
from typing import List, Optional


_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def hashes_in_url(url: str) -> List[str]:
    """Every 64 character hex run in ``url``, in order, lowercased.

    Exposed alongside :func:`hash_from_url` because the recovery and
    verification paths want to reason about how many runs there are,
    not only which one wins.
    """
    if not isinstance(url, str) or not url:
        return []
    return _HEX64_RE.findall(url.lower())


def hash_from_url(url: str) -> Optional[str]:
    """The sha256 ``url`` names per BUD-03, or None when it names none.

    None means "this URL does not carry a hash", never "compute one".
    A caller that needs a hash for bytes it holds must hash the bytes.
    """
    runs = hashes_in_url(url)
    return runs[-1] if runs else None


def blob_url(origin: str, sha256: str) -> str:
    """The canonical ``<origin>/<sha256>`` address of a blob.

    BUD-01 serves every endpoint from the root of the domain, which is
    what makes this form safe to construct for a server that confirmed
    the blob without ever having been told this exact string.
    """
    return f"{(origin or '').rstrip('/')}/{(sha256 or '').lower()}"


def url_agrees_with_hash(url: str, sha256: str) -> bool:
    """Whether ``url`` may be used as an address for ``sha256``.

    This is a VERIFICATION predicate, not the extraction rule. Query and
    fragment are stripped first, then the strict last-occurrence rule is
    applied to what remains, so a signed or tokenised URL such as
    ``https://cdn.example/<sha>.png?token=<64 hex>`` still agrees with
    the blob it serves. A URL with no hex run at all agrees: plenty of
    servers address blobs by an opaque path, and refusing those would
    break retrieval for no security gain.

    The looser rule "our hash appears anywhere among the runs" was
    considered and rejected. It accepts ``<origin>/<ours>/x/<theirs>``,
    whose last run is a different blob, and every other client applying
    the BUD-03 MUST would resolve that stored URL to the wrong hash. We
    would be creating an interoperability defect in order to fix one.
    """
    if not isinstance(url, str) or not url:
        return False
    wanted = (sha256 or "").lower()
    if not wanted:
        return False
    path_only = url.split("#", 1)[0].split("?", 1)[0]
    runs = hashes_in_url(path_only)
    if not runs:
        return True
    return runs[-1] == wanted
