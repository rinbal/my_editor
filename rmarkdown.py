#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R Markdown support: export, and knitting through the R toolchain.

Export model (mirrors how RStudio treats documents):
- A tab opened from an .Rmd file is SOURCE and saves as plain text.
- A rich-text tab saved as .Rmd is converted: YAML frontmatter plus a
  Pandoc-markdown body. Bold, italic, strikethrough, inline code, links,
  headings and lists (bulleted, numbered, checklists) map natively;
  underline and colors use Pandoc bracketed spans ([x]{.underline},
  [x]{style="color:#hex"}) which rmarkdown renders in HTML output.
- Images are NOT embedded as data URIs (unreadable to edit, and they
  break LaTeX PDF knitting). Instead the caller provides copy_image()
  which copies each image into a sidecar media folder next to the .Rmd
  and returns the relative reference. Knitted html_document output is
  self-contained, so the rendered artifact still travels as one file.
  For audio and video it returns a MediaLink instead: the file is a link
  of its own paragraph, "[name](address)", not a picture.

Knitting runs `Rscript -e 'rmarkdown::render(...)'` in a QProcess with
the environment composed by rmd_toolchain (managed R, pandoc, private
package library). The file path travels via commandArgs so no shell
quoting can corrupt it.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal
from PySide6.QtGui import QTextBlockFormat, QTextCharFormat, QTextListFormat

import photo_group
from doc_walk import (
    bullet_depth,
    iter_block_runs,
    iter_blocks,
    parse_bullet_line,
    skip_prefix,
)
from markdown_writer import link_markdown
import rich_text
import rmd_toolchain
from i18n import _

# Shift+Enter line separator and the inline-object placeholder.
_LINE_SEP = "\u2028"
_OBJ = "\ufffc"

# Characters that would change meaning mid-line in Pandoc markdown.
_MD_SPECIALS = "\\`*_[]<>"

# Line leaders that would turn a plain line into markdown structure.
_MD_LINE_LEADERS = re.compile(r"^(?:[#>+-]|\d+[.)])")


