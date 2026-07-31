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
        "Intro\n\n> Arabic text\n> English translation\n\nTail\n",
        encoding="utf-8",
    )

    transform_markdown_quotes(input_path, output_path)

    rendered = output_path.read_text(encoding="utf-8")
    assert '<blockquote class="report-quote" dir="auto" lang="ar">' in rendered
    assert '<div class="report-quote-body">' in rendered
    assert '<p dir="auto">' in rendered
    assert 'Intro' in rendered
    assert 'Tail' in rendered
