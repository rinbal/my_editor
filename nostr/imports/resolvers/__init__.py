# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source resolvers, one module per source kind.

Each module exports a ``SourceResolver`` instance that
``registry._resolvers()`` places in match order (most specific first,
RSS last as the catch-all).
"""
