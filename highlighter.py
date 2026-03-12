#!/usr/bin/env python3
"""
Basic syntax highlighter with language detection by file extension.
"""

import re
import os
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont


_EXT_TO_LANG: dict[str, str] = {
    '.py': 'python', '.pyw': 'python',
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.jsx': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript',
    '.css': 'css', '.scss': 'css', '.sass': 'css', '.less': 'css',
    '.json': 'json', '.jsonc': 'json',
    '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash', '.fish': 'bash',
    '.c': 'c', '.h': 'c',
    '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
    '.rs': 'rust',
    '.go': 'go',
    '.java': 'java',
    '.yaml': 'yaml', '.yml': 'yaml',
    '.toml': 'toml',
    '.sql': 'sql',
    '.html': 'html', '.htm': 'html',
    '.md': 'markdown', '.markdown': 'markdown',
}

LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    'python':     'Python',
    'javascript': 'JavaScript',
    'typescript': 'TypeScript',
    'html':       'HTML',
    'css':        'CSS',
    'json':       'JSON',
    'bash':       'Shell',
    'c':          'C',
    'cpp':        'C++',
    'rust':       'Rust',
    'go':         'Go',
    'java':       'Java',
    'yaml':       'YAML',
    'toml':       'TOML',
    'sql':        'SQL',
    'markdown':   'Markdown',
}

# Token colors for dark / light themes
_DARK: dict[str, str] = {
    'keyword':   '#569CD6',
    'keyword2':  '#C586C0',
    'string':    '#CE9178',
    'comment':   '#6A9955',
    'number':    '#B5CEA8',
    'func':      '#DCDCAA',
    'type':      '#4EC9B0',
    'decorator': '#C586C0',
    'tag':       '#569CD6',
    'attr':      '#9CDCFE',
    'var':       '#9CDCFE',
    'key':       '#9CDCFE',
}

_LIGHT: dict[str, str] = {
    'keyword':   '#0070C1',
    'keyword2':  '#AF00DB',
    'string':    '#A31515',
    'comment':   '#008000',
    'number':    '#098658',
    'func':      '#795E26',
    'type':      '#267F99',
    'decorator': '#AF00DB',
    'tag':       '#800000',
    'attr':      '#FF0000',
    'var':       '#001080',
    'key':       '#001080',
}


def detect_language(path: str | None) -> str | None:
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    return _EXT_TO_LANG.get(ext)


