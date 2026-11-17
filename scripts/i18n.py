#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep the translation files in step with the code.

    python scripts/i18n.py extract     write locale/myeditor.pot from the code
    python scripts/i18n.py update      extract, then bring every locale/*.po up
                                       to date (new texts empty, old ones kept,
                                       texts no longer used dropped)
    python scripts/i18n.py check       report untranslated texts, placeholders
                                       that differ from the English, and texts
                                       the code builds in a way that cannot be
                                       translated (an f-string passed to _())
    python scripts/i18n.py merge PART.po [...]
                                       copy the translations in PART.po into
                                       the .po of the same language (the file's
                                       Language header), then update
    python scripts/i18n.py part LANG OUT.po FILE.py [...]
                                       write OUT.po with the texts of these
                                       files only (and any translation the
                                       language has already), to translate one
                                       part of the app on its own
    python scripts/i18n.py check-part OUT.po FILE.py [...]
                                       the check, for a part file

The texts are found by reading the code, not by running it: every call
of ``_()``, ``ngettext()``, ``pgettext()`` or ``N_()`` with literal
strings. See docs/develop/translating.md.
"""

from __future__ import annotations

import ast
import os
import re
import symtable
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import i18n  # noqa: E402

LOCALE = os.path.join(ROOT, "locale")
POT = os.path.join(LOCALE, "myeditor.pot")
# Code that never shows a text to a person: tests, the build, the
# membership service (its words are for server logs), third-party code.
SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "tests", "sidecar", "packaging",
             "site", "scripts", "__pycache__", ".claude", "node_modules", "locale"}
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
FUNCTIONS = {"_": ("msgid",), "N_": ("msgid",), "ngettext": ("msgid", "msgid_plural"),
             "pgettext": ("msgctxt", "msgid")}

Key = Tuple[str, str]   # (context, msgid)


class Message:
    def __init__(self, context: str, msgid: str, plural: str = "") -> None:
        self.context = context
        self.msgid = msgid
        self.plural = plural
        self.references: List[str] = []

    @property
    def key(self) -> Key:
        return (self.context, self.msgid)


def python_files() -> List[str]:
    found = []
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".py"):
                found.append(os.path.join(directory, name))
    return found


def _shadowing(source: str, tree: ast.AST, rel: str) -> List[str]:
    """Scopes where ``_`` is a variable of its own (``path, _ = ...``,
    ``for _ in``, a parameter, an import) while _() is called in that
    scope or in one nested in it that sees the same ``_``: Python then
    calls the variable, and the window fails when it opens or a button
    is clicked. Read with Python's own scope rules (symtable), so a
    comprehension's own ``_`` or a nested scope's own one is told apart.
    The module itself counts too: rebinding the imported _ there breaks
    every function in the file."""
    try:
        top = symtable.symtable(source, rel, "exec")
    except SyntaxError:
        return []
    calls = {}      # (first line of a scope, kind) -> lines where _() is called
    scopes = [(tree, top)]
    problems = []

    def called_here(table) -> List[int]:
        """Lines of _() calls made directly in this scope."""
        node = _node_of(tree, table)
        if node is None:
            return []
        found = []
        stack = list(ast.iter_child_nodes(node)) if node is not tree else list(tree.body)
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                  ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp,
                                  ast.GeneratorExp)):
                continue
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                    and child.func.id == "_"):
                found.append(child.lineno)
            stack.extend(ast.iter_child_nodes(child))
        return found

    def sees_outer(table) -> bool:
        if "_" not in table.get_identifiers():
            return True
        symbol = table.lookup("_")
        return symbol.is_free() or (not symbol.is_local() and not symbol.is_parameter())

    def calls_through(table) -> List[int]:
        """_() calls in this scope and in the nested scopes that see its _."""
        lines = called_here(table)
        for child in table.get_children():
            if sees_outer(child):
                lines.extend(calls_through(child))
        return lines

    def visit(table) -> None:
        if "_" in table.get_identifiers():
            symbol = table.lookup("_")
            own = symbol.is_assigned() or symbol.is_parameter() or symbol.is_imported()
            if table.get_type() == "module":
                # Importing _ from i18n is the point; any other binding is not.
                own = symbol.is_assigned() and not _only_imported(tree)
            elif symbol.is_global() or symbol.is_free():
                own = False
            if own:
                lines = calls_through(table)
                if lines:
                    problems.append(
                        f"{rel}:{table.get_lineno()}: _ is used as a variable here and "
                        f"_() is called (line {min(lines)}); rename the variable "
                        "(for example to _unused)")
        for child in table.get_children():
            visit(child)

    visit(top)
    return problems


def _only_imported(tree: ast.AST) -> bool:
    """Whether the module code itself (not its functions, classes or
    comprehensions) never binds ``_`` other than by importing it."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            continue
        if isinstance(node, ast.Name) and node.id == "_" and isinstance(node.ctx, ast.Store):
            return False
        stack.extend(ast.iter_child_nodes(node))
    return True


def _node_of(tree: ast.AST, table):
    """The AST node a symbol table describes, found by kind and line."""
    if table.get_type() == "module":
        return tree
    kinds = {"function": (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
             "class": (ast.ClassDef,)}.get(table.get_type(),
                                           (ast.ListComp, ast.SetComp, ast.DictComp,
                                            ast.GeneratorExp))
    for node in ast.walk(tree):
        if isinstance(node, kinds) and getattr(node, "lineno", None) == table.get_lineno():
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name != table.get_name():
                    continue
            return node
    return None


def extract(only: Optional[List[str]] = None) -> Tuple["OrderedDict[Key, Message]", List[str]]:
    """Every text in the code (or in the files ``only`` names), and every
    call that cannot be read."""
    messages: "OrderedDict[Key, Message]" = OrderedDict()
    problems: List[str] = []
    paths = python_files() if not only else [os.path.join(ROOT, p) for p in only]
    for path in paths:
        rel = os.path.relpath(path, ROOT)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            problems.append(f"{rel}: cannot parse ({exc})")
            continue
        problems.extend(_shadowing(source, tree, rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "i18n"
                else "")
            if name not in FUNCTIONS:
                continue
            fields = FUNCTIONS[name]
            args = node.args[:len(fields)]
            where = f"{rel}:{node.lineno}"
            if any(isinstance(a, ast.JoinedStr) for a in args):
                problems.append(f"{where}: an f-string passed to {name}() is never "
                                "found in a translation; use {{name}} and .format()")
                continue
            if any(isinstance(a, (ast.BinOp, ast.Call)) for a in args):
                problems.append(f"{where}: a text built before {name}() looks it up is "
                                "never found in a translation; pass the literal text "
                                "and fill it in after")
                continue
            if len(args) < len(fields) or not all(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
                continue        # a variable: its text is marked where it is written
            values = dict(zip(fields, (a.value for a in args)))
            if not values["msgid"].strip():
                continue
            message = Message(values.get("msgctxt", ""), values["msgid"],
                              values.get("msgid_plural", ""))
            existing = messages.setdefault(message.key, message)
            if message.plural and not existing.plural:
                existing.plural = message.plural
            existing.references.append(where)
    return messages, problems


# --------------------------------------------------------------------------- #
# Writing .pot and .po files                                                  #
# --------------------------------------------------------------------------- #

def _quote(text: str) -> str:
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
               .replace("\r", "\\r"))
    if "\n" not in escaped:
        return f'"{escaped}"'
    lines = escaped.split("\n")
    parts = [f'"{line}\\n"' for line in lines[:-1]]
    if lines[-1]:
        parts.append(f'"{lines[-1]}"')
    return '""\n' + "\n".join(parts)


def _header(language: str, plural_forms: str) -> str:
    return (
        'msgid ""\n'
        'msgstr ""\n'
        '"Project-Id-Version: MyEditor\\n"\n'
        f'"Language: {language}\\n"\n'
        '"MIME-Version: 1.0\\n"\n'
        '"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Content-Transfer-Encoding: 8bit\\n"\n'
        f'"Plural-Forms: {plural_forms}\\n"\n'
    )


def _entry(message: Message, forms: Optional[List[str]], *, fuzzy: bool = False) -> str:
    lines = []
    for ref in message.references[:8]:
        lines.append(f"#: {ref}")
    flags = []
    if fuzzy:
        flags.append("fuzzy")
    if PLACEHOLDER.search(message.msgid):
        flags.append("python-brace-format")
    if flags:
        lines.append("#, " + ", ".join(flags))
    if message.context:
        lines.append(f"msgctxt {_quote(message.context)}")
    lines.append(f"msgid {_quote(message.msgid)}")
    if message.plural:
        lines.append(f"msgid_plural {_quote(message.plural)}")
        forms = forms or ["", ""]
        for index, form in enumerate(forms):
            lines.append(f"msgstr[{index}] {_quote(form)}")
    else:
        lines.append(f"msgstr {_quote((forms or [''])[0])}")
    return "\n".join(lines) + "\n"


def write_pot(messages) -> None:
    os.makedirs(LOCALE, exist_ok=True)
    body = [_header("", "nplurals=2; plural=(n != 1);")]
    for message in messages.values():
        body.append(_entry(message, None))
    with open(POT, "w", encoding="utf-8") as f:
        f.write("\n".join(body))


def _read_po(path: str) -> Tuple[Dict[Key, Tuple[List[str], bool]], str, str]:
    """Translations by key (forms, fuzzy), the language and plural forms."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    found: Dict[Key, Tuple[List[str], bool]] = {}
    language, plural_forms = "", "nplurals=2; plural=(n != 1);"
    for entry in i18n._po_entries(text):
        if entry.get("msgid") == "" and not entry.get("msgctxt"):
            header = entry.get("msgstr", [""])[0]
            match = re.search(r"Language:\s*([^\n]*)", header)
            language = match.group(1).strip() if match else ""
            match = re.search(r"Plural-Forms:\s*([^\n]*)", header)
            if match:
                plural_forms = match.group(1).strip()
            continue
        found[(entry.get("msgctxt", ""), entry["msgid"])] = (
            entry.get("msgstr", [""]), bool(entry.get("fuzzy")))
    return found, language, plural_forms


def update_po(path: str, messages) -> Tuple[int, int]:
    """Rewrite ``path`` for the texts in the code. Returns (translated, missing)."""
    old, language, plural_forms = _read_po(path) if os.path.exists(path) else (
        {}, os.path.splitext(os.path.basename(path))[0], "nplurals=2; plural=(n != 1);")
    language = language or os.path.splitext(os.path.basename(path))[0]
    body = [_header(language, plural_forms)]
    done = missing = 0
    for message in messages.values():
        forms, fuzzy = old.get(message.key, (None, False))
        if forms and all(forms) and not fuzzy:
            done += 1
        else:
            missing += 1
        body.append(_entry(message, forms, fuzzy=fuzzy))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    return done, missing


def po_files() -> List[str]:
    if not os.path.isdir(LOCALE):
        return []
    return [os.path.join(LOCALE, n) for n in sorted(os.listdir(LOCALE)) if n.endswith(".po")]


# --------------------------------------------------------------------------- #
# Checking                                                                    #
# --------------------------------------------------------------------------- #

def placeholders(text: str) -> List[str]:
    return sorted(PLACEHOLDER.findall(text))


def check(messages, paths: Optional[List[str]] = None) -> List[str]:
    problems = []
    for path in (paths if paths is not None else po_files()):
        name = os.path.relpath(path, ROOT)
        found, _language, plural_forms = _read_po(path)
        counted = re.search(r"nplurals\s*=\s*(\d+)", plural_forms or "")
        nplurals = int(counted.group(1)) if counted else 2
        for key, message in messages.items():
            forms, fuzzy = found.get(key, ([], False))
            label = f"{name}: {message.msgid[:60]!r}"
            if not forms or not all(forms) or fuzzy:
                problems.append(f"{label} is not translated")
                continue
            if message.plural and len(forms) != nplurals:
                problems.append(f"{label} has {len(forms)} plural forms, the language "
                                f"needs {nplurals}")
                continue
            expected = placeholders(message.msgid)
            for index, form in enumerate(forms):
                source = message.plural if (message.plural and index > 0) else message.msgid
                want = placeholders(source)
                if message.plural:
                    # A plural form may leave out the number ("eine Datei").
                    if not set(placeholders(form)) <= set(want) | set(expected):
                        problems.append(f"{label} form {index} has placeholders "
                                        f"{placeholders(form)}, the English {want}")
                elif placeholders(form) != want:
                    problems.append(f"{label} has placeholders {placeholders(form)}, "
                                    f"the English {want}")
                if "\u2014" in form:
                    problems.append(f"{label} uses an em dash")
    return problems


def merge(parts: List[str]) -> None:
    """Copy translations from partial .po files into locale/<language>.po."""
    for part in parts:
        found, language, _plural = _read_po(part)
        if not language:
            raise SystemExit(f"{part} has no Language header")
        target = os.path.join(LOCALE, f"{language}.po")
        existing, _lang, plural_forms = _read_po(target) if os.path.exists(target) else (
            {}, language, "nplurals=2; plural=(n != 1);")
        existing.update({k: v for k, v in found.items() if v[0] and all(v[0])})
        # Write them back through a temporary catalog; update() then
        # orders and filters by the code.
        body = [_header(language, plural_forms)]
        for (context, msgid), (forms, fuzzy) in existing.items():
            message = Message(context, msgid, "x" if len(forms) > 1 else "")
            if len(forms) > 1:
                message.plural = msgid
            body.append(_entry(message, forms, fuzzy=fuzzy))
        os.makedirs(LOCALE, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(body))


def write_part(language: str, out: str, files: List[str]) -> int:
    messages, problems = extract(files)
    known = {}
    target = os.path.join(LOCALE, f"{language}.po")
    if os.path.exists(target):
        known = _read_po(target)[0]
    if os.path.exists(out):
        known.update(_read_po(out)[0])
    body = [_header(language, "nplurals=2; plural=(n != 1);")]
    for message in messages.values():
        forms, fuzzy = known.get(message.key, (None, False))
        body.append(_entry(message, forms, fuzzy=fuzzy))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    for problem in problems:
        print(problem)
    print(f"{len(messages)} texts in {out}")
    return 1 if problems else 0


def main(argv: List[str]) -> int:
    command = argv[1] if len(argv) > 1 else "check"
    if command == "part":
        return write_part(argv[2], argv[3], argv[4:])
    if command == "check-part":
        messages, problems = extract(argv[3:])
        problems += check(messages, [os.path.join(ROOT, argv[2])])
        for problem in problems:
            print(problem)
        print(f"{len(messages)} texts, {len(problems)} problems")
        return 1 if problems else 0
    messages, problems = extract()
    if command == "merge":
        merge(argv[2:])
        command = "update"
    if command in ("extract", "update"):
        write_pot(messages)
        print(f"{len(messages)} texts in {os.path.relpath(POT, ROOT)}")
    if command == "update":
        for path in po_files():
            done, missing = update_po(path, messages)
            print(f"{os.path.relpath(path, ROOT)}: {done} translated, {missing} missing")
    if command == "check":
        problems += check(messages)
    for problem in problems:
        print(problem)
    return 1 if problems and command == "check" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
