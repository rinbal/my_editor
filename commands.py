# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One list of everything the editor can do, behind every way of doing it.

The menus, the keyboard shortcuts, the Keyboard Shortcuts window and,
later, the toolbar and the slash menu all read the same list, so a
command cannot be in one and missing from (or named differently in)
another, and two commands can never claim the same shortcut.

A :class:`Command` describes one thing: its title (title case, as menus
show it), its group (the menu it belongs to, and the section it is
listed under in the Keyboard Shortcuts window), its shortcut, extra
words to find it by, and whether it needs a Nostr account. The
:class:`CommandRegistry` turns each into one QAction, owned by the
window and added to it, so its shortcut works even while the menu bar
is hidden (full screen, compact themes).

Nostr commands follow the Nostr state (see nostr_state.py): a surface
that should only show them once Nostr is in use asks
:meth:`CommandRegistry.available`.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QKeySequence

from i18n import _

# A tuple holds several keys, the one menus show first ("Ctrl+W", "Ctrl+F4").
ShortcutSpec = Union[None, str, QKeySequence, QKeySequence.StandardKey, Tuple[str, ...]]


def undo_words(action: str = "", *, named: Optional[bool] = None) -> str:
    """Edit > Undo's words for taking back ``action`` (an N_ word:
    "Typing", "Paste", "Skip"): "Undo Typing" where the platform's own
    apps name what Undo takes back (macOS), else "Undo", as Notepad and
    GNOME's Text Editor say. ``named`` True names it on every platform
    (a toolbar button's tip, as Word's says "Undo Typing" on Windows)."""
    if action and (named if named is not None else sys.platform == "darwin"):
        return _("Undo {action}").format(action=_(action))
    return _("Undo")


def redo_words(action: str = "", *, named: Optional[bool] = None) -> str:
    """Edit > Redo's words, by the same rules as undo_words."""
    if action and (named if named is not None else sys.platform == "darwin"):
        return _("Redo {action}").format(action=_(action))
    return _("Redo")

# The groups, in the order the Keyboard Shortcuts window lists them. They
# are keys, so they stay English; the window translates them where it
# shows them (shortcuts_dialog.py).
FILE = "File"
EDIT = "Editing"
FORMAT = "Formatting"
INSERT = "Insert"
SEARCH = "Search"
VIEW = "View"
NAVIGATION = "Navigation"   # tabs and panes, by key (a Mac's Window menu has two)
NOSTR = "Nostr"
WINDOW = "Window"       # a Mac's Window menu
HELP = "Help"
GROUP_ORDER: Tuple[str, ...] = (FILE, EDIT, FORMAT, INSERT, SEARCH, VIEW, NAVIGATION, NOSTR,
                                WINDOW, HELP)


