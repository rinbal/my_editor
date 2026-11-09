# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Submitting a work to a STANDUP publication, end to end encrypted.

A STANDUP publication (an EINUNDZWANZIG STANDUP Site with a Redaktion,
its editorial team) takes submissions through a submission server. The
server stores and forwards; it can read titles and states, never the
work, the conversation or a key. The shared rules both apps follow are
STANDUP's ``docs/editorial-contract.md`` (v1); its test vectors are
copied into ``tests/fixtures/standup_editorial_fixtures.json``. Section
numbers in this package's comments are the contract's.

No user interface lives here; the windows build on it.

- ``formats``: canonical JSON, the identifiers and hashes, the revision
  and its checks, the limits;
- ``crypto``: the content key, the revision envelope, SMF1 media files,
  key wraps with their binding checks;
- ``conversation``: rumors, seals and gift wraps (NIP-59) for messages,
  reactions, edits and structured notes, and reading them with every
  check;
- ``client``: the submission server's HTTP API (session, signed actions,
  the submission body), on the app's shared pipeline (``nostr/signed_api.py``);
- ``views``: the server's answers read defensively, receipts compared
  with what was sent, stored signed actions verified;
- ``trust``: the Redaktion's key event, checked, with the highest epoch
  seen kept per publication;
- ``lists``: the writer's encrypted submissions list and read state;
- ``watcher``: following the writer's submissions without a signer
  prompt, through their status keys.
"""
