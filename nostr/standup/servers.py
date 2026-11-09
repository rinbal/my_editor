# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where STANDUP requests may go.

A submission server's address comes from the publication's verified key
event (``server``, contract 8.1), never from this app: the association
runs the instance and names it, so no address is built in and without
one nothing is sent (the UI says "kommt vom Verein"). Only https, except
plain http to exactly ``127.0.0.1`` when a developer allows it
(``MYEDITOR_STANDUP_LOCAL_HTTP=1``) for a local STANDUP, which
``MYEDITOR_STANDUP_SERVER`` can name.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..signed_api import usable_base_url

# A developer's own server (a local STANDUP). Unset in production, where
# the server comes from the publication's verified key event.
ENV_SERVER: str = "MYEDITOR_STANDUP_SERVER"
# "1" allows plain http to exactly 127.0.0.1, for a local STANDUP.
ENV_LOCAL_HTTP: str = "MYEDITOR_STANDUP_LOCAL_HTTP"
_LOCAL_HTTP_HOSTS = frozenset({"127.0.0.1"})


def local_http_allowed() -> bool:
    return os.environ.get(ENV_LOCAL_HTTP, "").strip() == "1"


def usable_server_url(value: Any, *, local_http: Optional[bool] = None) -> str:
    """``value`` as a submission server's address, without a trailing
    slash, or ``""``: https to any host; plain http only to exactly
    ``127.0.0.1``, and only with ``local_http`` (default: the developer
    setting). No user, password, query or fragment."""
    allowed = local_http_allowed() if local_http is None else local_http
    return usable_base_url(value, http_hosts=_LOCAL_HTTP_HOSTS if allowed else frozenset())


def configured_server() -> str:
    """The server a developer named, or ``""``. Never a built-in address."""
    return usable_server_url(os.environ.get(ENV_SERVER, ""))


__all__ = ["ENV_LOCAL_HTTP", "ENV_SERVER", "configured_server", "local_http_allowed",
           "usable_server_url"]