def detect_language_from_content(text: str) -> str | None:
    """Heuristic language detection from text content (for untitled buffers)."""
    if not text or not text.strip():
        return None

    lines = text.splitlines()
    first = lines[0].strip() if lines else ''

    # Shebang
    if first.startswith('#!'):
        if 'python' in first:
            return 'python'
        if 'node' in first or 'deno' in first:
            return 'javascript'
        if 'bash' in first or first.endswith('/sh') or '/sh ' in first:
            return 'bash'
        if 'zsh' in first or 'fish' in first:
            return 'bash'

    sample = lines[:60]
    scores: dict[str, int] = {}
    hits: dict[str, int] = {}   # number of distinct lines that matched each lang

    def inc(lang: str, n: int = 1):
        scores[lang] = scores.get(lang, 0) + n

    def hit(lang: str, n: int = 1):
        """Score points AND count this as a matching line."""
        inc(lang, n)
        hits[lang] = hits.get(lang, 0) + 1

    for line in sample:
        s = line.strip()
        if not s:
            continue
        # Python — only strong structural indicators
        if re.match(r'(def \w+\s*\(|class \w+[\s:(]|from \w[\w.]* import|elif |except[\s:]|async def \w)', s):
            hit('python', 2)
        if re.match(r'(if __name__|@\w+\s*$|@\w+\()', s):
            hit('python')
        # JavaScript
        if re.match(r'(const |let |var |function \w|export (default )?|module\.exports)', s):
            hit('javascript', 2)
        if re.search(r'=>\s*[\{\(]', s):
            hit('javascript')
        if re.match(r"import .+ from ['\"]", s):
            hit('javascript', 2)
        # TypeScript (on top of JS)
        if re.match(r'(interface \w|type \w+ =|declare |readonly |abstract class)', s):
            hit('typescript', 3)
        if re.search(r':\s*(string|number|boolean|void|any|unknown)\b', s):
            hit('typescript', 2)
        # Rust
        if re.match(r'(fn \w|let mut |impl \w|pub fn |mod \w+\s*\{|struct \w|enum \w|trait \w|use \w+::)', s):
            hit('rust', 2)
        # Go
        if re.match(r'(func \w|type \w+ struct|import \()', s):
            hit('go', 2)
        if s == 'package main':
            hit('go', 5)
        elif re.match(r'package \w+', s):
            hit('go', 2)
        # Bash
        if re.match(r'(if \[|if \[\[)', s):
            hit('bash', 2)
        if re.match(r'\$\(', s):
            hit('bash', 2)
        if s in ('do', 'done', 'fi', 'then', 'esac'):
            hit('bash', 2)
        # C
        if re.match(r'#include\s*[<"]', s):
            hit('c', 3)
        if re.match(r'(int main\s*\(|printf\s*\(|typedef )', s):
            hit('c', 2)
        # C++
        if re.match(r'(std::|cout\s*<<|cin\s*>>|namespace \w|template\s*<)', s):
            hit('cpp', 2)
            inc('c')
        # Java
        if re.match(r'(public class \w|private \w|protected \w|@Override|System\.out\.)', s):
            hit('java', 2)
        # HTML
        if re.match(r'(<html|<!DOCTYPE|<head|<body|<div)', s, re.IGNORECASE):
            hit('html', 3)
        elif re.match(r'<\w[\w-]*[\s>/]', s):
            hit('html')
        # CSS
        if re.match(r'[\w.#:\*\[\]>~+]+\s*\{', s):
            hit('css', 2)
        if re.match(r'[\w-]+\s*:\s*[^{}\n]+;', s):
            hit('css', 2)
        # JSON
        if re.match(r'"[\w-]+"\s*:', s):
            hit('json', 2)
        # SQL — only unambiguous statement starters
        if re.match(r'(SELECT\s+\*?[\w,\s]+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|DROP\s+TABLE)\b', s, re.IGNORECASE):
            hit('sql', 4)
        # YAML — skip, too ambiguous for arbitrary text

    if not scores:
        return None

    # TypeScript absorbs JavaScript when TS-specific patterns are found
    if scores.get('typescript', 0) >= 4:
        scores.pop('javascript', None)
    # C++ absorbs C when C++-specific patterns found
    if scores.get('cpp', 0) >= 4:
        scores.pop('c', None)

    best = max(scores, key=scores.get)
    # Require: total score ≥ 8 AND at least 2 distinct matching lines
    if scores[best] >= 8 and hits.get(best, 0) >= 2:
        return best
    return None


