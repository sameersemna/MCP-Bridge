import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genpdf_quote_transform import transform_markdown_quotes


def test_transform_markdown_quotes_wraps_blockquotes_in_rtl_container(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    output_path = tmp_path / "output.md"
    input_path.write_text(
        "Intro\n\n> مرحبا بالعالم\n> English translation\n\nTail\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, output_path)

    rendered = output_path.read_text(encoding="utf-8")
    assert '<blockquote class="report-quote" dir="rtl" lang="ar">' in rendered
    assert '<div class="report-quote-body">' in rendered
    assert '<p dir="rtl" lang="ar">' in rendered
    assert 'Intro' in rendered
    assert 'Tail' in rendered


def test_transform_markdown_quotes_leaves_ltr_blockquotes_as_ltr(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    output_path = tmp_path / "output.md"
    input_path.write_text(
        "Intro\n\n> This is an English quote.\n\nTail\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, output_path)

    rendered = output_path.read_text(encoding="utf-8")
    assert '<blockquote class="report-quote"' in rendered
    assert 'dir="rtl"' not in rendered
    assert '<p dir="ltr" lang="en">' in rendered


def test_transform_blockquote_bullets_render_as_one_flat_list(tmp_path: Path) -> None:
    """A bulleted warning must be a single flat <ul>, not nested <ul>s.

    Regression: every blockquote line used to be wrapped in its own raw
    ``<p>`` while still carrying a ``- `` markdown marker. Pandoc
    (``markdown+raw_html`` keeps ``markdown_in_html_blocks``) re-parsed those
    markers as markdown *inside* the raw blocks, nesting each list one level
    deeper than the previous one.
    """
    input_path = tmp_path / "input.md"
    output_path = tmp_path / "output.md"
    input_path.write_text(
        "Intro\n\n"
        "> [!WARNING]\n"
        "> **Unverified citations detected.** 3 links follow:\n"
        ">\n"
        "> - `https://a.example/x` _(not retrieved)_\n"
        "> - `https://b.example/y` _(not retrieved)_\n"
        "> - `https://c.example/z` _(not retrieved)_\n"
        ">\n"
        "> Treat these as unverified.\n\n"
        "Tail\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, output_path)
    rendered = output_path.read_text(encoding="utf-8")

    assert rendered.count("<ul>") == 1
    assert rendered.count("</ul>") == 1
    assert rendered.count("<li>") == 3
    assert rendered.count("</li>") == 3
    # Inline markdown is pre-rendered so pandoc cannot re-parse it as markdown.
    assert "<code>https://a.example/x</code>" in rendered
    assert "<em>(not retrieved)</em>" in rendered
    assert "<strong>Unverified citations detected.</strong>" in rendered
    # No markdown bullet markers survive inside the raw HTML body.
    assert "lang=\"en\">- " not in rendered
    assert "- `https://" not in rendered


def test_transform_blockquote_ordered_list_renders_as_single_ol(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    output_path = tmp_path / "output.md"
    input_path.write_text(
        "> Steps:\n>\n> 1. First step\n> 2. Second step\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, output_path)
    rendered = output_path.read_text(encoding="utf-8")

    assert "<ol>" in rendered
    assert "</ol>" in rendered
    assert "<li>First step</li>" in rendered
    assert "<li>Second step</li>" in rendered
    assert "<ul>" not in rendered


def test_transform_blockquote_list_after_paragraph_closes_list(tmp_path: Path) -> None:
    """A text line between lists must close the open list container."""
    input_path = tmp_path / "input.md"
    output_path = tmp_path / "output.md"
    input_path.write_text(
        "> - one\n> - two\n> plain text\n> - three\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, output_path)
    rendered = output_path.read_text(encoding="utf-8")

    # Two separate (flat) lists with a paragraph between them.
    assert rendered.count("<ul>") == 2
    assert rendered.count("</ul>") == 2
    assert rendered.count("<li>") == 3
    assert '<p dir="ltr" lang="en">plain text</p>' in rendered


def test_transform_blockquote_bullets_stay_flat_after_pandoc(
    tmp_path: Path,
) -> None:
    """End-to-end: pandoc must not nest the bullets inside the quote.

    This is the check that actually reproduced the bug (the transform output
    alone looked fine; pandoc's ``markdown_in_html_blocks`` re-parsed it).
    """
    import shutil
    import subprocess

    if shutil.which("pandoc") is None:
        import pytest

        pytest.skip("pandoc not installed")

    input_path = tmp_path / "input.md"
    transformed_path = tmp_path / "transformed.md"
    html_path = tmp_path / "out.html"
    input_path.write_text(
        "Intro\n\n> [!WARNING]\n> **Unverified citations detected.**\n>\n"
        "> - `https://a.example/x` _(not retrieved)_\n"
        "> - `https://b.example/y` _(not retrieved)_\n"
        "> - `https://c.example/z` _(not retrieved)_\n\nTail\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, transformed_path)
    subprocess.run(
        [
            "pandoc",
            str(transformed_path),
            "--from",
            "markdown+raw_html",
            "--standalone",
            "--metadata",
            "title=t",
            "-t",
            "html5",
            "-o",
            str(html_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    body = html_path.read_text(encoding="utf-8").split("<body>", 1)[-1]

    assert body.count("<ul>") == 1, body
    assert body.count("</ul>") == 1, body
    assert body.count("<li>") == 3, body
    # The list must close before the blockquote container ends -- the buggy
    # output instead closed the blockquote/div *inside* the nested <li>s.
    assert body.index("</ul>") < body.rindex("</blockquote>"), body
    assert "</ul>\n</div>" in body or "</ul>\n</div>\n</blockquote>" in body, body
