# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Blossom media client for my_editor.

Implements the parts of the spec this app actually needs: BUD-01 blob
retrieval, BUD-02 upload, BUD-03 server lists, BUD-04 mirroring, BUD-08
nip94 capture, BUD-11 authorization and BUD-12 list and delete. Uploads
go to a primary server, are mirrored to the remaining configured
servers, and are deduped by sha256. Signing rides on the existing NIP-46
``BunkerClient`` so no new key material is introduced.

Module layout matches STANDUP one-for-one:

  servers  : DEFAULT_BLOSSOM_SERVERS, BLOSSOM_SERVER_INFO,
             RECOMMENDED_ADDON_SERVERS, BLOSSOM_MAX_FILE_SIZE
  auth     : build_blossom_auth_event / to_auth_header (kind 24242)
  errors   : stable codes and the friendly copy they select
  plan     : plan_upload (size pre-flight + reroute logic)
  settings : load/save the user's custom server list
  client   : BlossomClient (HTTP layer, QNetworkAccessManager)
  store    : MediaStore (in-memory library, fetch coalescing, upload
             orchestration with sign + mirror)
"""

from .servers import (  # noqa: F401
    BLOSSOM_MAX_FILE_SIZE,
    BLOSSOM_SERVER_INFO,
    BLOSSOM_UNPUBLISHED_LIMIT_FALLBACK,
    DEFAULT_BLOSSOM_SERVERS,
    RECOMMENDED_ADDON_SERVERS,
)
