# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Blossom media client for my_editor.

A minimal, BUD-02 / BUD-04 Blossom implementation: upload to a primary
server, mirror to secondaries in parallel, list / dedupe by sha256,
delete. Signing rides on the existing NIP-46 ``BunkerClient`` so no new
key material is introduced.

Module layout matches STANDUP one-for-one:

  servers  — DEFAULT_BLOSSOM_SERVERS, BLOSSOM_SERVER_INFO,
             RECOMMENDED_ADDON_SERVERS, BLOSSOM_MAX_FILE_SIZE
  auth     — build_blossom_auth_event / to_auth_header (kind 24242)
  plan     — plan_upload (size pre-flight + reroute logic)
  settings — load/save the user's custom server list
  client   — BlossomClient (HTTP layer, QNetworkAccessManager)
  store    — MediaStore (in-memory library, fetch coalescing, upload
             orchestration with sign + mirror)
"""

from .servers import (  # noqa: F401
    BLOSSOM_MAX_FILE_SIZE,
    BLOSSOM_SERVER_INFO,
    BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK,
    DEFAULT_BLOSSOM_SERVERS,
    RECOMMENDED_ADDON_SERVERS,
)