# The menu commands left without a key, on purpose, and why: everything
# else in a menu has one (tests/test_shortcut_map.py holds the two lists
# together). Every one of them is reached with the keyboard through its
# menu all the same. (command id, or a prefix ending in ".", where: "all",
# "mac" or "other" for Windows and Linux, the reason.)
NO_SHORTCUT: Tuple[Tuple[str, str, str], ...] = (
    ("edit.delete", "all", "The Delete key itself does it."),
    ("edit.footnote_jump", "all",
     "It follows the footnote at the caret; Command-click (Ctrl-click) on the mark does "
     "the same, and so does the context menu."),
    ("edit.markdown_shortcuts", "all", "A preference, set once."),
    ("search.use_selection", "other",
     "A Mac convention (the find text shared by every app); Windows and Linux have none."),
    ("file.page_setup", "other", "Rare, and no Windows or Linux convention has a key for it."),
    ("file.knit_pdf", "all",
     "R Markdown is for the few; RStudio, whose key Knit to HTML has, has none for it."),
    ("file.rmd_toolchain", "all", "A one-off setup."),
    ("file.revert", "all",
     "Rare, and it asks before it throws anything away; TextEdit's Revert To has no key "
     "either."),
    ("format.color.", "all",
     "Choices from the Color menu; Clear Formatting (Ctrl+D) takes color away too."),
    ("format.code_language.", "all",
     "A choice from a list, offered right after Insert Code Block."),
    ("format.sheet.", "all",
     "Choices for a workbook's sheets, also in the sheet bar's context menu; Numbers has "
     "no keys for them, and deleting a sheet is destructive."),
    ("format.table.", "all",
     "Tab or Return at the end of a table adds a row; no convention has keys for the "
     "others, and deleting rows, columns or the table is destructive."),
    ("view.appearance.system", "all", "A choice; Ctrl+Shift+T switches light and dark."),
    ("view.appearance.light", "all", "A choice; Ctrl+Shift+T switches light and dark."),
    ("view.appearance.dark", "all", "A choice; Ctrl+Shift+T switches light and dark."),
    ("view.toolbar", "other",
     "Set once and remembered; only the Mac has a convention for it (Option-Command-T)."),
    ("view.monospace_writing", "all", "A look preference, set once and remembered."),
    ("format.bigger", "all",
     "Option-Command-0 to 3 (Ctrl+0 to 3) choose each style; Bigger and Smaller step "
     "through them from the format bar and the menu."),
    ("format.smaller", "all",
     "Option-Command-0 to 3 (Ctrl+0 to 3) choose each style; Bigger and Smaller step "
     "through them from the format bar and the menu."),
    ("edit.start_dictation", "all",
     "The system's own key starts it: Fn twice on a Mac, Windows+H on Windows."),
    ("edit.speech.", "all", "Apple's Edit > Speech has no keys; the system's Speak "
     "Selection key reads any selection."),
    ("file.password", "all",
     "Set once for a document, as Pages' File > Set Password… is; it asks for a password "
     "each time anyway."),
    ("view.actual_size", "other",
     "Ctrl+0 is Body there, as in LibreOffice; Zoom Out and Zoom In step back to it, and "
     "Settings shows the size."),
    ("view.paper_mode", "all", "A look preference, set once and remembered."),
    ("view.highlight_line", "all", "A look preference, set once and remembered."),
    ("view.background.", "all", "A look preference, set once and remembered."),
    ("view.language.", "all", "Set once; it applies at the next start."),
    ("view.sidebar", "other",
     "Windows and Linux have no convention for it; the sidebar button at the start of the "
     "tab row and the View menu show and hide it."),
    ("view.file_list", "other",
     "Windows and Linux have no convention for it; the button at the start of the tab "
     "row and the View menu show and hide it."),
    ("file.new_folder", "all",
     "The Finder's Shift-Command-N opens Imports here (since 3.3); the list's own "
     "button and context menu make folders too."),
    ("file.rename", "all", "The Finder gives Rename no key in its menu either."),
    ("file.duplicate", "all",
     "Command-D (Ctrl+D) clears formatting here, as since 3.3; the list's context menu "
     "duplicates too."),
    ("file.reveal", "all", "The Finder's own Show in Enclosing Folder key moves up a "
     "folder, which has no meaning here; a one-off."),
    ("file.quick_look", "all",
     "Space in the file list, as in the Finder; the Finder's Command-Y is Redo here, as "
     "on every platform's Edit menu."),
    ("file.open_as_text", "all",
     "Rare: for the few text files MyEditor shows another way (a CSV table, an SVG "
     "picture); the tab's menu and Open With have it too."),
    ("file.add_folder", "other",
     "The Finder's Add to Sidebar key (Control-Command-T) has no Windows or Linux "
     "counterpart; a one-off."),
    ("nostr.insert_mention", "all",
     "Typing @ at the start of a word does it, as in every app that mentions people."),
    ("nostr.submit_publication", "all",
     "Once per finished text, and never by accident: a sheet asks where to first."),
    ("nostr.draft_relays", "all", "A one-off account setup."),
    ("nostr.imports_badge", "all", "A preference, set once."),
    ("nostr.membership", "all", "A one-off."),
    ("nostr.connect", "all", "A one-off account setup."),
    ("nostr.edit_profile", "all", "A one-off account setup."),
    ("nostr.backup_account", "all", "A one-off account setup."),
    ("nostr.create_account", "all",
     "A one-off that makes a new key; Apple's guidelines give such commands no easy key."),
    ("nostr.restore_account", "all",
     "A one-off that replaces the key in use; no easy key, as Apple's guidelines ask."),
    ("nostr.move_to_signer", "all",
     "Removes the key from this computer; destructive, so no easy key."),
    ("nostr.sign_out", "all", "Ends the account's use here; no easy key, as for Log Out."),
    ("window.zoom", "mac", "Apple's Window menu gives it no key."),
    ("window.bring_all", "mac", "Apple's Window menu gives it no key."),
    ("help.welcome", "all", "A one-off."),
    ("help.install", "all", "A one-off."),
    ("help.check_updates", "all", "A one-off."),
    ("help.show_logs", "all", "A one-off, for a bug report."),
    ("help.about", "all", "A one-off."),
)