def _escape_md(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _guard_line_start(line: str) -> str:
    """Backslash-escape leaders like '#', '-', '1.' at the start of a line."""
    stripped = line.lstrip()
    m = _MD_LINE_LEADERS.match(stripped)
    if m:
        pad = line[:len(line) - len(stripped)]
        return pad + "\\" + stripped
    return line


def derive_title(doc, fallback: str) -> str:
    """First non-blank line, stripped of a leading '# ', else the fallback."""
    for block in iter_blocks(doc):
        text = block.text().strip()
        if text:
            return text.lstrip("#").strip() or fallback
    return fallback


def _render_text_run_md(text: str, fmt, heading: bool = False) -> str:
    if fmt.fontFixedPitch():
        ticks = "``" if "`" in text else "`"
        return f"{ticks}{text.replace(_LINE_SEP, ' ')}{ticks}"
    out = _escape_md(text).replace(_LINE_SEP, "\\\n")
    bold = fmt.fontWeight() > 400 and not heading
    strike = fmt.fontStrikeOut()
    italic = fmt.fontItalic()
    underline = fmt.fontUnderline()
    # A link's own color is how links look, not a color of the text.
    colored = fmt.hasProperty(QTextCharFormat.ForegroundBrush) and not fmt.isAnchor()
    underline = underline and not fmt.isAnchor()

    # Emphasis markers must hug non-space text; move edge whitespace out.
    lead = out[:len(out) - len(out.lstrip())]
    trail = out[len(out.rstrip()):]
    core = out.strip()
    if not core:
        return out
    if strike:
        core = f"~~{core}~~"
    if bold:
        core = f"**{core}**"
    if italic:
        core = f"*{core}*"
    attrs = []
    if underline:
        attrs.append(".underline")
    if colored:
        color = fmt.foreground().color().name()
        attrs.append(f'style="color:{color}"')
    if attrs:
        core = f"[{core}]{{{' '.join(attrs)}}}"
    return lead + core + trail


@dataclass(frozen=True)
class MediaLink:
    """What copy_image answers for an audio or video file: a link to it,
    named ``label``, at ``url`` (its address, or its copy beside the .Rmd)."""

    label: str
    url: str

    def markdown(self) -> str:
        return link_markdown(self.label, self.url)


def _render_runs_md(runs, copy_image, heading: bool = False) -> str:
    parts = []
    link_parts: list[str] = []
    link = ""

    def close_link():
        if link:
            parts.append("[" + "".join(link_parts) + "](" + link + ")")
            link_parts.clear()

    for text, fmt in runs:
        href = fmt.anchorHref() if fmt.isAnchor() and not fmt.isImageFormat() else ""
        if href != link:
            close_link()
            link = href
        if href:
            text = text.replace(_OBJ, "")
            if text:
                link_parts.append(_render_text_run_md(text, fmt))
            continue
        if fmt.isImageFormat():
            target = copy_image(fmt.toImageFormat().name()) if copy_image else None
            if isinstance(target, MediaLink):
                parts.append(target.markdown())
            elif target:
                parts.append(f"![]({target})")
            else:
                parts.append("*[image unavailable]*")
            continue
        text = text.replace(_OBJ, "")
        if text:
            parts.append(_render_text_run_md(text, fmt, heading))
    close_link()
    return "".join(parts)


_ORDERED_STYLES = {QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha,
                   QTextListFormat.Style.ListUpperAlpha, QTextListFormat.Style.ListLowerRoman,
                   QTextListFormat.Style.ListUpperRoman}


def _list_item_md(block, copy_image) -> str:
    """A real list item as Pandoc Markdown: nested by four spaces, which
    lines up under both "- " and "1. "."""
    text_list = block.textList()
    fmt = text_list.format()
    depth = max(1, fmt.indent())
    if fmt.style() in _ORDERED_STYLES:
        start = fmt.start() if hasattr(fmt, "start") else 1
        marker = f"{max(1, start) + text_list.itemNumber(block)}."
    else:
        marker = "-"
    check = block.blockFormat().marker()
    if check == QTextBlockFormat.MarkerType.Checked:
        marker += " [x]"
    elif check == QTextBlockFormat.MarkerType.Unchecked:
        marker += " [ ]"
    content = _render_runs_md(list(iter_block_runs(block)), copy_image)
    return "    " * (depth - 1) + marker + " " + content


def document_to_rmd(doc, title: str, copy_image=None) -> str:
    """Convert a rich QTextDocument to an R Markdown source string. A
    photo group is written as the paragraphs it was read from: a knitted
    page shows its pictures one under another."""
    doc = photo_group.unfolded(doc)
    header = "\n".join([
        "---",
        f"title: {json.dumps(title)}",
        f'date: "{datetime.date.today().isoformat()}"',
        "output:",
        "  html_document:",
        "    self_contained: true",
        "---",
    ])

    lines: list[str] = []
    prev_kind = None  # None | "text" | "bullet" | "blank" | "quote"
    prev_quote = 0
    for block in iter_blocks(doc):
        text = block.text()
        spaces, has_bullet = parse_bullet_line(text)

        if rich_text.is_divider(block):
            if prev_kind not in (None, "blank"):
                lines.append("")
            # Not "---": at the top it would read as the YAML header.
            lines.append("***")
            lines.append("")
            prev_kind = "blank"
            continue

        quote = rich_text.quote_depth(block)
        if quote and text.strip():
            prefix = "> " * quote
            if prev_kind == "quote":
                # A new paragraph in the quote, at the shallower depth.
                lines.append(("> " * min(prev_quote, quote)).rstrip())
            elif prev_kind not in (None, "blank"):
                lines.append("")
            if block.textList() is not None:
                lines.append(prefix + _list_item_md(block, copy_image))
            else:
                lines.append(prefix + _guard_line_start(
                    _render_runs_md(list(iter_block_runs(block)), copy_image)))
            prev_kind = "quote"
            prev_quote = quote
            continue

        if block.textList() is not None:
            if prev_kind in ("text", "quote"):
                lines.append("")  # a list needs a blank line before it
            lines.append(_list_item_md(block, copy_image))
            prev_kind = "bullet"
            continue

        heading = block.blockFormat().headingLevel()
        if heading and text.strip():
            if prev_kind is not None and prev_kind != "blank":
                lines.append("")
            content = _render_runs_md(list(iter_block_runs(block)), copy_image, True)
            lines.append("#" * min(6, heading) + " " + content.strip())
            lines.append("")
            prev_kind = "blank"
            continue

        if has_bullet:
            depth = bullet_depth(spaces)
            runs = skip_prefix(list(iter_block_runs(block)), spaces + 2)
            content = _render_runs_md(runs, copy_image)
            if prev_kind in ("text", "quote"):
                lines.append("")  # a list needs a blank line before it
            lines.append("  " * (depth - 1) + "- " + content)
            prev_kind = "bullet"
            continue

        if not text.strip():
            if prev_kind != "blank" and prev_kind is not None:
                lines.append("")
            prev_kind = "blank"
            continue

        media = _media_link(block, copy_image)
        if media is not None:
            # Audio or video: a link in a paragraph of its own.
            if prev_kind not in (None, "blank"):
                lines.append("")
            lines.append(media.markdown())
            lines.append("")
            prev_kind = "blank"
            continue

        content = _guard_line_start(
            _render_runs_md(list(iter_block_runs(block)), copy_image))
        if prev_kind == "text":
            # Keep the editor's line structure: hard break, not a merged
            # paragraph and not a paragraph gap.
            lines[-1] += "\\"
        elif prev_kind in ("bullet", "quote"):
            # A blank line ends the list or quote (else the line joins it).
            lines.append("")
        lines.append(content)
        prev_kind = "text"

    body = "\n".join(lines).strip("\n")
    return f"{header}\n\n{body}\n"


def _media_link(block, copy_image):
    """The MediaLink of a paragraph that is one audio or video file, else None."""
    if copy_image is None or block.text() != _OBJ:
        return None
    fmt = block.begin().fragment().charFormat()
    if not fmt.isImageFormat():
        return None
    target = copy_image(fmt.toImageFormat().name())
    return target if isinstance(target, MediaLink) else None


def media_dir_for(rmd_path: str) -> str:
    """Sidecar folder holding images referenced by the .Rmd."""
    base = os.path.splitext(os.path.basename(rmd_path))[0]
    return os.path.join(os.path.dirname(rmd_path), f"{base}_media")


def find_rscript() -> str | None:
    """Single source of truth lives in rmd_toolchain."""
    return rmd_toolchain.find_rscript()


# --------------------------------------------------------------------------- #
# Knitting                                                                    #
# --------------------------------------------------------------------------- #

_RENDER_EXPR = (
    "args <- commandArgs(trailingOnly=TRUE); "
    "rmarkdown::render(args[1], output_format=args[2])"
)

_OUTPUT_RE = re.compile(r"Output created:\s*(.+)")

OUTPUT_FORMATS = {
    "html": "html_document",
    "pdf": "pdf_document",
}


def classify_failure(output: str) -> str:
    """Map render output to an actionable failure kind."""
    low = output.lower()
    if "there is no package called" in low and "rmarkdown" in low:
        return "missing-rmarkdown"
    if "pandoc" in low and ("not found" in low or "is required" in low
                            or "install pandoc" in low):
        return "missing-pandoc"
    if ("no latex installation" in low or "tinytex" in low
            or "pdflatex" in low or "xelatex" in low
            or "latex failed" in low):
        return "missing-latex"
    return "render-error"


def parse_output_path(output: str, rmd_path: str, fmt: str) -> str | None:
    """Locate the rendered file: 'Output created:' line, else extension swap."""
    matches = _OUTPUT_RE.findall(output)
    workdir = os.path.dirname(os.path.abspath(rmd_path))
    if matches:
        candidate = matches[-1].strip()
        if not os.path.isabs(candidate):
            candidate = os.path.join(workdir, candidate)
        if os.path.exists(candidate):
            return candidate
    ext = ".html" if fmt == "html" else ".pdf"
    guess = os.path.splitext(os.path.abspath(rmd_path))[0] + ext
    if os.path.exists(guess):
        return guess
    return None


class KnitRunner(QObject):
    """Runs one rmarkdown::render at a time, without blocking the UI.

    Signals:
      started()                  the process launched
      finished(str output_path)  render succeeded
      failed(str kind, str detail) render failed; kind is one of
          missing-r / missing-rmarkdown / missing-pandoc / missing-latex /
          render-error, detail is the output tail for display.
    """

    started = Signal()
    finished = Signal(str)
    failed = Signal(str, str)

    _DETAIL_CHARS = 2500

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._output = ""
        self._rmd_path = ""
        self._fmt = "html"

    @property
    def busy(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def knit(self, rmd_path: str, fmt: str = "html") -> None:
        if self.busy:
            return
        rscript = find_rscript()
        if not rscript:
            self.failed.emit("missing-r", _("No R installation was found."))
            return

        self._rmd_path = rmd_path
        self._fmt = fmt
        self._output = ""

        proc = QProcess(self)
        proc.setWorkingDirectory(os.path.dirname(os.path.abspath(rmd_path)))
        proc.setProcessChannelMode(QProcess.MergedChannels)

        # R's environment as the toolchain gives it: the person's own, with
        # pandoc and the app's R library. A new QProcess's own environment
        # is empty, and what is added to it is all a program then gets (Qt
        # adds SystemRoot on Windows, and PATH when there is none): Rscript
        # ran without HOME, TEMP or the system's PATH.
        env = QProcessEnvironment()
        for name, value in rmd_toolchain.knit_environment().items():
            env.insert(name, value)
        proc.setProcessEnvironment(env)

        proc.readyReadStandardOutput.connect(self._on_output)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        self._proc = proc
        proc.start(rscript, ["-e", _RENDER_EXPR, "--args",
                             os.path.abspath(rmd_path), OUTPUT_FORMATS[fmt]])
        self.started.emit()

    def kill(self) -> None:
        if self.busy:
            self._proc.kill()

    # ------------------------------------------------------------------ #

    def _on_output(self):
        if self._proc is not None:
            self._output += bytes(self._proc.readAllStandardOutput()).decode(
                "utf-8", errors="replace")

    def _on_error(self, error):
        if error == QProcess.FailedToStart:
            self.failed.emit("missing-r",
                             _("Rscript could not be started. It may have been "
                               "removed or is not executable."))
            self._proc = None

    def _on_finished(self, exit_code, _status):
        self._on_output()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        detail = self._output[-self._DETAIL_CHARS:]
        if exit_code == 0:
            out_path = parse_output_path(self._output, self._rmd_path, self._fmt)
            if out_path:
                self.finished.emit(out_path)
            else:
                self.failed.emit("render-error",
                                 _("Render reported success but no output file "
                                   "was found.") + "\n\n" + detail)
        else:
            self.failed.emit(classify_failure(self._output), detail)
