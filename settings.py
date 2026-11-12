#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import Optional

import user_dirs
from atomic_file import read_json, write_json

# A file of its own (a test's); None: settings.json in the person's config
# folder, found each time it is read or written (user_dirs).
_SETTINGS_FILE: Optional[str] = None


def _settings_file() -> str:
    return _SETTINGS_FILE or str(user_dirs.config_dir() / "settings.json")


def load_settings() -> dict:
    """Return the stored settings dict (tolerates a missing, unreadable
    or corrupt file)."""
    return read_json(_settings_file(), dict)


def save_setting(key: str, value) -> None:
    """Set a single setting, preserving the rest of the stored settings."""
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


def save_settings(settings: dict) -> bool:
    """Overwrite the settings file with the given dict, in one step.
    False when it could not be written; the old settings are kept."""
    return write_json(_settings_file(), settings, indent=2)