def no_shortcut_reason(command_id: str, *, platform: Optional[str] = None) -> str:
    """Why ``command_id`` has no key on ``platform`` (this computer's by
    default), or "" when it should have one."""
    mac = (platform or sys.platform) == "darwin"
    for name, where, reason in NO_SHORTCUT:
        if where == "mac" and not mac or where == "other" and mac:
            continue
        if command_id == name or (name.endswith(".") and command_id.startswith(name)):
            return reason
    return ""


@dataclass(frozen=True)
class Command:
    """One thing the editor can do."""

    id: str                          # "file.save": stable, never shown
    title: str                       # "Save As…": what menus show
    group: str                       # one of GROUP_ORDER
    shortcut: ShortcutSpec = None
    checkable: bool = False
    nostr: bool = False              # needs a Nostr account to mean anything
    keywords: Tuple[str, ...] = ()   # other words people use for it
    # The words the Keyboard Shortcuts window uses, when they differ from
    # the menu title ("Open file" for "Open…").
    listed_as: str = ""
    # Where macOS puts it. Only About, Settings and Quit move to the
    # application menu; every other command stays where it was put,
    # whatever its title says in any language (Qt would otherwise guess
    # from the words).
    role: QAction.MenuRole = QAction.MenuRole.NoRole
    # The slash menu's group it is offered in (slash_menu.SECTIONS); ""
    # keeps it out of the slash menu.
    slash_section: str = ""
    # Its title in the Insert menu and the slash menu, where the menu's
    # name says the verb ("Mention…" for "Insert Mention…"), as Pages'
    # Insert menu says "Table", never "Insert Table"; "" for its title.
    insert_title: str = ""


def platform_keys(mac: ShortcutSpec, other: ShortcutSpec, *,
                  platform: Optional[str] = None) -> ShortcutSpec:
    """The shortcut for this computer: ``mac`` on a Mac, ``other`` elsewhere.

    For the few commands whose keys differ between the platforms' own
    conventions (Apple's Human Interface Guidelines on a Mac; on Windows
    and Linux, Ctrl+Alt is AltGr, which types characters on many
    keyboards). Qt writes Command as Ctrl and Option as Alt on a Mac, so
    ``mac`` is "Ctrl+Alt+1" for Option-Command-1.
    """
    return mac if (platform or sys.platform) == "darwin" else other


def standard_and(standard: QKeySequence.StandardKey, *keys: str) -> Tuple[str, ...]:
    """Every key the system binds to ``standard``, then ``keys`` where the
    system's list lacks them (F3 for Find Next on a Mac, as MyEditor 3.3
    had it everywhere). Menus show the system's first key."""
    texts = all_key_texts(standard)
    return tuple(texts) + tuple(k for k in keys if k not in texts)


def all_key_texts(shortcut: ShortcutSpec) -> List[str]:
    """Every key combination a shortcut answers to: a standard key (Copy,
    Find Next) has the platform's whole set, anything else one."""
    if isinstance(shortcut, QKeySequence.StandardKey):
        texts = [s.toString(QKeySequence.SequenceFormat.PortableText)
                 for s in QKeySequence.keyBindings(shortcut)]
        return [t for t in texts if t]
    if isinstance(shortcut, tuple):
        return [t for t in (key_text(keys) for keys in shortcut) if t]
    keys = key_text(shortcut)
    return [keys] if keys else []


def key_text(shortcut: ShortcutSpec) -> str:
    """``Ctrl+Shift+S``: a shortcut the way the Keyboard Shortcuts window
    writes it (Qt maps Ctrl to Command on a Mac)."""
    if shortcut is None:
        return ""
    if isinstance(shortcut, tuple):
        return key_text(shortcut[0]) if shortcut else ""
    sequence = shortcut if isinstance(shortcut, QKeySequence) else QKeySequence(shortcut)
    return sequence.toString(QKeySequence.SequenceFormat.PortableText)