class SyntaxHighlighter(QSyntaxHighlighter):
    """Regex-based syntax highlighter supporting common languages."""

    def __init__(self, document, language: str, is_dark: bool = True):
        super().__init__(document)
        self._language = language
        self._is_dark = is_dark
        # (pattern, format) for single-line rules
        self._rules: list[tuple[re.Pattern, QTextCharFormat]] = []
        # (start_pattern, end_pattern, format) for multi-line constructs
        self._ml_rules: list[tuple[re.Pattern, re.Pattern, QTextCharFormat]] = []
        self._build_rules()

    def set_theme(self, is_dark: bool):
        if self._is_dark != is_dark:
            self._is_dark = is_dark
            self._build_rules()
            self.rehighlight()

    # ------------------------------------------------------------------
    # Format helpers
    # ------------------------------------------------------------------

    def _fmt(self, color_key: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        palette = _DARK if self._is_dark else _LIGHT
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(palette.get(color_key, '#D4D4D4')))
        if bold:
            fmt.setFontWeight(QFont.Bold)
        if italic:
            fmt.setFontItalic(True)
        return fmt

    def _add(self, pattern: str, color_key: str, bold=False, italic=False, flags: int = 0):
        self._rules.append((re.compile(pattern, flags), self._fmt(color_key, bold, italic)))

    def _add_ml(self, start: str, end: str, color_key: str, italic: bool = False):
        self._ml_rules.append((
            re.compile(start),
            re.compile(end),
            self._fmt(color_key, italic=italic),
        ))

    # ------------------------------------------------------------------
    # Rule sets per language
    # ------------------------------------------------------------------

    def _build_rules(self):
        self._rules = []
        self._ml_rules = []
        lang = self._language
        dispatch = {
            'python':     self._python,
            'javascript': self._javascript,
            'typescript': self._typescript,
            'html':       self._html,
            'css':        self._css,
            'json':       self._json,
            'bash':       self._bash,
            'c':          self._c,
            'cpp':        self._cpp,
            'rust':       self._rust,
            'go':         self._go,
            'java':       self._java,
            'yaml':       self._yaml,
            'toml':       self._toml,
            'sql':        self._sql,
        }
        build = dispatch.get(lang)
        if build:
            build()

    def _python(self):
        kw = (r'\b(False|None|True|and|as|assert|async|await|break|class|continue'
              r'|def|del|elif|else|except|finally|for|from|global|if|import|in|is'
              r'|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b')
        builtins = (r'\b(print|len|range|type|int|str|list|dict|set|tuple|bool|float'
                    r'|open|super|self|cls|isinstance|hasattr|getattr|setattr'
                    r'|enumerate|zip|map|filter|sorted|reversed|sum|min|max|abs|round)\b')
        self._add(kw, 'keyword', bold=True)
        self._add(builtins, 'type')
        self._add(r'@[\w.]+', 'decorator')
        self._add(r'(?<=def )\w+', 'func')
        self._add(r'(?<=class )\w+', 'type', bold=True)
        self._add(r'"[^"\\]*(?:\\.[^"\\]*)*"', 'string')
        self._add(r"'[^'\\]*(?:\\.[^'\\]*)*'", 'string')
        self._add(r'#[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d*)?(j|[eE][+\-]?\d+)?\b', 'number')
        self._add_ml(r'"""', r'"""', 'string')
        self._add_ml(r"'''", r"'''", 'string')

    def _javascript(self):
        kw = (r'\b(break|case|catch|class|const|continue|debugger|default|delete|do'
              r'|else|export|extends|finally|for|from|function|if|import|in|instanceof'
              r'|let|new|of|return|static|super|switch|this|throw|try|typeof|var|void'
              r'|while|with|yield|async|await)\b')
        literals = r'\b(true|false|null|undefined|NaN|Infinity)\b'
        self._add(kw, 'keyword', bold=True)
        self._add(literals, 'keyword2')
        self._add(r'(?<=function )\w+', 'func')
        self._add(r'(?<=class )\w+', 'type', bold=True)
        self._add(r'`[^`\\]*(?:\\.[^`\\]*)*`', 'string')
        self._add(r'"[^"\\]*(?:\\.[^"\\]*)*"', 'string')
        self._add(r"'[^'\\]*(?:\\.[^'\\]*)*'", 'string')
        self._add(r'//[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d+)?\b', 'number')
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    def _typescript(self):
        self._javascript()
        types = (r'\b(string|number|boolean|any|void|never|object|symbol|bigint'
                 r'|unknown|interface|type|enum|namespace|abstract|implements'
                 r'|declare|readonly|keyof|infer|satisfies)\b')
        self._add(types, 'type')

    def _html(self):
        self._add(r'</?[\w]+', 'tag', bold=True)
        self._add(r'/?>', 'tag')
        self._add(r'\b[\w-]+(?=\s*=)', 'attr')
        self._add(r'"[^"]*"', 'string')
        self._add(r"'[^']*'", 'string')
        self._add(r'<![\w]+', 'keyword')
        self._add_ml(r'<!--', r'-->', 'comment', italic=True)

    def _css(self):
        self._add(r'@[\w-]+', 'keyword', bold=True)
        self._add(r'[.#::][\w-]+', 'func')
        self._add(r'[\w-]+(?=\s*:)', 'attr')
        self._add(r'#[0-9a-fA-F]{3,8}\b', 'number')
        self._add(r'\b\d+(\.\d+)?(px|em|rem|vh|vw|%|pt|s|ms|ch|ex|vmin|vmax)?\b', 'number')
        self._add(r'"[^"]*"', 'string')
        self._add(r"'[^']*'", 'string')
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    def _json(self):
        self._add(r'"[^"]*"(?=\s*:)', 'key', bold=True)
        self._add(r':\s*"[^"]*"', 'string')
        self._add(r'\b-?\d+(\.\d+)?([eE][+\-]?\d+)?\b', 'number')
        self._add(r'\b(true|false|null)\b', 'keyword2')

    def _bash(self):
        kw = (r'\b(if|then|else|elif|fi|for|while|do|done|case|esac|in|function'
              r'|return|local|export|readonly|declare|source|echo|exit|shift|set'
              r'|unset|trap|break|continue|select|until)\b')
        self._add(kw, 'keyword', bold=True)
        self._add(r'#[^\n]*', 'comment', italic=True)
        self._add(r'\$\{?[\w@#?*!\-]+\}?', 'var')
        self._add(r'"[^"]*"', 'string')
        self._add(r"'[^']*'", 'string')
        self._add(r'\b\d+\b', 'number')

    def _c_common(self):
        kw = (r'\b(auto|break|case|char|const|continue|default|do|double|else|enum'
              r'|extern|float|for|goto|if|inline|int|long|register|restrict|return'
              r'|short|signed|sizeof|static|struct|switch|typedef|union|unsigned'
              r'|void|volatile|while)\b')
        self._add(kw, 'keyword', bold=True)
        self._add(r'#\s*\w+', 'decorator')
        self._add(r'"[^"\\]*(?:\\.[^"\\]*)*"', 'string')
        self._add(r"'[^'\\]*(?:\\.[^'\\]*)*'", 'string')
        self._add(r'//[^\n]*', 'comment', italic=True)
        self._add(r'\b0x[0-9a-fA-F]+\b', 'number')
        self._add(r'\b\d+(\.\d+)?[uUlLfF]*\b', 'number')
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    def _c(self):
        self._c_common()

    def _cpp(self):
        cpp_kw = (r'\b(alignas|alignof|and|and_eq|bitand|bitor|bool|catch|class'
                  r'|compl|concept|consteval|constexpr|constinit|co_await|co_return'
                  r'|co_yield|decltype|delete|explicit|export|false|final|friend'
                  r'|mutable|namespace|new|noexcept|not|not_eq|nullptr|operator|or'
                  r'|or_eq|override|private|protected|public|requires|static_assert'
                  r'|static_cast|dynamic_cast|reinterpret_cast|const_cast|template'
                  r'|this|thread_local|throw|true|try|typename|using|virtual|xor|xor_eq)\b')
        self._add(cpp_kw, 'keyword2', bold=True)
        self._c_common()

    def _rust(self):
        kw = (r'\b(as|async|await|break|const|continue|crate|dyn|else|enum|extern'
              r'|false|fn|for|if|impl|in|let|loop|match|mod|move|mut|pub|ref|return'
              r'|self|Self|static|struct|super|trait|true|type|unsafe|use|where|while)\b')
        types = (r'\b(i8|i16|i32|i64|i128|isize|u8|u16|u32|u64|u128|usize|f32|f64'
                 r'|bool|char|str|String|Vec|Option|Result|Box|Rc|Arc|HashMap|HashSet)\b')
        self._add(kw, 'keyword', bold=True)
        self._add(types, 'type')
        self._add(r'#!\[[\w::()\s,]+\]', 'decorator')
        self._add(r'#\[[\w::()\s,]+\]', 'decorator')
        self._add(r'"[^"\\]*(?:\\.[^"\\]*)*"', 'string')
        self._add(r"'[^'\\]*(?:\\.[^'\\]*)*'", 'string')
        self._add(r'//[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d+)?(_\d+)?(u\d+|i\d+|f\d+)?\b', 'number')
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    def _go(self):
        kw = (r'\b(break|case|chan|const|continue|default|defer|else|fallthrough'
              r'|for|func|go|goto|if|import|interface|map|package|range|return'
              r'|select|struct|switch|type|var)\b')
        builtins = (r'\b(bool|byte|complex64|complex128|error|float32|float64|int'
                    r'|int8|int16|int32|int64|rune|string|uint|uint8|uint16|uint32'
                    r'|uint64|uintptr|true|false|nil|iota|make|new|len|cap|append'
                    r'|copy|delete|close|panic|recover|print|println)\b')
        self._add(kw, 'keyword', bold=True)
        self._add(builtins, 'type')
        self._add(r'`[^`]*`', 'string')
        self._add(r'"[^"\\]*(?:\\.[^"\\]*)*"', 'string')
        self._add(r"'[^'\\]*(?:\\.[^'\\]*)*'", 'string')
        self._add(r'//[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d+)?\b', 'number')
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    def _java(self):
        kw = (r'\b(abstract|assert|break|case|catch|class|const|continue|default'
              r'|do|else|enum|extends|final|finally|for|goto|if|implements|import'
              r'|instanceof|interface|native|new|package|private|protected|public'
              r'|return|static|strictfp|super|switch|synchronized|this|throw|throws'
              r'|transient|try|var|volatile|while)\b')
        literals = r'\b(true|false|null)\b'
        types = (r'\b(boolean|byte|char|double|float|int|long|short|void|String'
                 r'|Integer|Long|Double|Boolean|Object|Class|System|List|Map|Set'
                 r'|ArrayList|HashMap|Optional)\b')
        self._add(kw, 'keyword', bold=True)
        self._add(literals, 'keyword2')
        self._add(types, 'type')
        self._add(r'@\w+', 'decorator')
        self._add(r'"[^"\\]*(?:\\.[^"\\]*)*"', 'string')
        self._add(r"'[^'\\]*(?:\\.[^'\\]*)*'", 'string')
        self._add(r'//[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d+)?[lLfFdD]?\b', 'number')
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    def _yaml(self):
        self._add(r'^[\s-]*[\w-]+(?=\s*:)', 'key', bold=True)
        self._add(r':\s*[^#\n]+', 'string')
        self._add(r'#[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d+)?\b', 'number')
        self._add(r'\b(true|false|null|yes|no|on|off)\b', 'keyword2')

    def _toml(self):
        self._add(r'^\[[\w.]+\]', 'type', bold=True)
        self._add(r'^[\w.-]+(?=\s*=)', 'key', bold=True)
        self._add(r'=\s*"[^"]*"', 'string')
        self._add(r"=\s*'[^']*'", 'string')
        self._add(r'#[^\n]*', 'comment', italic=True)
        self._add(r'\b\d+(\.\d+)?\b', 'number')
        self._add(r'\b(true|false)\b', 'keyword2')

    def _sql(self):
        kw = (r'\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|FULL|ON|AND|OR'
              r'|NOT|IN|IS|NULL|AS|DISTINCT|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|INSERT'
              r'|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|INDEX|VIEW|DROP|ALTER|ADD'
              r'|COLUMN|PRIMARY|KEY|FOREIGN|REFERENCES|UNIQUE|DEFAULT|CHECK|CONSTRAINT'
              r'|BEGIN|COMMIT|ROLLBACK|TRANSACTION|GRANT|REVOKE|UNION|ALL|EXISTS|CASE'
              r'|WHEN|THEN|ELSE|END|LIKE|BETWEEN|WITH|RECURSIVE|RETURNING)\b')
        types = (r'\b(INT|INTEGER|BIGINT|SMALLINT|TINYINT|FLOAT|DOUBLE|DECIMAL|NUMERIC'
                 r'|CHAR|VARCHAR|TEXT|BLOB|DATE|TIME|DATETIME|TIMESTAMP|BOOLEAN|BOOL'
                 r'|SERIAL|AUTOINCREMENT|UUID|JSON|JSONB)\b')
        self._add(kw, 'keyword', bold=True, flags=re.IGNORECASE)
        self._add(types, 'type', flags=re.IGNORECASE)
        self._add(r"'[^']*'", 'string')
        self._add(r'\b\d+(\.\d+)?\b', 'number')
        self._add(r'--[^\n]*', 'comment', italic=True)
        self._add_ml(r'/\*', r'\*/', 'comment', italic=True)

    # ------------------------------------------------------------------
    # Core highlighting logic
    # ------------------------------------------------------------------

    def highlightBlock(self, text: str):
        self.setCurrentBlockState(0)

        # --- Continue a multiline construct from the previous block ---
        offset = 0
        prev_state = self.previousBlockState()
        if prev_state > 0:
            idx = prev_state - 1
            if idx < len(self._ml_rules):
                _, end_pat, fmt = self._ml_rules[idx]
                m = end_pat.search(text)
                if m:
                    self.setFormat(0, m.end(), fmt)
                    offset = m.end()
                else:
                    self.setFormat(0, len(text), fmt)
                    self.setCurrentBlockState(prev_state)
                    return

        # --- Single-line rules on the remaining text ---
        for pat, fmt in self._rules:
            for m in pat.finditer(text, offset):
                self.setFormat(m.start(), m.end() - m.start(), fmt)

        # --- Check whether a multiline construct starts in remaining text ---
        for i, (start_pat, end_pat, fmt) in enumerate(self._ml_rules):
            m_s = start_pat.search(text, offset)
            if not m_s:
                continue
            m_e = end_pat.search(text, m_s.end())
            if m_e:
                # Starts and ends on this line
                self.setFormat(m_s.start(), m_e.end() - m_s.start(), fmt)
            else:
                # Spans into next block(s)
                self.setFormat(m_s.start(), len(text) - m_s.start(), fmt)
                self.setCurrentBlockState(i + 1)
            break  # handle only the first multiline found per line
