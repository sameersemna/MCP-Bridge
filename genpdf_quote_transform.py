"""Rewrite markdown blockquotes as raw-HTML report callouts for pandoc.

``genpdf.sh`` pipes the response markdown through this transform and then
through pandoc (``--from markdown+raw_html``). Blockquotes (the citation-guard
``[!WARNING]`` block, video transcripts, etc.) are rewritten as
``<blockquote class="report-quote">`` containers so ``styles.css`` can style
them and so RTL Arabic quotes get correct direction.

The blockquote body is emitted as **raw HTML** -- one ``<p>`` per text line and
one flat ``<ul>``/``<ol>`` per run of list lines -- and inline markdown
(``code``, ``**bold**``, ``_(italic)_``) is pre-rendered to HTML. This matters
because pandoc's default ``markdown_in_html_blocks`` extension (still active
under ``markdown+raw_html``) re-parses markdown left *inside* raw HTML blocks.
Emitting ``- item`` lines inside the raw ``<p>`` wrappers therefore made pandoc
nest each list one level deeper than the previous one, producing a deeply
nested ``<ul>`` chain in the HTML and PDF.
"""

import html
import re
import sys
from pathlib import Path

RTL_CHAR_PATTERN = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFD\uFE70-\uFEFC]")

# A blockquote line that is a list item: ``- text``, ``* text``, ``+ text``
# (unordered) or ``1. text`` / ``1) text`` (ordered). The marker is dropped and
# the remainder becomes the ``<li>`` content.
_UNORDERED_ITEM_RE = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_ITEM_RE = re.compile(r"^\d+[.)]\s+(.*)$")

# Inline markdown the bridge emits inside blockquotes. It is rendered to HTML
# here rather than left for pandoc: the transform emits *raw HTML* blockquote
# bodies, and pandoc is invoked with ``markdown+raw_html`` (which keeps the
# default ``markdown_in_html_blocks`` extension), so any markdown left inside
# those blocks -- including ``- `` list markers -- is re-parsed as markdown and
# nests on every line. Pre-rendering the inline markup keeps the HTML flat.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Matches the ``_(not retrieved)_`` style emphasis tag emitted by the citation
# guard, without matching underscores inside URLs (it requires a literal ``_(``).
_PAREN_ITALIC_RE = re.compile(r"(?<![\w_])_\(([^)]*)\)_(?![\w_])")


def _is_rtl_text(text: str) -> bool:
    return bool(RTL_CHAR_PATTERN.search(text))


def _emphasis_html(text: str) -> str:
    """Convert ``**bold**`` and ``_(italic)_`` emphasis to HTML."""
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    return _PAREN_ITALIC_RE.sub(lambda m: f"<em>({m.group(1)})</em>", text)


def _inline_html(text: str) -> str:
    """Escape ``text`` and convert its inline markdown to HTML.

    ``<p>``/``<li>`` content must be valid HTML (and markdown-free) so pandoc's
    raw-HTML handling cannot re-interpret it. Code spans are tokenized first so
    emphasis rules can never rewrite the inside of a ``<code>`` element.
    """
    escaped = html.escape(text, quote=False)
    parts: list[str] = []
    pos = 0
    for match in _CODE_SPAN_RE.finditer(escaped):
        parts.append(_emphasis_html(escaped[pos : match.start()]))
        parts.append(f"<code>{match.group(1)}</code>")
        pos = match.end()
    parts.append(_emphasis_html(escaped[pos:]))
    return "".join(parts)


def _render_blockquote_body(paragraphs: list[str], dir_attr: str) -> str:
    """Render blockquote lines as raw HTML, preserving list structure.

    Text lines become ``<p>`` blocks; consecutive list lines are grouped into a
    single (flat) ``<ul>``/``<ol>`` so a bulleted warning renders as one list
    rather than a chain of nested lists.
    """
    out: list[str] = []
    current_list: str | None = None

    def close_list() -> None:
        nonlocal current_list
        if current_list is not None:
            out.append(f"</{current_list}>")
            current_list = None

    for line in paragraphs:
        unordered = _UNORDERED_ITEM_RE.match(line)
        ordered = _ORDERED_ITEM_RE.match(line)
        if unordered:
            tag, item = "ul", unordered.group(1)
        elif ordered:
            tag, item = "ol", ordered.group(1)
        else:
            tag, item = None, None

        if tag is None:
            close_list()
            out.append(f'<p {dir_attr}>{_inline_html(line)}</p>')
            continue

        if current_list != tag:
            close_list()
            out.append(f"<{tag}>")
            current_list = tag
        out.append(f"<li>{_inline_html(item)}</li>")

    close_list()
    return "\n".join(out)


def transform_markdown_quotes(src_path: Path | str, out_path: Path | str) -> None:
    src = Path(src_path)
    out = Path(out_path)

    lines = src.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">"):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(lines[i][1:].lstrip(" "))
                i += 1
            paragraphs = [part.strip() for part in quote_lines if part.strip()]
            if paragraphs:
                if any(_is_rtl_text(paragraph) for paragraph in paragraphs):
                    wrapped = _render_blockquote_body(paragraphs, 'dir="rtl" lang="ar"')
                    out_lines.append('<blockquote class="report-quote" dir="rtl" lang="ar">')
                else:
                    wrapped = _render_blockquote_body(paragraphs, 'dir="ltr" lang="en"')
                    out_lines.append('<blockquote class="report-quote" dir="ltr" lang="en">')
                out_lines.append('<div class="report-quote-body">')
                out_lines.append(wrapped)
                out_lines.append("</div>")
                out_lines.append("</blockquote>")
                continue
        out_lines.append(line)
        i += 1

    out.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: genpdf_quote_transform.py <input.md> <output.md>")
    transform_markdown_quotes(sys.argv[1], sys.argv[2])
