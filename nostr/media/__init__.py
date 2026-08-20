# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document media assets: local-first images the editor can rely on.

The editor knows nothing about Blossom. It hands bytes to
:class:`AssetManager`, gets back a :class:`DocumentAsset` whose key is
already resolvable, and inserts it. Uploading happens afterwards as
enrichment: a missing network, a declined signer or a dead server can
never remove an image from a document.

  assets   ASSET_SCHEME + key helpers, AssetState, DocumentAsset,
           AssetIndex (the persisted, versioned metadata file)
  manager  AssetManager: ingest, resolution, the single-flight upload
           queue, and the export view exporters consume

Identity is always the sha256 of the bytes, so the same picture pasted
twice is one asset with one upload.
"""

from .assets import (  # noqa: F401
    ASSET_SCHEME,
    CURRENT_INDEX_VERSION,
    INDEX_FILE,
    LEGAL_TRANSITIONS,
    AssetIndex,
    AssetState,
    DocumentAsset,
    asset_key,
    is_asset_key,
    is_sha256,
    parse_asset_key,
)
from .manager import (  # noqa: F401
    MAX_ASSET_BYTES,
    AssetErrorCodes,
    AssetManager,
    ExportAsset,
)

__all__ = [
    "ASSET_SCHEME",
    "CURRENT_INDEX_VERSION",
    "INDEX_FILE",
    "LEGAL_TRANSITIONS",
    "MAX_ASSET_BYTES",
    "AssetErrorCodes",
    "AssetIndex",
    "AssetManager",
    "AssetState",
    "DocumentAsset",
    "ExportAsset",
    "asset_key",
    "is_asset_key",
    "is_sha256",
    "parse_asset_key",
]
