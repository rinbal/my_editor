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
import html
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal
from PySide6.QtGui import QTextBlockFormat, QTextCharFormat, QTextListFormat

import atomic_file
import photo_page
import photo_group
import photo_layout
from doc_walk import (
    bullet_depth,
    iter_block_runs,
    iter_blocks,
    parse_bullet_line,
    skip_prefix,
)
from image_safety import MAX_PICTURE_BYTES, data_uri_bytes, declared_size
from markdown_writer import link_markdown
import rich_text
import rmd_toolchain
from i18n import _

# Shift+Enter line separator and the inline-object placeholder.
_LINE_SEP = "\u2028"
_OBJ = "\ufffc"

# Characters that would change meaning mid-line in Pandoc markdown (as
# rmarkdown reads it: "$" opens TeX math, "@" a citation, "^" and "~"
# a superscript and a subscript).
_MD_SPECIALS = "\\`*_[]<>$@^~"

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


# A photo group in R Markdown: a fenced div of this class holding one
# paragraph, a line for each picture and each caption (_group_rmd).
GROUP_CLASS = "photo-group"
_UNAVAILABLE_MD = "*[image unavailable]*"


def _group_rmd(group, copy_image) -> list[str]:
    """A photo group as R Markdown: a fenced div (pandoc's) around one
    paragraph, its pictures and captions on lines joined by hard breaks.
    A knitted PDF or Word document shows them one under another, as other
    apps do (one paragraph: no figure floats away, no description turns
    into a caption); a page knitted here lays them out as the HTML export
    does (lay_out_knitted_groups). A wide picture carries ``{.wide}``."""
    lines: list[str] = []
    for unit in group.units:
        target = copy_image(photo_group.without_wide(unit.name)) if copy_image else None
        if isinstance(target, str) and target:
            lines.append(f"![{_escape_md(unit.alt)}]({target})" + ("{.wide}" if unit.wide else ""))
        else:
            lines.append(_UNAVAILABLE_MD)
        if not unit.has_caption or not unit.caption_text.strip():
            continue
        text = _render_runs_md(unit.caption_runs(), copy_image).strip()
        if text:
            lines.extend(_guard_line_start(line) for line in text.split("\\\n"))
    return [f"::: {{.{GROUP_CLASS}}}", "\\\n".join(lines), ":::"]


def document_to_rmd(doc, title: str, copy_image=None) -> str:
    """Convert a rich QTextDocument to an R Markdown source string, a
    photo group as its pictures in a div of their own (_group_rmd)."""
    groups = photo_group.groups_in(doc)
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
    next_group, written = 0, -1
    for block in iter_blocks(doc):
        # A block of a group's tables (or one Qt keeps around them): the
        # group is written where its first one is, the rest skipped.
        position = block.position()
        while next_group < len(groups) and position > groups[next_group].end:
            next_group += 1
        if next_group < len(groups) and groups[next_group].start <= position \
                and position + block.length() - 1 <= groups[next_group].end:
            group = groups[next_group]
            if written != next_group:
                written = next_group
                if prev_kind not in (None, "blank"):
                    lines.append("")
                lines.extend(_group_rmd(group, copy_image))
                lines.append("")
                prev_kind = "blank"
            continue
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


# --------------------------------------------------------------------------- #
# Photo groups on a knitted page                                              #
# --------------------------------------------------------------------------- #
#
# pandoc writes a group's fenced div (_group_rmd) as that div around one
# paragraph, the pictures and captions in it on lines joined by <br />,
# each picture an <img> its own line (self_contained: its bytes a data:
# URI). That is what is read here; a group in any other shape stays as
# pandoc wrote it, its pictures one under another.

_GROUP_PARAGRAPH = re.compile(
    r'<div class="' + GROUP_CLASS + r'">\s*<p>(.*?)</p>\s*</div>', re.S)
