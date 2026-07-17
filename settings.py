#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os

_SETTINGS_FILE = os.path.expanduser("~/.config/my_editor/settings.json")


def load_settings() -> dict:
    """Return the stored settings dict (tolerates a missing or corrupt file)."""
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_setting(key: str, value) -> None:
    """Set a single setting, preserving the rest of the stored settings."""
    settings = load_settings()
    settings[key] = value
    save_settings(settings)


def save_settings(settings: dict) -> None:
    """Overwrite the settings file with the given dict."""
    os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
