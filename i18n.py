# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MyEditor in the reader's language.

Every word the app shows goes through one of four functions:

    _("Save")                                  a sentence or a label
    ngettext("{n} file", "{n} files", n)       one that depends on a number
    pgettext("verb", "Open")                   one that needs a hint to translate
    N_("Save")                                 marked here, translated later

with placeholders written ``{name}`` and filled with ``.format(name=...)``
after the lookup, never before (an f-string would be looked up with the
values already in it, and never be found).

Translations live in ``locale/<language>.po``, the standard gettext
format that Poedit, Weblate and every translation tool reads, and are
read straight from there: there is no compile step to forget.
``locale/myeditor.pot`` lists every text to translate; scripts/i18n.py
updates it and the .po files from the code. docs/develop/translating.md says how
to add a language.

The language is chosen once, before any window is built, because many
texts are module-level constants: :func:`install` must run before the
modules that hold them are imported (main.py does this), and a change in
the language setting applies on the next start. ``system`` (the default)
follows the operating system's languages, in its order of preference,
and English is used for anything a language has not translated yet.

Qt's own words (the buttons of a file dialog, "Cancel" in a button box)
come from Qt's translations, installed by :func:`install_qt_translations`.

The pseudo language ``pseudo`` makes every text about 40 percent longer
and accented, to find a window that would cut off a longer translation.
"""

from __future__ import annotations

import gettext as _gettext
import os
import re
import sys
from typing import Callable, Dict, List, Optional, Tuple

DOMAIN = "myeditor"
SYSTEM = "system"
ENGLISH = "en"
PSEUDO = "pseudo"
# The setting's key in settings.json.
SETTING = "language"
# Names of the languages in their own language, for the menu.
LANGUAGE_NAMES: Dict[str, str] = {ENGLISH: "English", "de": "Deutsch"}


def _locale_dir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "locale")


# --------------------------------------------------------------------------- #
# Reading a .po file                                                           #
# --------------------------------------------------------------------------- #

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "a": "\a", "b": "\b",
            "f": "\f", "v": "\v"}
_ESCAPE_RE = re.compile(r'\\(.)')


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise ValueError(f"not a quoted string: {text[:40]!r}")
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), text[1:-1])


class Catalog:
    """One language's translations, read from a .po file.

    Keys are ``(context, msgid)``; a value is the list of forms (one, or
    one per plural form). Fuzzy and empty translations are left out, so
    English shows instead of a guess.
    """

    def __init__(self, entries: Dict[Tuple[str, str], List[str]],
                 plural: Callable[[int], int]) -> None:
        self.entries = entries
        self.plural = plural

    @classmethod
    def from_po(cls, text: str) -> "Catalog":
        entries: Dict[Tuple[str, str], List[str]] = {}
        header = ""
        for entry in _po_entries(text):
            if entry.get("msgid") == "" and not entry.get("msgctxt"):
                header = entry.get("msgstr", [""])[0]
                continue
            if entry.get("fuzzy"):
                continue
            forms = entry.get("msgstr", [])
            if not forms or not all(forms):
                continue
            entries[(entry.get("msgctxt", ""), entry["msgid"])] = forms
        return cls(entries, _plural_rule(header))

    def lookup(self, context: str, msgid: str, n: Optional[int] = None) -> Optional[str]:
        forms = self.entries.get((context, msgid))
        if not forms:
            return None
        if n is None or len(forms) == 1:
            return forms[0]
        try:
            index = int(self.plural(n))
        except Exception:  # noqa: BLE001
            index = _english_plural(n)
        return forms[index] if 0 <= index < len(forms) else forms[-1]


def _po_entries(text: str):
    """Entries of a .po file as dicts: msgctxt, msgid, msgid_plural,
    msgstr (a list), fuzzy. Obsolete (#~) entries are skipped."""
    entry: dict = {}
    key: Optional[str] = None
    index = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if "msgid" in entry:
                yield entry
            entry, key = {}, None
            continue
        if line.startswith("#~"):
            continue
        if line.startswith("#,"):
            if "msgid" in entry:
                yield entry
                entry, key = {}, None
            if "fuzzy" in line:
                entry["fuzzy"] = True
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgctxt "):
            if "msgid" in entry:
                yield entry
                entry = {}
            key = "msgctxt"
            entry[key] = _unquote(line[len("msgctxt "):])
        elif line.startswith("msgid_plural "):
            key = "msgid_plural"
            entry[key] = _unquote(line[len("msgid_plural "):])
        elif line.startswith("msgid "):
            if "msgid" in entry:
                yield entry
                entry = {}
            key = "msgid"
            entry[key] = _unquote(line[len("msgid "):])
        elif line.startswith("msgstr["):
            close = line.index("]")
            index = int(line[len("msgstr["):close])
            if not 0 <= index < _MAX_PLURAL_FORMS:
                raise ValueError(f"plural form {index} is out of range")
            forms = entry.setdefault("msgstr", [])
            while len(forms) <= index:
                forms.append("")
            forms[index] = _unquote(line[close + 1:])
            key = "msgstr[]"
        elif line.startswith("msgstr "):
            entry["msgstr"] = [_unquote(line[len("msgstr "):])]
            key = "msgstr[]"
            index = 0
        elif line.startswith('"') and key is not None:
            value = _unquote(line)
            if key == "msgstr[]":
                entry["msgstr"][index] += value
            else:
                entry[key] += value
        else:
            raise ValueError(f"unreadable line in .po file: {raw[:60]!r}")
    if "msgid" in entry:
        yield entry


# No language has more plural forms than this (Arabic has six).
_MAX_PLURAL_FORMS = 16


def _english_plural(n: int) -> int:
    return 0 if n == 1 else 1


def _plural_rule(header: str) -> Callable[[int], int]:
    """The language's plural rule from the Plural-Forms header, tried on
    every count from 0 to 200 first: a rule that fails or answers a form
    the language does not have is replaced by English's, so a broken
    header cannot break a window that shows a count."""
    match = re.search(r"plural=([^;\n]+)", header or "")
    forms = re.search(r"nplurals\s*=\s*(\d+)", header or "")
    if not match:
        return _english_plural
    try:
        rule = _gettext.c2py(match.group(1).strip())
        count = int(forms.group(1)) if forms else _MAX_PLURAL_FORMS
        if all(0 <= int(rule(n)) < count for n in range(201)):
            return rule
    except Exception:  # noqa: BLE001, whatever the header holds
        pass
    return _english_plural


# --------------------------------------------------------------------------- #
# The active language                                                          #
# --------------------------------------------------------------------------- #

_catalogs: List[Catalog] = []      # in order of preference; English is the fallback
_language: str = ENGLISH
_pseudo: bool = False


def _pseudo_text(text: str) -> str:
    """Accented and about 40 percent longer, placeholders kept intact."""
    accents = str.maketrans("aeiouAEIOUcnyCNY", "àéîõüÀÉÎÕÜçñýÇÑÝ")
    out = []
    for part in re.split(r"(\{[^{}]*\})", text):
        if part.startswith("{") and part.endswith("}"):
            out.append(part)
        else:
            out.append(part.translate(accents))
    body = "".join(out)
    pad = "~" * max(1, len(text) * 2 // 5)
    return f"[{body} {pad}]" if text.strip() else text


def _translate(context: str, msgid: str, plural: Optional[str] = None,
               n: Optional[int] = None) -> str:
    for catalog in _catalogs:
        found = catalog.lookup(context, msgid, n)
        if found is not None:
            return found
    text = msgid if plural is None or n == 1 else plural
    return _pseudo_text(text) if _pseudo else text


def _(message: str) -> str:
    """``message`` in the active language."""
    return _translate("", message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """The form of a text that fits the number ``n``."""
    return _translate("", singular, plural, n)


def pgettext(context: str, message: str) -> str:
    """``message`` as meant in ``context`` ("verb", "menu"), for a word
    that translates differently depending on what it means."""
    return _translate(context, message)


def N_(message: str) -> str:
    """Mark ``message`` for translation without translating it now; the
    place that shows it calls ``_()`` on it then."""
    return message


def language() -> str:
    """The language texts are shown in: ``en``, ``de``, ..., or ``pseudo``."""
    return _language


_LANGUAGE_CODE = re.compile(r"\A[a-z]{2,3}\Z")


def available(locale_dir: Optional[str] = None) -> List[str]:
    """English, and every language with a translation in ``locale/``
    (files named by a two- or three-letter code, like ``de.po``)."""
    found = [ENGLISH]
    try:
        names = sorted(os.listdir(locale_dir or _locale_dir()))
    except OSError:
        names = []
    for name in names:
        code, ext = os.path.splitext(name)
        if ext == ".po" and _LANGUAGE_CODE.match(code) and code not in found:
            found.append(code)
    return found


def system_languages() -> List[str]:
    """The operating system's languages, most preferred first, as codes
    (``de``, ``en``)."""
    try:
        from PySide6.QtCore import QLocale
        names = QLocale.system().uiLanguages()
    except Exception:
        names = []
    codes: List[str] = []
    for name in names:
        code = name.replace("_", "-").split("-")[0].lower()
        if code and code not in codes:
            codes.append(code)
    return codes


def resolve(choice: str, locale_dir: Optional[str] = None) -> str:
    """The language a setting means: the system's first language that has
    a translation (English otherwise), or the one chosen."""
    if choice == PSEUDO:
        return PSEUDO
    have = available(locale_dir)
    if choice and choice != SYSTEM:
        return choice if choice in have else ENGLISH
    for code in system_languages():
        if code in have:
            return code
    return ENGLISH


def load_catalog(code: str, locale_dir: Optional[str] = None) -> Optional[Catalog]:
    path = os.path.join(locale_dir or _locale_dir(), f"{code}.po")
    try:
        # utf-8-sig: an editor may have saved the file with a BOM.
        with open(path, "r", encoding="utf-8-sig") as f:
            return Catalog.from_po(f.read())
    except Exception:  # noqa: BLE001, a broken translation must never stop the app
        return None


def install(choice: str = SYSTEM, *, locale_dir: Optional[str] = None) -> str:
    """Show every text from now on in the language ``choice`` means
    (``system``, ``en``, ``de``, ``pseudo``). Returns the language used.

    Call before the modules whose constants hold texts are imported."""
    global _catalogs, _language, _pseudo
    code = resolve(choice, locale_dir)
    _pseudo = code == PSEUDO
    _catalogs = []
    if code not in (ENGLISH, PSEUDO):
        catalog = load_catalog(code, locale_dir)
        if catalog is None:
            code = ENGLISH
        else:
            _catalogs.append(catalog)
    _language = code
    return code


def install_qt_translations(app) -> None:
    """Qt's own words (file dialogs, standard buttons) in the same language,
    and MyEditor's own word where Qt's is out of date."""
    from PySide6.QtCore import QLibraryInfo, QTranslator
    keep = getattr(app, "_myeditor_translators", [])
    if _language not in (ENGLISH, PSEUDO):
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        for name in ("qtbase", "qt"):
            translator = QTranslator(app)
            if translator.load(f"{name}_{_language}", path):
                app.installTranslator(translator)
                keep.append(translator)
    # Installed last, so Qt asks it first.
    own_words = _own_words_translator(app)
    app.installTranslator(own_words)
    keep.append(own_words)
    app._myeditor_translators = keep


def _own_words_translator(parent):
    """A translator for the few words MyEditor says instead of Qt's.

    A Mac's application menu titles the Settings command itself, with the
    word macOS used before Ventura: Qt asks for "Preferences..." in the
    contexts below and never shows the command's own text. macOS 13 and
    the HIG call it Settings, and so do MyEditor's window and docs. Every
    other text is left to Qt's translations: the answer is None (a null
    text, which Qt reads as "not translated here"; an empty one would be
    taken as the translation)."""
    from PySide6.QtCore import QTranslator

    class OwnWords(QTranslator):
        def translate(self, context, source_text, disambiguation=None, n=-1):
            if context in ("QMenuBar", "MAC_APPLICATION_MENU") \
                    and source_text == "Preferences...":
                return _("Settings\u2026")
            return None

        def isEmpty(self) -> bool:
            return False

    return OwnWords(parent)


# --------------------------------------------------------------------------- #
# Numbers and dates, the language's way                                         #
# --------------------------------------------------------------------------- #

def display_locale():
    """The QLocale numbers and dates are written in: the language MyEditor
    speaks, not the system's, so a German window never shows an English
    month name nor an English one a German one. The pseudo language
    writes them as English does."""
    from PySide6.QtCore import QLocale
    return QLocale(ENGLISH if _language == PSEUDO else _language)


def _day_pattern(locale, *, short_month: bool, year: bool) -> str:
    """The locale's own long date without the weekday ("MMMM d, yyyy" in
    English, "d. MMMM yyyy" in German), with "MMM" for ``short_month``,
    and without the year and its separator unless ``year``."""
    from PySide6.QtCore import QLocale
    pattern = locale.dateFormat(QLocale.FormatType.LongFormat)
    pattern = re.sub(r"\s*dddd[,.]?\s*", " ", pattern, count=1).strip(" ,")
    if not year:
        pattern = re.sub(r"[\s,./-]*y+[\s,./-]*", " ", pattern).strip(" ,/-")
    return pattern.replace("MMMM", "MMM") if short_month else pattern


def format_day(day, *, short_month: bool = False, year: bool = True) -> str:
    """``day`` (a QDate, a QDateTime or a datetime.date) the way the
    language writes a day: "January 6, 2027", "6. Januar 2027"; with
    ``short_month``, "Jan 6, 2027" for a list row; without ``year``,
    "Jan 6", "6. Jan.", for a day of this year in a list. Never a padded
    day from a fixed pattern ("06 January 2027")."""
    from PySide6.QtCore import QDate, QDateTime
    if isinstance(day, QDateTime):
        day = day.date()
    elif not isinstance(day, QDate):
        day = QDate(day.year, day.month, day.day)
    locale = display_locale()
    return locale.toString(day, _day_pattern(locale, short_month=short_month, year=year))


def format_moment(moment) -> str:
    """A QDateTime as the day and the clock time, the language's way:
    "January 6, 2027 at 2:05 PM", "6. Januar 2027 um 14:05"."""
    from PySide6.QtCore import QLocale
    locale = display_locale()
    time = locale.toString(moment.time(), locale.timeFormat(QLocale.FormatType.ShortFormat))
    return _("{date} at {time}").format(date=format_day(moment), time=time)


def chosen_language() -> str:
    """The language setting as saved (``system`` when never chosen). The
    environment variable MYEDITOR_LANGUAGE overrides it, for testing."""
    from_env = os.environ.get("MYEDITOR_LANGUAGE", "").strip()
    if from_env:
        return from_env
    try:
        from settings import load_settings
        return str(load_settings().get(SETTING) or SYSTEM)
    except Exception:
        return SYSTEM