@dataclass
class _Entry:
    command: Command
    action: QAction
    hidden_from_list: bool = False
    extra_keys: List[str] = field(default_factory=list)


class CommandRegistry(QObject):
    """The commands of one window, in the order they were added."""

    def __init__(self, window, parent: Optional[QObject] = None) -> None:
        super().__init__(parent if parent is not None else window)
        self._window = window
        self._entries: Dict[str, _Entry] = {}
        self._by_keys: Dict[str, str] = {}

    # -- adding ------------------------------------------------------------

    def add(self, command: Command, *, triggered: Optional[Callable] = None,
            toggled: Optional[Callable[[bool], None]] = None,
            checked: Optional[bool] = None, enabled: Optional[bool] = None,
            listed: bool = True) -> QAction:
        """Make ``command`` an action of the window and return it.

        ``triggered`` runs on a click or the shortcut; ``toggled`` gets the
        new state of a checkable command. ``listed=False`` keeps it out of
        the Keyboard Shortcuts window (for an alias of a listed command).
        Raises ValueError for a second command with the same id or the
        same shortcut: a shortcut that means two things means neither.
        """
        if command.id in self._entries:
            raise ValueError(f"command {command.id!r} is already registered")
        if command.group not in GROUP_ORDER:
            raise ValueError(f"command {command.id!r} has an unknown group {command.group!r}")
        all_keys = all_key_texts(command.shortcut)
        if isinstance(command.shortcut, QKeySequence.StandardKey):
            # A standard key's extra combinations differ by desktop (KDE's
            # Full Screen is Ctrl+Shift+F as well as F11); one a command
            # names is left to it, as long as the standard key keeps one.
            free = [keys for keys in all_keys if keys not in self._by_keys]
            if free:
                all_keys = free
        for keys in all_keys:
            if keys in self._by_keys:
                raise ValueError(f"{keys} is taken by {self._by_keys[keys]!r}; "
                                 f"{command.id!r} cannot have it too")
        action = QAction(command.title, self._window)
        action.setObjectName(command.id)
        action.setMenuRole(command.role)
        if isinstance(command.shortcut, QKeySequence.StandardKey):
            # Every combination the platform uses for it (F3 and Command-G
            # for Find Next on a Mac), not only the first.
            action.setShortcuts([QKeySequence(keys) for keys in all_keys])
        elif isinstance(command.shortcut, tuple):
            action.setShortcuts([QKeySequence(keys) for keys in command.shortcut])
        elif command.shortcut is not None:
            action.setShortcut(command.shortcut if isinstance(command.shortcut, QKeySequence)
                               else QKeySequence(command.shortcut))
        if command.checkable:
            action.setCheckable(True)
            if checked is not None:
                action.setChecked(checked)
        if enabled is not None:
            action.setEnabled(enabled)
        if triggered is not None:
            action.triggered.connect(lambda _checked=False: triggered())
        if toggled is not None:
            action.toggled.connect(toggled)
        # Window-wide, so the shortcut works without the menu bar.
        self._window.addAction(action)
        self._entries[command.id] = _Entry(command, action, hidden_from_list=not listed)
        for keys in all_keys:
            self._by_keys[keys] = command.id
        return action

    def use_insert_titles(self, menu) -> None:
        """While ``menu`` (named for the verb: Insert) is open, its commands
        show their insert_title; the titles they had come back as it
        closes. The same actions, so one key and one state for each,
        whichever menu shows it."""
        shown: Dict[QAction, str] = {}

        def opening() -> None:
            for action in menu.actions():
                entry = self._entries.get(action.objectName())
                if entry is not None and entry.command.insert_title and action not in shown:
                    shown[action] = action.text()
                    action.setText(entry.command.insert_title)

        def closing() -> None:
            for action, text in shown.items():
                action.setText(text)
            shown.clear()

        menu.aboutToShow.connect(opening)
        menu.aboutToHide.connect(closing)

    def refuse_clashes(self, where: str, keys: Iterable[str], *,
                       yielding: Iterable[str] = ()) -> None:
        """Raise ValueError when a key that ``where`` binds for itself (the
        PDF reader's) is bound twice there, or is also a command's, unless
        that command is one of ``yielding``: commands that are off wherever
        ``where`` has its keys (the editing commands, in a PDF tab). Two
        shortcuts on one key are ambiguous to Qt, and then neither works."""
        yielding = set(yielding)
        seen = set()
        for spec in keys:
            text = key_text(spec)
            if text in seen:
                raise ValueError(f"{text} is bound twice in {where}")
            seen.add(text)
            owner = self._by_keys.get(text)
            if owner is not None and owner not in yielding:
                raise ValueError(f"{text} is taken by {owner!r}; {where} cannot have it too")

    # -- reading -----------------------------------------------------------

    def action(self, command_id: str) -> QAction:
        return self._entries[command_id].action

    def command(self, command_id: str) -> Command:
        return self._entries[command_id].command

    def __contains__(self, command_id: str) -> bool:
        return command_id in self._entries

    def commands(self, group: Optional[str] = None) -> List[Command]:
        return [e.command for e in self._entries.values()
                if group is None or e.command.group == group]

    def available(self, *, nostr: bool) -> List[Command]:
        """What a surface that follows the Nostr state may offer: every
        command, and the Nostr ones only while Nostr is in use."""
        return [c for c in self.commands() if nostr or not c.nostr]

    def find(self, text: str, *, nostr: bool) -> List[Command]:
        """Commands whose title or keywords contain every word of ``text``,
        for a slash menu or a command search. Enabled ones only."""
        words = [w for w in text.lower().split() if w]
        found = []
        for command in self.available(nostr=nostr):
            if not self.action(command.id).isEnabled():
                continue
            haystack = " ".join((command.title, command.listed_as, *command.keywords)).lower()
            if all(word in haystack for word in words):
                found.append(command)
        return found

    def rank(self, query: str, *, nostr: bool,
             sections: Tuple[str, ...] = ("text", "lists", "blocks", "insert")
             ) -> List[Command]:
        """What the slash menu offers for ``query`` (the words typed after
        the slash), best first: commands in a slash section, enabled,
        Nostr ones only while Nostr is in use. A command whose title or
        keywords have a word starting with each word of the query comes
        before one that only contains them; otherwise the menu's order
        (group by group, then the order the commands were made) stays."""
        order = {section: i for i, section in enumerate(sections)}
        offered = [c for c in self.available(nostr=nostr)
                   if c.slash_section in order and self.action(c.id).isEnabled()]
        offered.sort(key=lambda c: order[c.slash_section])
        words = query.lower().split()
        if not words:
            return offered
        scored = []
        for command in offered:
            text = " ".join((command.title, command.listed_as, *command.keywords)).lower()
            tokens = re.split(r"[^\w]+", text)
            if all(any(t.startswith(w) for t in tokens) for w in words):
                scored.append((2, command))
            elif all(w in text for w in words):
                scored.append((1, command))
        scored.sort(key=lambda pair: -pair[0])
        return [command for _score, command in scored]

    def shortcut_groups(self, extra: Iterable[Tuple[str, Iterable[Tuple[str, str]]]] = (),
                        *, nostr: bool = True) -> List[Tuple[str, List[Tuple[str, str]]]]:
        """``[(group, [(keys, words), ...]), ...]`` for the Keyboard
        Shortcuts window: every listed command that has a shortcut, plus
        ``extra`` rows for keys that are not commands (Tab indents, Esc
        closes the find bar), each group in GROUP_ORDER, then any group
        only ``extra`` has."""
        rows: Dict[str, List[Tuple[str, str]]] = {}
        for entry in self._entries.values():
            command = entry.command
            keys = key_text(command.shortcut)
            if not keys or entry.hidden_from_list or (command.nostr and not nostr):
                continue
            words = command.listed_as or command.title.rstrip("…").rstrip(".").rstrip()
            rows.setdefault(command.group, []).append((keys, words))
        for group, items in extra:
            rows.setdefault(group, []).extend(items)
        ordered = [g for g in GROUP_ORDER if g in rows]
        ordered += [g for g in rows if g not in GROUP_ORDER]
        return [(group, rows[group]) for group in ordered]