_LINE_BREAK = re.compile(r"<br\s*/?>")
_PICTURE_LINE = re.compile(r"(<img\s[^>]*>)", re.S)
_UNAVAILABLE_HTML = "<em>[image unavailable]</em>"
_TAG = re.compile(r"<[^>]*>")
# A caption is shown as the export shows it: in its words, not in italic.
_EMPHASIS = re.compile(r"</?(?:em|i)(?:\s[^>]*)?>")


class _ImgAttributes(HTMLParser):
    def __init__(self, tag: str):
        super().__init__(convert_charrefs=True)
        self.attributes: dict = {}
        self.feed(tag)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "img" and not self.attributes:
            self.attributes = {name: value or "" for name, value in attrs}

    handle_startendtag = handle_starttag


def _knitted_figure(tag: str):
    attributes = _ImgAttributes(tag).attributes
    src = attributes.get("src") or None
    size = None
    data = data_uri_bytes(src) if src else None
    if data is not None and len(data) <= MAX_PICTURE_BYTES:
        found = declared_size(data)
        if found is not None:
            size = photo_layout.picture_size(found.width(), found.height())
    # pandoc may break a long line where a description has a space.
    alt = " ".join(attributes.get("alt", "").split())
    return photo_page.GroupFigure(src, size, None, alt,
                                   wide="wide" in attributes.get("class", "").split())


def _knitted_figures(paragraph: str):
    """The figures of a group's paragraph, or None when it is not one."""
    figures: list = []
    captions: list[str] = []

    def close_caption():
        if figures and captions:
            # A line pandoc wrapped is one line of words again.
            words = " ".join(_EMPHASIS.sub("", "<br>".join(captions)).split())
            figures[-1].caption_html = words
            figures[-1].caption_text = html.unescape(_TAG.sub("", words)).strip()
        captions.clear()

    for line in _LINE_BREAK.split(paragraph):
        line = line.strip()
        if not line:
            continue
        picture = _PICTURE_LINE.fullmatch(line)
        if picture is not None or line == _UNAVAILABLE_HTML:
            close_caption()
            figures.append(_knitted_figure(picture.group(1)) if picture is not None
                           else photo_page.GroupFigure(None, None, None, ""))
        elif figures and "<img" not in line:
            captions.append(line)
        else:
            return None          # words before the first picture, or two on a line
    close_caption()
    return figures if len(figures) >= 2 else None


def lay_out_knitted_groups(page: str) -> str:
    """``page``, knitted from R Markdown MyEditor wrote, with each photo
    group as the HTML export writes it (photo_page.group_figures_html),
    and the groups' stylesheet and scripts in it once. In the page's own
    light colours: a knitted page does not change with Dark Mode."""
    laid = [False]

    def group(match):
        figures = _knitted_figures(match.group(1))
        if figures is None:
            return match.group(0)
        laid[0] = True
        return photo_page.group_figures_html(figures)

    out = _GROUP_PARAGRAPH.sub(group, page)
    body = out.find("<body")
    head_end = out.rfind("</head>", 0, body) if body >= 0 else -1
    body_end = out.rfind("</body>")
    if not laid[0] or head_end < 0 or body_end < body:
        return page
    style = f"<style>{photo_page.group_head(dark=False)}</style>\n"
    out = out[:body_end] + photo_page.group_scripts() + out[body_end:]
    return out[:head_end] + style + out[head_end:]


def lay_out_groups_in(path: str) -> None:
    """lay_out_knitted_groups for the page at ``path``, in place. A page
    that cannot be read or written stays as pandoc wrote it (its pictures
    one under another), and nothing is said."""
    try:
        with open(path, encoding="utf-8") as f:
            page = f.read()
    except (OSError, UnicodeDecodeError):
        return
    laid = lay_out_knitted_groups(page)
    if laid != page:
        try:
            atomic_file.write_text(path, laid)
        except OSError:
            pass


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
                if self._fmt == "html":
                    lay_out_groups_in(out_path)
                self.finished.emit(out_path)
            else:
                self.failed.emit("render-error",
                                 _("Render reported success but no output file "
                                   "was found.") + "\n\n" + detail)
        else:
            self.failed.emit(classify_failure(self._output), detail)
